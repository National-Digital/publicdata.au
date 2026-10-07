"""Draft a register entry from a portal dataset URL, for a person to review.

The draft reads the portal's own metadata and a sample of the file. Each field's type is the
first one the normaliser accepts for every sampled value, so a draft builds as it stands. Search
copy is left blank with a TODO, which `register validate` refuses for a live entry.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pyarrow as pa
import requests
import yaml

from . import catalogue
from .directory import locate
from .fetch import UA, normalise_licence_id
from .normalise import NormaliseError, convert, detect_encoding, read_csv, read_xlsx, xls_to_xlsx
from .publishers import JUR_SEGMENT, PORTAL_JUR, Publisher, clean_title, slugify
from .register import CLOSED_LICENCES, OPEN_LICENCES, Field, draft_label

SAMPLE_BYTES = 20_000_000
WORKBOOK_BYTES = 200_000_000
TABULAR = {
    "CSV": "csv",
    "XLSX": "xlsx",
    "XLSM": "xlsx",
    "EXCEL": "xlsx",
    "EXCEL (.XLSX)": "xlsx",
    "XLS": "xls",
}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%Y%m%d")
DATETIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M")
# Cells a publisher writes in place of a small or withheld count.
SUPPRESSION = re.compile(r"^(<\s*\d+|n\.?p\.?|\*+|\.\.|c)$", re.I)
GOVERNMENT = {
    "Cth": "Australian Government",
    "NSW": "NSW Government",
    "Vic": "Victorian Government",
    "Qld": "Queensland Government",
    "WA": "Government of Western Australia",
    "SA": "Government of South Australia",
    "Tas": "Tasmanian Government",
    "ACT": "ACT Government",
    "NT": "Northern Territory Government",
}
TODO = (
    "search_title",
    "also_known_as",
    "keywords",
    "faq",
)


# What the normaliser raises for a value its type cannot take.
UNFIT = (NormaliseError, pa.ArrowInvalid, pa.ArrowNotImplementedError)


class DraftError(ValueError):
    pass


def portal_for(url: str) -> tuple[catalogue.Portal, str, str]:
    """The CKAN portal, package name and any resource id a dataset URL names."""
    hit = locate(url)
    if not hit or hit[1] != "name":
        raise DraftError(f"{url} does not name a dataset on a portal page")
    host, _, name = hit
    portal = next(
        (p for p in catalogue.PORTALS if p.host.removeprefix("www.") == host and p.kind == "ckan"),
        None,
    )
    if portal is None:
        raise DraftError(f"{host} is not a CKAN portal the catalogue reads")
    m = re.search(r"/resource/([0-9a-f-]{36})", url)
    return portal, name, m.group(1) if m else ""


def field_name(header: str, taken: set[str]) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_") or "field"
    if not s[0].isalpha():
        s = "f_" + s
    s = s[:60]
    name, n = s, 2
    while name in taken:
        name, n = f"{s}_{n}", n + 1
    taken.add(name)
    return name


def _fits(arr: pa.ChunkedArray, f: Field, suppression: tuple[str, ...] = ()) -> bool:
    try:
        convert(arr, f, suppression)
    except UNFIT:
        return False
    return True


def infer(name: str, header: str, arr: pa.ChunkedArray) -> tuple[Field, tuple[str, ...]]:
    """The field and any suppression tokens it uses, typed by the normaliser's own conversion."""
    values = {v.strip() for v in arr.to_pylist() if v is not None and v.strip()}
    base = Field(name, header)
    if not values:
        return base, ()
    # A leading zero is part of a code, such as a postcode, and a number would drop it.
    coded = any(len(v) > 1 and v[0] == "0" and v[1].isdigit() for v in values)
    tokens = tuple(sorted(v for v in values if SUPPRESSION.match(v)))
    for t in ("integer", "number"):
        if coded:
            break
        if _fits(arr, Field(name, header, t)):
            return Field(name, header, t), ()
        if tokens and len(tokens) < len(values) and _fits(arr, Field(name, header, t), tokens):
            return Field(name, header, t), tokens
    if not coded and _fits(arr, Field(name, header, "boolean")):
        return Field(name, header, "boolean"), ()
    for t, formats in (("date", DATE_FORMATS), ("datetime", DATETIME_FORMATS)):
        for fmt in formats:
            if _fits(arr, Field(name, header, t, date_format=fmt)):
                return Field(name, header, t, date_format=fmt), ()
    return base, ()


def _get(s: requests.Session, url: str, cap: int) -> tuple[bytes, bool]:
    with s.get(url, stream=True, timeout=300, allow_redirects=True) as r:
        r.raise_for_status()
        buf = io.BytesIO()
        for chunk in r.iter_content(1 << 20):
            buf.write(chunk)
            if buf.tell() >= cap:
                return buf.getvalue()[:cap], True
    return buf.getvalue(), False


def sample_table(data: bytes, kind: str, cut: bool, sheet: str = "", header_row: int = 1):
    try:
        return _sample_table(data, kind, cut, sheet, header_row)
    except UNFIT as e:
        raise DraftError(f"the file could not be read as {kind.upper()}: {e}") from e


def _sample_table(data: bytes, kind: str, cut: bool, sheet: str, header_row: int):
    if kind in ("xlsx", "xls"):
        if cut:
            raise DraftError("the workbook is larger than a draft reads; draft it by hand")
        if kind == "xls":
            data = xls_to_xlsx(data)
        return read_xlsx(data, sheet, header_row), "xlsx"
    if cut:
        data = data[: data.rfind(b"\n") + 1]
    enc = detect_encoding(data)
    return read_csv(data, enc), enc


def _licence(pkg: dict, host: str = "") -> str:
    lic = normalise_licence_id(pkg.get("license_id") or "", host)
    if lic in OPEN_LICENCES or lic in CLOSED_LICENCES:
        return lic
    by_title = catalogue.licence_id(pkg.get("license_title") or "")
    return by_title or lic or "not-specified"


def publisher_for(portal: catalogue.Portal, pkg: dict, curated: list[Publisher]) -> dict:
    org = pkg.get("organization") or {}
    key = f"{portal.code}:{org.get('name') or 'unknown'}"
    cur = next((p for p in curated if key in p.orgs), None)
    if cur:
        return {
            "name": cur.name,
            "short": cur.short or cur.name,
            "jurisdiction": cur.jurisdiction,
            "url": cur.url,
        }
    name = clean_title(org.get("title") or org.get("name") or portal.host)
    return {
        "name": name,
        "short": name,
        "jurisdiction": PORTAL_JUR[portal.jurisdiction],
        "url": "",
    }


def draft(
    url: str,
    curated: list[Publisher],
    session: requests.Session | None = None,
    resource: str = "",
    slug: str = "",
    sheet: str = "",
    header_row: int = 1,
) -> tuple[str, dict, list[str]]:
    """Returns (slug, the entry, notes for the reviewer)."""
    s = session or requests.Session()
    s.headers["User-Agent"] = UA
    portal, name, in_url = portal_for(url)
    pkg = catalogue.get_json(s, f"{portal.api}/package_show", {"id": name})
    if not pkg.get("success"):
        raise DraftError(f"{portal.host}: package_show found no dataset {name}")
    p = pkg["result"]
    want = resource or in_url
    tabular = [r for r in p.get("resources") or [] if (r.get("format") or "").upper() in TABULAR]
    res = next((r for r in p["resources"] if r["id"] == want), None) if want else None
    if want and res is None:
        raise DraftError(f"resource {want} is not in {name}")
    if res is not None and (res.get("format") or "").upper() not in TABULAR:
        raise DraftError(
            f"resource {want} is {res.get('format') or 'of no stated format'}, not CSV or Excel"
        )
    if res is None:
        if not tabular:
            raise DraftError(f"{name} has no CSV or Excel resource")
        res = next((r for r in tabular if r["format"].upper() == "CSV"), tabular[0])
    notes = []
    if len(tabular) > 1:
        notes.append(
            "other tabular resources: "
            + ", ".join(
                f"{r['id']} ({r.get('name') or r['format']})" for r in tabular if r is not res
            )
        )
    kind = TABULAR.get((res.get("format") or "").upper(), "csv")
    data, cut = _get(s, res["url"], WORKBOOK_BYTES if kind in ("xlsx", "xls") else SAMPLE_BYTES)
    raw, enc = sample_table(data, kind, cut, sheet, header_row)
    if cut:
        notes.append(
            f"types were inferred from the first {raw.num_rows:,} rows; the build types every row and stops on one that does not fit"
        )
    taken: set[str] = set()
    fields, suppression = [], set()
    headers = [c.strip() for c in raw.column_names]
    names = [field_name(h, taken) for h in headers]
    for n, h, col in zip(names, headers, raw.columns, strict=True):
        f, tokens = infer(n, h, col)
        fields.append(f)
        suppression |= set(tokens)
    labels, seen = {}, set()
    for f in fields:
        label = draft_label(f.name, names)
        if label in seen:
            label = draft_label(f.name, [])
        if label in seen:
            label = f.name.replace("_", " ").capitalize()
        seen.add(label)
        labels[f.name] = label
    pub = publisher_for(portal, p, curated)
    lic = _licence(p, portal.host)
    landing = portal.landing(p["name"])
    title = re.sub(r"\s+", " ", p.get("title") or p["name"]).strip()
    open_ = lic in OPEN_LICENCES
    seg = JUR_SEGMENT[pub["jurisdiction"]]
    words = slugify(title).removeprefix(f"{seg}-")
    slug = slug or f"{seg}-{words}"[:64].rstrip("-")
    entry = {
        "slug": slug,
        "title": title,
        "status": "building" if open_ else ("blocked" if lic in CLOSED_LICENCES else "assessing"),
        "summary": catalogue.summary(p.get("notes") or "") or title,
        "description": catalogue.plain_text(p.get("notes") or ""),
        "publisher": {k: v for k, v in pub.items() if v},
        "licence": {
            "id": lic,
            # The date a person reads the licence on the evidence page; no draft can set it.
            "reviewed": "",
            "evidence": landing,
            "attribution": (
                f"{pub['name']}, {GOVERNMENT[pub['jurisdiction']]}, {title}, sourced {{sourced}}, "
                f"{landing}, licensed under {OPEN_LICENCES[lic][0]}."
                if open_
                else ""
            ),
        },
        "source": {
            "adapter": "ckan-resource",
            "portal": portal.api.removesuffix("/api/3/action"),
            "package": p["name"],
            "resource": res["id"],
            "url": landing,
            **({"sheet": sheet} if sheet else {}),
            **({"header_row": header_row} if header_row != 1 else {}),
            **({"encoding": "utf-8-sig"} if enc == "utf-8-sig" else {}),
        },
        **({"suppression": sorted(suppression)} if suppression else {}),
        "search_title": "",
        "also_known_as": [],
        "keywords": [],
        "faq": [],
        "fields": [
            {
                "name": f.name,
                "label": labels[f.name],
                "source": f.source,
                "type": f.type,
                **({"date_format": f.date_format} if f.type in ("date", "datetime") else {}),
            }
            for f in fields
        ],
    }
    if not entry["licence"]["attribution"]:
        entry["licence"].pop("attribution")
    if entry["status"] == "blocked":
        entry["blocked_reason"] = (
            f"The portal states {p.get('license_title') or lic}, which does not allow a re-serialisation."
        )
    if not open_:
        notes.append(f"the portal states the licence as {p.get('license_title') or lic!r}")
    geo = [f.name for f in fields if re.search(r"(^|_)(lat|latitude|lon|lng|longitude)$", f.name)]
    if len(geo) >= 2:
        notes.append(
            f"{' and '.join(geo)} look like coordinates; add geometry with the publisher's CRS"
        )
    if len(fields) > 50:
        notes.append(
            f"{len(fields)} columns: if they are dates or periods, the table may need unpivot"
        )
    if not pub["url"]:
        notes.append("the publisher has no website on record; add publisher.url")
    notes.append("check the attribution against the publisher's own statement")
    return slug, entry, notes


def to_yaml(entry: dict) -> str:
    text = yaml.safe_dump(entry, sort_keys=False, allow_unicode=True, width=100)
    for k in TODO:
        text = re.sub(
            rf"^{k}:",
            f"# TODO: write {k.replace('_', ' ')} before this entry goes live.\n{k}:",
            text,
            flags=re.M,
        )
    return text


def write(register_dir: Path, slug: str, text: str) -> Path:
    path = register_dir / f"{slug}.yaml"
    if path.exists():
        raise DraftError(f"{path.name} already exists")
    path.write_text(text, encoding="utf-8")
    return path
