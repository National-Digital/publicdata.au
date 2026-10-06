"""Fetch adapters. Each returns a Manifest and the bytes, or None when nothing changed.

The version label is the date the publisher changed the file, in Australia/Brisbane time.
An unchanged SHA-256 produces no version. A changed file on a date that already has a
version takes the fetch date, or the next day no version holds, and its notes say so.
"""

from __future__ import annotations

import csv
import datetime as dt
import email.utils
import hashlib
import html
import io
import itertools
import json
import re
import time
import urllib.parse
import zoneinfo
from pathlib import Path

import requests

from . import catalogue, store
from .normalise import detect_encoding
from .register import Dataset

TZ = zoneinfo.ZoneInfo("Australia/Brisbane")
UNREADABLE_DATE = (TypeError, ValueError)
# Victorian government firewalls reset a request whose user agent says "fetcher" or holds a URL
# scheme, so the contact page is given without one.
UA = "publicdata.au/1.0 (+publicdata.au/about/)"
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}


def _date_local(iso: str) -> str:
    t = dt.datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return t.astimezone(TZ).date().isoformat()


def parse_as_at(text: str, regex: str) -> str:
    """Pull an as-at date such as '30 June 2025' out of the publisher's own words."""
    if not regex:
        return ""
    m = re.search(regex, text or "", re.I | re.S)
    if not m:
        return ""
    s = m.group(1).strip()
    mm = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if mm and mm.group(2).lower() in MONTHS:
        return dt.date(int(mm.group(3)), MONTHS[mm.group(2).lower()], int(mm.group(1))).isoformat()
    mm = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if mm:
        return dt.date(int(mm.group(3)), int(mm.group(2)), int(mm.group(1))).isoformat()
    mm = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return mm.group(0) if mm else ""


def ckan_resource(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    s = session or requests.Session()
    s.headers["User-Agent"] = UA
    api = f"{ds.source.portal.rstrip('/')}/api/3/action"
    p = _package(ds, s, api)
    res = pick_resource(ds, p["resources"])
    licence = {
        "id": p.get("license_id", ""),
        "title": p.get("license_title", ""),
        "url": p.get("license_url", ""),
        "read_from": f"{api}/package_show?id={p['name']}",
        "read_at": _now(),
    }
    existing = store.manifests(store_dir, ds.slug)
    if (
        ds.source.manual
        and ds.slug not in MANUAL
        and existing
        and _same_record(existing[-1], p, res)
    ):
        return None, existing[-1], licence
    r = _download(ds, s, res["url"])
    data = r.content
    expect_page(ds, res["url"], r, data)
    digest = hashlib.sha256(data).hexdigest()
    if existing and existing[-1].sha256 == digest:
        return None, existing[-1], licence
    # A portal that replaces the file inside one resource may leave last_modified empty, and the
    # resource's creation date would then date every new file.
    changed = (
        res.get("last_modified")
        or res.get("metadata_modified")
        or res.get("created")
        or p["metadata_modified"]
    )
    # A publisher that replaces the file on its own host leaves the portal's dates behind; the
    # file's Last-Modified is then the later and truer date.
    served = r.headers.get("Last-Modified", "")
    if served:
        try:
            when = email.utils.parsedate_to_datetime(served).astimezone(dt.UTC).isoformat()
            changed = max(_normal_iso(changed), when)
        except UNREADABLE_DATE:
            pass
    version, note = free_version(
        _date_local(changed), {m.version for m in existing}, dt.datetime.now(TZ).date()
    )
    as_at = parse_as_at(
        " ".join([p.get("version") or "", res.get("description") or "", p.get("notes") or ""]),
        ds.source.as_at_regex,
    )
    filename = resource_filename(res)
    m = store.Manifest(
        dataset=ds.slug,
        version=version,
        as_at=as_at,
        fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        sha256=digest,
        bytes=len(data),
        filename=filename,
        encoding=_encoding(filename, data, ds.source.encoding),
        source={
            "url": res["url"],
            "portal": ds.source.portal,
            "package": p["name"],
            "package_id": p["id"],
            "resource": res["id"],
            "resource_name": res.get("name", ""),
            "resource_last_modified": res.get("last_modified"),
            "package_modified": p.get("metadata_modified"),
            "package_version": p.get("version"),
            "etag": etag(r.headers),
            "http_last_modified": r.headers.get("Last-Modified", ""),
        },
        licence=licence,
        notes=[note] if note else [],
    )
    return data, m, licence


def _same_record(m: store.Manifest, p: dict, res: dict) -> bool:
    """Whether the portal still describes the file the newest version was made from."""
    was = m.source
    return (
        was.get("url") == res["url"]
        and was.get("resource_last_modified") == res.get("last_modified")
        and was.get("package_modified") == p.get("metadata_modified")
    )


def _package(ds: Dataset, s: requests.Session, api: str) -> dict:
    if ds.source.package_match:
        found = s.get(
            f"{api}/package_search",
            params={"q": ds.source.package, "sort": "metadata_created desc", "rows": 100},
            timeout=60,
        ).json()
        if not found.get("success"):
            raise RuntimeError(f"{ds.slug}: package_search failed: {found.get('error')}")
        name = pick_package(ds, found["result"]["results"])
    else:
        name = ds.source.package
    pkg = s.get(f"{api}/package_show", params={"id": name}, timeout=60).json()
    if not pkg.get("success"):
        raise RuntimeError(f"{ds.slug}: package_show failed: {pkg.get('error')}")
    return pkg["result"]


def pick_package(ds: Dataset, packages: list[dict]) -> str:
    """The newest package whose name matches, by the date the portal created it."""
    rx = re.compile(ds.source.package_match)
    hits = [p for p in packages if rx.search(p.get("name", ""))]
    if not hits:
        raise FetchError(f"{ds.slug}: no package matches '{ds.source.package_match}'")
    return max(hits, key=lambda p: p.get("metadata_created") or "")["name"]


def pick_resource(ds: Dataset, resources: list[dict]) -> dict:
    """The named resource, or the newest whose name matches: latest created, then last listed,
    since a portal that re-imports old files gives them all one creation date."""
    if not ds.source.resource_match:
        res = next((r for r in resources if r["id"] == ds.source.resource), None)
        if res is None:
            raise RuntimeError(f"{ds.slug}: resource {ds.source.resource} not in package")
        return res
    rx = re.compile(ds.source.resource_match)
    hits = [
        (r.get("created") or "", r.get("last_modified") or "", i, r)
        for i, r in enumerate(resources)
        if rx.search((r.get("name") or "").strip())
    ]
    if not hits:
        raise FetchError(f"{ds.slug}: no resource matches '{ds.source.resource_match}'")
    # A re-import gives every resource one creation date; its file's change date then decides.
    return max(hits, key=lambda h: h[:3])[3]


def etag(headers) -> str:
    """The ETag without its quotes, keeping the W/ that marks a weak one."""
    tag = headers.get("ETag", "").strip()
    weak = tag.startswith("W/")
    tag = tag[2:].strip('"') if weak else tag.strip('"')
    return f"W/{tag}" if weak else tag


def resource_filename(res: dict) -> str:
    """The file's name from its URL. A name with no extension takes the resource's stated format,
    since the reader is chosen by extension."""
    name = res["url"].split("?", 1)[0].rsplit("/", 1)[-1]
    # A download script, main.html?download&realfilename=layer.zip, names the file it serves.
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(res["url"]).query)
    named = (query.get("realfilename") or query.get("filename") or [""])[0]
    if Path(named).suffix:
        name = named.rsplit("/", 1)[-1]
    # A file served through a page script, report.xlsx.aspx, is the file its inner name gives.
    if name.lower().endswith(".aspx") and Path(name[:-5]).suffix:
        name = name[:-5]
    fmt = (res.get("format") or "").strip().lstrip(".").lower()
    if not name:
        return f"source.{fmt or 'bin'}"
    has_ext = Path(name).suffix and Path(name).suffix.lower() != ".aspx"
    return name if has_ext or not fmt else f"{name}.{fmt}"


def free_version(changed: str, taken: set[str], today: dt.date) -> tuple[str, str]:
    """The version label for a changed file: the publisher's change date, or when a version
    already holds that date, the fetch date, or else the next day no version holds. The note
    says why the label is not the change date."""
    if changed not in taken:
        return changed, ""
    day = today
    while day.isoformat() in taken:
        day += dt.timedelta(days=1)
    return day.isoformat(), (
        f"The publisher's file changed on {changed}, which an earlier version already carries, "
        f"so this version is dated {day.isoformat()}."
    )


EXPORT_WAITS = 12
EXPORT_WAIT_SECONDS = 10


def _session(session: requests.Session | None) -> requests.Session:
    s = session or requests.Session()
    s.headers["User-Agent"] = UA
    return s


class Fetched:
    """A file a person downloaded, shaped like the response the adapters read."""

    def __init__(self, content: bytes, headers: dict[str, str]):
        self.content = content
        self.headers = requests.structures.CaseInsensitiveDict(headers)
        self.status_code = 200


# Files a person downloaded for manual sources, by slug; the fetch command fills it from --file.
MANUAL: dict[str, Path] = {}


class ManualDue(RuntimeError):
    pass


def _download(ds: Dataset, s: requests.Session, url: str):
    """The file's bytes and headers: by the session, or from the file a person downloaded when
    the entry's host turns automated clients away."""
    if ds.source.manual:
        if ds.slug not in MANUAL:
            raise ManualDue(f"{ds.slug}: download {url} and run the fetch with --file")
        got = MANUAL[ds.slug]
        if got.is_dir():
            # A stack's files are given as one folder, each under the name its address ends in.
            got = got / urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
            if not got.is_file():
                raise FetchError(f"{ds.slug}: {got.name} is not in {MANUAL[ds.slug]}")
        return Fetched(got.read_bytes(), {})
    r = s.get(url, timeout=600, allow_redirects=True)
    r.raise_for_status()
    return r


def _changed(ds: Dataset, value, unit: int = 0) -> str:
    """The portal's change date as UTC ISO, from an epoch in seconds (unit 1), milliseconds
    (unit 1000) or an ISO string (unit 0). A version is dated by it, so a portal that states none
    is a failed fetch rather than a guessed date."""
    try:
        if unit:
            return dt.datetime.fromtimestamp(int(value) / unit, dt.UTC).isoformat()
        return _normal_iso(str(value))
    except UNREADABLE_DATE as e:
        raise FetchError(f"{ds.slug}: the portal states no change date ({value!r})") from e


def _licence(
    ds: Dataset, stated: str, normalised: str, title: str, url: str, read_from: str
) -> dict:
    """The licence a portal states, keeping its own code and the id worked out from its words.
    `id` is the one the register is checked against: the portal's code when the entry names one
    in licence.portal_id, as a CKAN portal's id is, and otherwise the worked-out id. `read_from`
    is the address the statement was read from, and `read_at` when."""
    return {
        "id": stated if ds.licence.portal_id else normalised,
        "stated": stated,
        "normalised": normalised,
        "title": title,
        "url": url,
        "read_from": read_from,
        "read_at": _now(),
    }


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _portal_version(
    ds: Dataset,
    store_dir: Path,
    s: requests.Session,
    url: str,
    changed: str,
    filename: str,
    source: dict,
    licence: dict,
):
    """Download a portal's export and make its manifest. The export is dated by the portal's own
    change date, and an unchanged SHA-256 is no version."""
    existing = store.manifests(store_dir, ds.slug)
    r = s.get(url, timeout=600, allow_redirects=True)
    # A portal that builds its export on request answers 202 until the file is ready.
    for _ in range(EXPORT_WAITS):
        if r.status_code != 202:
            break
        time.sleep(EXPORT_WAIT_SECONDS)
        r = s.get(url, timeout=600, allow_redirects=True)
    r.raise_for_status()
    if r.status_code != 200:
        raise FetchError(f"{ds.slug}: {url} answered HTTP {r.status_code} without the file")
    data = r.content
    expect_page(ds, url, r, data)
    digest = hashlib.sha256(data).hexdigest()
    if existing and existing[-1].sha256 == digest:
        return None, existing[-1], licence
    version, note = free_version(
        _date_local(changed), {m.version for m in existing}, dt.datetime.now(TZ).date()
    )
    m = store.Manifest(
        dataset=ds.slug,
        version=version,
        as_at="",
        fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        sha256=digest,
        bytes=len(data),
        filename=filename,
        encoding=_encoding(filename, data, ds.source.encoding),
        source={
            "url": url,
            "portal": ds.source.portal,
            **source,
            "etag": etag(r.headers),
            "http_last_modified": r.headers.get("Last-Modified", ""),
        },
        licence=licence,
        notes=[note] if note else [],
    )
    return data, m, licence


def socrata_view(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """A Socrata dataset, such as the ACT's, exported whole as CSV. package is its four-by-four."""
    s = _session(session)
    base = ds.source.portal.rstrip("/")
    view = catalogue.get_json(s, f"{base}/api/views/{ds.source.package}.json")
    lic = view.get("license") or {}
    title = lic.get("name") or ""
    return _portal_version(
        ds,
        store_dir,
        s,
        f"{base}/api/views/{ds.source.package}/rows.csv?accessType=DOWNLOAD",
        _changed(ds, view.get("rowsUpdatedAt") or view.get("viewLastModified"), 1),
        f"{ds.source.package}.csv",
        {
            "package": ds.source.package,
            "resource_name": view.get("name", ""),
            "rows_updated_at": view.get("rowsUpdatedAt"),
        },
        _licence(
            ds,
            view.get("licenseId") or title,
            catalogue.licence_id(title),
            title,
            lic.get("termsLink", ""),
            f"{base}/api/views/{ds.source.package}.json",
        ),
    )


def opendatasoft(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """An Opendatasoft dataset exported whole as comma-separated CSV. package is its dataset id."""
    s = _session(session)
    base = f"{ds.source.portal.rstrip('/')}/api/explore/v2.1/catalog/datasets/{ds.source.package}"
    meta = (catalogue.get_json(s, base).get("metas") or {}).get("default") or {}
    title = meta.get("license") or ""
    return _portal_version(
        ds,
        store_dir,
        s,
        f"{base}/exports/csv?delimiter=%2C&with_bom=false",
        _changed(ds, meta.get("data_processed") or meta.get("modified")),
        f"{ds.source.package}.csv",
        {
            "package": ds.source.package,
            "resource_name": meta.get("title", ""),
            "data_processed": meta.get("data_processed"),
        },
        _licence(
            ds,
            title,
            catalogue.cc_url(meta.get("license_url") or "") or catalogue.licence_id(title),
            title,
            meta.get("license_url") or "",
            base,
        ),
    )


def arcgis_hub(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """A layer of an ArcGIS Hub item, as the CSV the site's download API serves. package is the
    item id and resource the layer number, 0 when absent."""
    s = _session(session)
    base = ds.source.portal.rstrip("/")
    item = catalogue.get_json(
        s, f"{base}/api/search/v1/collections/dataset/items/{ds.source.package}"
    )
    props = item.get("properties") or item
    lic_id, lic_title = catalogue.hub_licence(props)
    layer = ds.source.resource or "0"
    changed = props.get("modified")
    if props.get("url"):
        info = catalogue.get_json(s, f"{props['url'].rstrip('/')}/{layer}", {"f": "json"})
        changed = (info.get("editingInfo") or {}).get("lastEditDate") or changed
    return _portal_version(
        ds,
        store_dir,
        s,
        f"{base}/api/download/v1/items/{ds.source.package}/csv?layers={layer}",
        _changed(ds, changed, 1000),
        f"{ds.source.package}_{layer}.csv",
        {
            "package": ds.source.package,
            "resource": layer,
            "resource_name": props.get("title", ""),
            "service": props.get("url", ""),
        },
        _licence(
            ds,
            props.get("license") or lic_id,
            lic_id,
            lic_title,
            "",
            f"{base}/api/search/v1/collections/dataset/items/{ds.source.package}",
        ),
    )


def arcgis_feature(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """An ArcGIS feature layer with no file behind it: every feature is read in pages ordered by
    the layer's object id, in WGS84, and kept as one GeoJSON file. url is the layer; the licence
    is the one the CKAN package named in portal and package states."""
    s = _session(session)
    p = _package(ds, s, f"{ds.source.portal.rstrip('/')}/api/3/action")
    stated = p.get("license_id", "")
    licence = _licence(
        ds,
        stated,
        normalise_licence_id(stated, ds.source.portal),
        p.get("license_title", ""),
        p.get("license_url", ""),
        f"{ds.source.portal.rstrip('/')}/api/3/action/package_show?id={p['name']}",
    )
    layer = ds.source.url.rstrip("/")
    info = catalogue.get_json(s, layer, {"f": "pjson"})
    if "fields" not in info:
        raise FetchError(f"{ds.slug}: {layer} is not a feature layer: {str(info)[:200]}")
    oid = info.get("objectIdField") or next(
        (f["name"] for f in info["fields"] if f["type"] == "esriFieldTypeOID"), "OBJECTID"
    )
    page = min(int(info.get("maxRecordCount") or 1000), 2000)
    feats: list[dict] = []
    offset = 0
    while True:
        got = catalogue.get_json(
            s,
            f"{layer}/query",
            {
                "where": "1=1",
                "outFields": "*",
                "orderByFields": oid,
                "resultOffset": offset,
                "resultRecordCount": page,
                "outSR": 4326,
                "f": "geojson",
            },
            timeout=300,
        )
        if "features" not in got:
            raise FetchError(f"{ds.slug}: page at {offset} is not GeoJSON: {str(got)[:200]}")
        feats.extend(got["features"])
        if len(got["features"]) < page:
            break
        offset += page
    if not feats:
        raise FetchError(f"{ds.slug}: {layer} returned no features")
    lines = ",\n".join(json.dumps(f, sort_keys=True, separators=(",", ":")) for f in feats)
    data = f'{{"type":"FeatureCollection","features":[\n{lines}\n]}}\n'.encode()
    digest = hashlib.sha256(data).hexdigest()
    existing = store.manifests(store_dir, ds.slug)
    if existing and existing[-1].sha256 == digest:
        return None, existing[-1], licence
    date_fields = [f["name"] for f in info["fields"] if f["type"] == "esriFieldTypeDate"]
    edited = (info.get("editingInfo") or {}).get("lastEditDate")
    newest = max(
        (
            v
            for f in feats
            for k, v in (f.get("properties") or {}).items()
            if k in date_fields and v
        ),
        default=None,
    )
    # A layer that states no edit date is dated by the newest record it holds, which moves with
    # each release, and failing that by the portal record's own modified date.
    if edited:
        changed, why = _changed(ds, edited, 1000), ""
    elif newest:
        changed = _changed(ds, newest, 1000)
        why = (
            "The layer states no edit date, so this version is dated by the newest record it holds."
        )
    else:
        changed = _changed(ds, p.get("metadata_modified"))
        why = "The layer states no edit date or dated records, so this version is dated by the portal record."
    today = dt.datetime.now(TZ).date()
    version, note = free_version(_date_local(changed), {m.version for m in existing}, today)
    m = store.Manifest(
        dataset=ds.slug,
        version=version,
        as_at=parse_as_at(p.get("notes") or "", ds.source.as_at_regex),
        fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        sha256=digest,
        bytes=len(data),
        filename=f"{ds.slug}.geojson",
        encoding="utf-8",
        source={
            "url": layer,
            "portal": ds.source.portal,
            "package": p["name"],
            "package_id": p["id"],
            "package_modified": p.get("metadata_modified"),
            "layer_name": info.get("name", ""),
            "object_id_field": oid,
            "last_edit_date": changed if edited else None,
            "newest_record": newest,
            "features": len(feats),
            "date_fields": date_fields,
        },
        licence=licence,
        notes=[n for n in (why, note, ARCGIS_NOTE) if n],
    )
    return data, m, licence


ARCGIS_NOTE = (
    "The publisher offers no file. These bytes are every feature of the layer as the service "
    "returned them in WGS84, ordered by its object id, so the same layer gives the same bytes."
)


ALA_LICENCES = ("CC0", "CC-BY", "CC-BY 4.0 (Int)", "CC-BY 3.0 (Au)")
ALA_FIELDS = (
    "id",
    "occurrenceID",
    "raw_catalogNumber",
    "raw_institutionCode",
    "raw_collectionCode",
    "scientificName",
    "vernacularName",
    "taxonRank",
    "species",
    "eventDate",
    "year",
    "month",
    "stateProvince",
    "decimalLatitude",
    "decimalLongitude",
    "coordinateUncertaintyInMeters",
    "basisOfRecord",
    "dataResourceUid",
    "dataResourceName",
    "license",
    "typeStatus",
    "locality",
    "occurrenceStatus",
    "firstLoadedDate",
)
ALA_COLUMNS = ("uuid", *ALA_FIELDS[1:])
# The API is asked for the plain names and answers with the provider's raw values under these.
ALA_REQUEST = ",".join(f.removeprefix("raw_") for f in ALA_FIELDS)
ALA_PAGE = 100
# The search API answers no page past this offset, so a larger slice is split before it is read.
ALA_DEEP = 5000
ALA_NOTE = (
    "The rows are the Atlas's records from the named government providers under CC0 and CC BY "
    "licences, read from its search API. The provider and the licence are in every row."
)


def _ala_count(s, base: str, q: str, fq: list[str]) -> int:
    got = catalogue.get_json(s, base, {"q": q, "fq": fq, "pageSize": 0}, timeout=120)
    if "totalRecords" not in got:
        raise FetchError(f"ALA count failed: {str(got)[:200]}")
    return int(got["totalRecords"])


def _ala_pages(s, base: str, q: str, fq: list[str], n: int):
    for start in range(0, n, ALA_PAGE):
        got = catalogue.get_json(
            s,
            base,
            {
                "q": q,
                "fq": fq,
                "pageSize": ALA_PAGE,
                "startIndex": start,
                "fl": ALA_REQUEST,
                # Sorting by load date drops rows: whole slices share one second and the
                # API's page order is not stable across ties. The record id is unique.
                "sort": "id",
                "dir": "asc",
            },
            timeout=120,
        )
        rows = got.get("occurrences")
        if rows is None:
            raise FetchError(f"ALA page at {start} failed: {str(got)[:200]}")
        yield from rows
        if len(rows) < ALA_PAGE:
            break


def _ala_slices(s, base: str, q: str, fq: list[str], n: int, lo: float, hi: float, dim: int):
    """Rows of a query too large for the API's paging, split by load time, then by latitude
    and longitude, then by year, until every slice fits."""
    if n <= ALA_DEEP:
        yield from _ala_pages(s, base, q, fq, n)
        return
    dims = ("first_loaded_date", "decimalLatitude", "decimalLongitude", "year")
    field = dims[dim]
    if field == "first_loaded_date":
        if hi - lo <= 1:
            # One second's load: the rows without coordinates go by year, the rest by place.
            for cond, a, b, d in (
                ("-decimalLatitude:*", 0, 2200, 3),
                ("decimalLatitude:*", -90, 90, 1),
            ):
                sub = [*fq, cond]
                m = _ala_count(s, base, q, sub)
                if m:
                    yield from _ala_slices(s, base, q, sub, m, a, b, d)
            return
        mid = lo + (hi - lo) / 2
        fmt = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        halves = [
            (f"{field}:[{fmt(lo)} TO {fmt(mid)}}}", lo, mid),
            (f"{field}:[{fmt(mid)} TO {fmt(hi)}]", mid, hi),
        ]
    elif field == "year":
        if hi - lo <= 1:
            raise FetchError(f"ALA: {n} rows in one place and year cannot be read: {fq}")
        mid = int(lo + (hi - lo) / 2)
        halves = [(f"year:[{int(lo)} TO {mid}}}", lo, mid), (f"year:[{mid} TO {int(hi)}]", mid, hi)]
    else:
        if hi - lo <= 0.0001:
            yield from _ala_slices(
                s, base, q, fq, n, -180 if dim == 1 else 0, 180 if dim == 1 else 2200, dim + 1
            )
            return
        mid = (lo + hi) / 2
        halves = [
            (f"{field}:[{lo} TO {mid}}}", lo, mid),
            (f"{field}:[{mid} TO {hi}]", mid, hi),
        ]
    for cond, a, b in halves:
        sub = [*fq, cond]
        m = _ala_count(s, base, q, sub)
        if m:
            yield from _ala_slices(s, base, q, sub, m, a, b, dim)


def ala(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """The Atlas of Living Australia's search API: every record the search matches from each
    provider the register names, under an open licence, kept as one CSV. The version is dated
    by the newest load date among the rows. The manifest counts each provider's rows with and
    without the licence filter, and a provider whose open rows fall while its rows do not is a
    licence change, which stops the fetch."""
    s = _session(session)
    base = ds.source.url.rstrip("/")
    if not ds.source.search or not ds.source.providers:
        raise FetchError(f"{ds.slug}: an ala source needs a search and providers")
    common = [
        "country:Australia",
        "license:(" + " OR ".join(f'"{x}"' for x in ALA_LICENCES) + ")",
    ]
    existing = store.manifests(store_dir, ds.slug)
    before = (existing[-1].source.get("providers") or {}) if existing else {}
    rows: dict[str, dict] = {}
    counts: dict[str, dict] = {}
    now = dt.datetime.now(dt.UTC).timestamp()
    for uid in ds.source.providers:
        fq = [*common, f"dataResourceUid:{uid}"]
        n = _ala_count(s, base, ds.source.search, fq)
        every = _ala_count(s, base, ds.source.search, [common[0], f"dataResourceUid:{uid}"])
        counts[uid] = {"open": n, "all": every}
        # Fewer open rows from a provider whose rows did not fall means it changed a licence.
        was = before.get(uid) or {}
        if was and n < was.get("open", 0) and every >= was.get("all", 0):
            raise LicenceDrift(
                f"{ds.slug}: {uid} had {was['open']} rows under an open licence and now has {n}"
                f" of {every}. Its licence has changed; review before fetching again."
            )
        got = 0
        for r in _ala_slices(s, base, ds.source.search, fq, n, 0, now, 0):
            key = r.get("uuid") or r.get("id")
            if key and key not in rows:
                rows[key] = r
                got += 1
        if got != n:
            raise FetchError(f"{ds.slug}: {uid} has {n} rows and {got} were read")
    if not rows:
        raise FetchError(f"{ds.slug}: the query matched no rows")
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(ALA_COLUMNS)
    for key in sorted(rows):
        r = rows[key]
        other = r.get("otherProperties") or {}
        line = [key]
        for f in ALA_FIELDS[1:]:
            v = r.get(f, other.get(f))
            if f == "firstLoadedDate" and v:
                v = str(v)[:19] + "Z"
            line.append("" if v is None else v)
        w.writerow(line)
    data = out.getvalue().encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    seen: dict[str, int] = {}
    for r in rows.values():
        seen[str(r.get("license"))] = seen.get(str(r.get("license")), 0) + 1
    licence = _licence(
        ds,
        "CC-BY-4.0",
        "CC-BY-4.0",
        "CC BY 4.0 and CC0, by provider",
        ds.licence.evidence,
        f"{base}/occurrences/search",
    )
    if existing and existing[-1].sha256 == digest:
        return None, existing[-1], licence
    newest = max(
        (
            str(
                (r.get("otherProperties") or {}).get("firstLoadedDate")
                or r.get("firstLoadedDate")
                or ""
            )
            for r in rows.values()
        ),
        default="",
    )
    if not newest:
        raise FetchError(f"{ds.slug}: no row states a load date")
    changed = _changed(ds, newest[:19] + "+00:00")
    today = dt.datetime.now(TZ).date()
    version, note = free_version(_date_local(changed), {m.version for m in existing}, today)
    m = store.Manifest(
        dataset=ds.slug,
        version=version,
        as_at="",
        fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        sha256=digest,
        bytes=len(data),
        filename=f"{ds.slug}.csv",
        encoding="utf-8",
        source={
            "url": base,
            "search": ds.source.search,
            "filters": common,
            "providers": counts,
            "licences": seen,
            "records": len(rows),
            "newest_load": changed,
        },
        licence=licence,
        notes=[x for x in (note, ALA_NOTE) if x],
    )
    return data, m, licence


def page_text(body: bytes) -> str:
    """A page's words, with tags gone and whitespace folded, for matching a licence statement."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", body.decode("utf-8", "replace")))
    return " ".join(text.split())


def statement_licence(ds: Dataset, s: requests.Session) -> dict:
    """The licence a publisher states on its own page, for a file no portal describes: the
    register's licence holds while the page still carries the words the entry quotes."""
    r = s.get(ds.licence.evidence, timeout=120)
    r.raise_for_status()
    body = r.content
    if ds.licence.statement not in page_text(body):
        raise LicenceDrift(
            f"{ds.slug}: {ds.licence.evidence} no longer says '{ds.licence.statement[:80]}'; "
            "review the licence before fetching again"
        )
    return {
        "id": ds.licence.id,
        "stated": ds.licence.statement,
        "normalised": ds.licence.id,
        "title": ds.licence.title,
        "url": ds.licence.evidence,
        "read_from": ds.licence.evidence,
        "read_at": _now(),
    }


WFS_PAGES = 1000


def _wfs_pages(ds: Dataset, s: requests.Session):
    """Every feature of a WFS GetFeature, `page_size` at a time in the order its sortBy gives, as
    one GeoJSON FeatureCollection. The last response stands for the whole for its headers."""
    feats: list = []
    sep = "&" if "?" in ds.source.url else "?"
    for page in range(WFS_PAGES):
        url = f"{ds.source.url}{sep}count={ds.source.page_size}&startIndex={page * ds.source.page_size}"
        r = _download(ds, s, url)
        expect_page(ds, url, r, r.content)
        got = json.loads(r.content).get("features") or []
        feats.extend(got)
        if len(got) < ds.source.page_size:
            break
    else:
        raise FetchError(f"{ds.slug}: more than {WFS_PAGES} pages of features")
    lines = ",\n".join(json.dumps(x, sort_keys=True, separators=(",", ":")) for x in feats)
    return r, f'{{"type":"FeatureCollection","features":[\n{lines}\n]}}\n'.encode()


FILE_NOTE = "The publisher's server states no change date, so this version is dated by the fetch."
UNREAD_DATE_NOTE = (
    "The publisher's server states a change date this fetch could not read, so this version is "
    "dated by the fetch."
)


def http_file(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """A file at a fixed URL on the publisher's own site, with no portal record. The version is
    dated by the server's Last-Modified, or failing that by the fetch, and the licence is the
    evidence page's own words, read every run."""
    s = _session(session)
    licence = statement_licence(ds, s)
    existing = store.manifests(store_dir, ds.slug)
    if ds.source.page_size:
        r, data = _wfs_pages(ds, s)
    else:
        r = _download(ds, s, ds.source.url)
        data = r.content
    expect_page(ds, ds.source.url, r, data)
    digest = hashlib.sha256(data).hexdigest()
    if existing and existing[-1].sha256 == digest:
        return None, existing[-1], licence
    served = r.headers.get("Last-Modified", "")
    changed, why = "", FILE_NOTE
    if served:
        try:
            changed = email.utils.parsedate_to_datetime(served).astimezone(dt.UTC).isoformat()
            why = ""
        except UNREADABLE_DATE:
            why = UNREAD_DATE_NOTE
    if not changed:
        changed = dt.datetime.now(dt.UTC).isoformat()
    today = dt.datetime.now(TZ).date()
    version, note = free_version(_date_local(changed), {m.version for m in existing}, today)
    filename = (
        f"{ds.slug}.{ds.source.format}"
        if ds.source.format
        else ds.source.url.split("?", 1)[0].rsplit("/", 1)[-1] or "source.bin"
    )
    m = store.Manifest(
        dataset=ds.slug,
        version=version,
        as_at="",
        fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        sha256=digest,
        bytes=len(data),
        filename=filename,
        encoding=_encoding(filename, data, ds.source.encoding),
        source={
            "url": ds.source.url,
            "etag": etag(r.headers),
            "http_last_modified": served,
        },
        licence=licence,
        notes=[n for n in (why, note) if n],
    )
    return data, m, licence


KIWIS_STATION_FIELDS = (
    "station_no",
    "station_name",
    "station_latitude",
    "station_longitude",
    "custom_attributes",
)
KIWIS_STATION_COLUMNS = {
    "station_no": "station_no",
    "station_name": "station_name",
    "station_latitude": "latitude",
    "station_longitude": "longitude",
    "DATA_OWNER_NAME": "data_owner",
    "FullStorageVolume": "full_storage_volume_ml",
    "DeadStorageVolume": "dead_storage_volume_ml",
    "FullStorageLevel": "full_storage_level_m",
    "DeadStorageLevel": "dead_storage_level_m",
    "REGULATION_NAME": "regulation",
}
# Series are packed into requests under this many values by their day counts; the service's own
# limit is 250,000, counted its way, and a batch it still refuses is split.
KIWIS_BATCH_VALUES = 120_000
KIWIS_NOTE = (
    "The publisher offers no file. These bytes are every value of the named time series for every "
    "station reporting the parameter, as the Bureau's Water Data Online service returned them, "
    "ordered by station and date, so the same data gives the same bytes."
)
KIWIS_STATIONS_NOTE = (
    "The publisher offers no file and states no change date for its station list, so this version "
    "is dated by the fetch. An unchanged list is no version."
)


def _kiwis_query(s: requests.Session, base: str, request: str, **params):
    """One KiWIS query. The service answers 500 to a space sent as "+", so the query is
    percent-encoded, and a refusal it explains in JSON, such as too many values, is final
    rather than retried."""
    query = {
        "service": "kisters",
        "type": "queryServices",
        "datasource": 0,
        "format": "json",
        "request": request,
        **params,
    }
    encoded = urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    try:
        got = catalogue.get_json(s, f"{base}?{encoded}", timeout=600)
    except requests.HTTPError as e:
        try:
            said = e.response.json() if e.response is not None else None
        except ValueError:
            said = None
        if isinstance(said, dict) and said.get("code"):
            raise FetchError(
                f"KiWIS {request} refused: {said.get('code')}: {said.get('message')}"
            ) from e
        raise
    if isinstance(got, dict):
        raise FetchError(f"KiWIS {request} failed: {str(got)[:200]}")
    return got


def _kiwis_values(s: requests.Session, base: str, batch: list[str]) -> list[dict]:
    """The values of a batch of series. The service counts values its own way, so a batch it
    refuses as too large is split until it answers."""
    try:
        return _kiwis_query(
            s,
            base,
            "getTimeseriesValues",
            ts_id=",".join(batch),
            period="complete",
            returnfields="Timestamp,Value,Quality Code",
            metadata="true",
            md_returnfields="station_no,ts_id",
        )
    except FetchError as e:
        if "TooManyResults" not in str(e) or len(batch) < 2:
            raise
        half = len(batch) // 2
        return [*_kiwis_values(s, base, batch[:half]), *_kiwis_values(s, base, batch[half:])]


def _kiwis_state(owner: str) -> str:
    """The state or territory in the Bureau's owner label, 'NSW - Water NSW' giving NSW."""
    head, sep, _ = owner.partition(" - ")
    return head.strip() if sep else ""


def kiwis(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """The Bureau of Meteorology's Water Data Online (a Kisters KiWIS service). `search` is the
    parameter type, `package` the time series name and `resource` the table: `stations` is the
    stations reporting the parameter with their attributes, `values` every value of the series for
    every station, as one CSV each. The licence is the evidence page's own words."""
    s = _session(session)
    if not (
        ds.source.search and ds.source.package and ds.source.resource in ("stations", "values")
    ):
        raise FetchError(f"{ds.slug}: a kiwis source needs search, package and resource")
    licence = statement_licence(ds, s)
    base = ds.source.url
    raw = _kiwis_query(
        s,
        base,
        "getStationList",
        parametertype_name=ds.source.search,
        returnfields=",".join(KIWIS_STATION_FIELDS),
    )
    header, rows = raw[0], raw[1:]
    stations = {r[0]: dict(zip(header, r, strict=True)) for r in rows}
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    source: dict = {"url": base, "parameter": ds.source.search, "stations": len(stations)}
    if ds.source.resource == "stations":
        w.writerow([*KIWIS_STATION_COLUMNS.values(), "state"])
        for no in sorted(stations):
            st = stations[no]
            w.writerow(
                [
                    *(st.get(k, "") for k in KIWIS_STATION_COLUMNS),
                    _kiwis_state(st.get("DATA_OWNER_NAME", "")),
                ]
            )
        changed, notes = dt.datetime.now(dt.UTC).isoformat(), [KIWIS_STATIONS_NOTE]
    else:
        series = _kiwis_query(
            s,
            base,
            "getTimeseriesList",
            parametertype_name=ds.source.search,
            ts_name=ds.source.package,
            # coverage answers as the from and to columns; naming those two is refused.
            returnfields="station_no,ts_id,ts_name,coverage",
        )
        dated = [dict(zip(series[0], r, strict=True)) for r in series[1:] if r[3] and r[4]]
        if not dated:
            raise FetchError(f"{ds.slug}: no station has a dated {ds.source.package} series")
        values: dict[str, list] = {}
        for batch in _kiwis_batches(dated):
            for ts in _kiwis_values(s, base, batch):
                values[ts["station_no"]] = ts.get("data") or []
        newest = ""
        w.writerow(["station_no", "station_name", "state", "date", "value", "quality_code"])
        for no in sorted(values):
            st = stations.get(no, {})
            state = _kiwis_state(st.get("DATA_OWNER_NAME", ""))
            for stamp, value, quality in sorted(values[no]):
                newest = max(newest, stamp)
                w.writerow([no, st.get("station_name", ""), state, stamp[:10], value, quality])
        if not newest:
            raise FetchError(f"{ds.slug}: the series hold no values")
        changed, notes = _changed(ds, newest), [KIWIS_NOTE]
        source |= {"series": ds.source.package, "series_read": len(values), "newest": newest}
    data = out.getvalue().encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    existing = store.manifests(store_dir, ds.slug)
    if existing and existing[-1].sha256 == digest:
        return None, existing[-1], licence
    today = dt.datetime.now(TZ).date()
    version, note = free_version(_date_local(changed), {m.version for m in existing}, today)
    m = store.Manifest(
        dataset=ds.slug,
        version=version,
        as_at="",
        fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        sha256=digest,
        bytes=len(data),
        filename=f"{ds.slug}.csv",
        encoding="utf-8",
        source=source,
        licence=licence,
        notes=[n for n in (note, *notes) if n],
    )
    return data, m, licence


def _kiwis_batches(series: list[dict]) -> list[list[str]]:
    """Series packed into requests by the days each covers, under the service's value limit."""
    est = []
    for r in series:
        days = (
            dt.date.fromisoformat(r["to"][:10]) - dt.date.fromisoformat(r["from"][:10])
        ).days + 1
        est.append((r["ts_id"], max(days, 1)))
    batches: list[list[str]] = []
    cur: list[str] = []
    n = 0
    for tid, e in sorted(est, key=lambda x: (-x[1], x[0])):
        if cur and n + e > KIWIS_BATCH_VALUES:
            batches.append(cur)
            cur, n = [], 0
        cur.append(tid)
        n += e
    if cur:
        batches.append(cur)
    return batches


AIHW_LISTING = "https://www.aihw.gov.au/api/search/all-downloadable-resources"
AIHW_ATTRS = re.compile(r'<div[^>]*class="s-downloadable-resources[^"]*"(.*?)>', re.S)


def _aihw_listing(page: bytes) -> dict:
    """The query the AIHW data page makes for its file list, from the wrapper's attributes."""
    for m in AIHW_ATTRS.finditer(page.decode("utf-8", "replace")):
        d = dict(re.findall(r'data-([a-z-]+)="([^"]*)"', m.group(1)))
        if "report-node-guid" in d:
            return _aihw_query(d)
    raise FetchError("the page has no downloadable resources list")


def _aihw_query(d: dict) -> dict:
    def flag(k: str) -> bool:
        return d.get(k, "").lower() == "true"

    return {
        "enableTagFilter": flag("enable-tag-filter"),
        "keywords": [],
        "itemsPerPage": 100,
        "filterByCurrentReport": flag("filter-by-current-report"),
        "filterByCurrentTopic": flag("filter-by-current-topic"),
        "filterByContentType": flag("filter-by-content-type"),
        "contentType": d.get("content-type", ""),
        "filterByChildrenOfCurrentNode": flag("filter-by-children-of-current-node"),
        "filterByChildrenOfSelectedNode": d.get("filter-by-children-of-selected-node", ""),
        "orderByColumn": d.get("order-by-column", ""),
        "includeArchived": None,
        "searchTerm": None,
        "dateFrom": None,
        "dateTo": None,
        "topic": None,
        "subtopics": None,
        "resourceTypes": None,
        "filterByItemNodeGUIDs": None,
        "reportNodeGuid": d["report-node-guid"],
        "currentNodeId": int(d.get("current-node-id") or 0),
        "showDescription": True,
        "showReportLink": False,
        "showTopicsAndTags": False,
        "showDate": True,
        "showNumResults": True,
        "isTopicLevel": flag("is-topic-level"),
        "ignoredResourceGuids": d.get("ignored-resource-guids", ""),
        "includeDataReports": flag("include-data-reports"),
        "page": 1,
        "showRelatedtopics": False,
    }


def aihw(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """A data table workbook of the Australian Institute of Health and Welfare. `url` is the
    report's data page, whose file list the Institute's site fetches from its search API; the
    newest file whose title matches `resource_match` is downloaded, since a release can carry
    a new file name and address. The licence is the copyright page's own words."""
    s = _session(session)
    licence = statement_licence(ds, s)
    page = s.get(ds.source.url, timeout=120)
    page.raise_for_status()
    query = _aihw_listing(page.content)
    r = s.post(
        AIHW_LISTING,
        json=query,
        headers={"Accept": "application/json", "Referer": ds.source.url},
        timeout=120,
    )
    r.raise_for_status()
    results = r.json().get("results") or []
    rx = re.compile(ds.source.resource_match or ".")
    hits = [x for x in results if rx.search(x.get("resultTitle") or "")]
    if not hits:
        raise FetchError(
            f"{ds.slug}: no listed file matches '{ds.source.resource_match}' among "
            f"{[x.get('resultTitle') for x in results][:8]}"
        )
    hit = max(
        hits, key=lambda x: (x.get("resultDateTimeFormatted") or "", x.get("resultUrl") or "")
    )
    url = hit["resultUrl"]
    if url.startswith("/"):
        url = "https://www.aihw.gov.au" + url
    existing = store.manifests(store_dir, ds.slug)
    f = _download(ds, s, url)
    data = f.content
    expect_page(ds, url, f, data)
    digest = hashlib.sha256(data).hexdigest()
    if existing and existing[-1].sha256 == digest:
        return None, existing[-1], licence
    served = f.headers.get("Last-Modified", "")
    changed, why = "", FILE_NOTE
    if served:
        try:
            changed = email.utils.parsedate_to_datetime(served).astimezone(dt.UTC).isoformat()
            why = ""
        except UNREADABLE_DATE:
            why = UNREAD_DATE_NOTE
    if not changed:
        changed = dt.datetime.now(dt.UTC).isoformat()
    today = dt.datetime.now(TZ).date()
    version, note = free_version(_date_local(changed), {m.version for m in existing}, today)
    filename = url.split("?", 1)[0].rsplit("/", 1)[-1]
    filename = re.sub(r"\.aspx$", "", filename) or "source.xlsx"
    m = store.Manifest(
        dataset=ds.slug,
        version=version,
        as_at="",
        fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        sha256=digest,
        bytes=len(data),
        filename=filename,
        encoding=_encoding(filename, data, ds.source.encoding),
        source={
            "url": url,
            "page": ds.source.url,
            "resource_name": hit.get("resultTitle", ""),
            "catalogue_number": hit.get("catNum", ""),
            "listed_date": hit.get("resultDateTimeFormatted", ""),
            "etag": etag(f.headers),
            "http_last_modified": served,
        },
        licence=licence,
        notes=[n for n in (why, note) if n],
    )
    return data, m, licence


def zenodo(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """A file on Zenodo, followed across versions of one concept record. `url` is the records
    API, `package` the concept record id and `resource_match` the file name. The licence is the
    one the newest version's metadata states, and the version is dated by that record."""
    s = _session(session)
    got = catalogue.get_json(
        s,
        ds.source.url,
        {"q": f"conceptrecid:{ds.source.package}", "sort": "mostrecent", "size": 1},
        timeout=120,
    )
    hits = (got.get("hits") or {}).get("hits") or []
    if not hits:
        raise FetchError(f"{ds.slug}: no Zenodo record has concept id {ds.source.package}")
    rec = hits[0]
    meta = rec.get("metadata") or {}
    stated = str((meta.get("license") or {}).get("id") or "")
    licence = _licence(
        ds,
        stated,
        normalise_licence_id(stated),
        stated,
        meta.get("license", {}).get("url", ""),
        f"{ds.source.url}?q=conceptrecid:{ds.source.package}",
    )
    rx = re.compile(ds.source.resource_match or ".")
    files = [f for f in rec.get("files") or [] if rx.search(f.get("key") or "")]
    if len(files) != 1:
        raise FetchError(
            f"{ds.slug}: {len(files)} files match '{ds.source.resource_match}' in record {rec.get('id')}"
        )
    f = files[0]
    url = f["links"]["self"]
    existing = store.manifests(store_dir, ds.slug)
    r = _download(ds, s, url)
    data = r.content
    expect_page(ds, url, r, data)
    digest = hashlib.sha256(data).hexdigest()
    if existing and existing[-1].sha256 == digest:
        return None, existing[-1], licence
    changed = _changed(ds, rec.get("updated") or meta.get("publication_date"))
    today = dt.datetime.now(TZ).date()
    version, note = free_version(_date_local(changed), {m.version for m in existing}, today)
    m = store.Manifest(
        dataset=ds.slug,
        version=version,
        as_at=str(meta.get("publication_date") or ""),
        fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        sha256=digest,
        bytes=len(data),
        filename=f["key"],
        encoding=_encoding(f["key"], data, ds.source.encoding),
        source={
            "url": url,
            "record": rec.get("id"),
            "concept_record": ds.source.package,
            "record_url": (rec.get("links") or {}).get("self_html", ""),
            "doi": rec.get("doi", ""),
            "record_updated": rec.get("updated", ""),
            "resource_name": f["key"],
            "stated_checksum": f.get("checksum", ""),
        },
        licence=licence,
        notes=[note] if note else [],
    )
    return data, m, licence


STACK_NOTE = (
    "The publisher offers the series as one file per period. These bytes are every row of "
    "every file as one table, ordered by every column, with a row that appears in two "
    "files kept once; the manifest counts the files and the repeated rows."
)


def _stack_rows(
    data: bytes,
    filename: str,
    header_match: str,
    section_match: str = "",
    header_depth: int = 1,
    group_match: str = "",
    footnote_marks: bool = False,
) -> tuple[list[str], list[list[str]]]:
    """A workbook's or a CSV's header and rows as text, the header being the first row whose first
    cell is header_match, since the publisher's title rows above it vary. With section_match the
    file holds several small tables, each under a title that pattern matches, read as one row per
    cell. A header over header_depth rows names each column by its lowest filled cell. With
    group_match, a row with only its first cell filled names the group of the rows below it when
    the pattern matches, and is a footnote otherwise. With footnote_marks, a footnote number at the
    end of the first cell moves to a Note column."""
    from .normalise import _cell, _distinct

    if filename.lower().endswith(".xls"):
        from .normalise import xls_to_xlsx

        data, filename = xls_to_xlsx(data), filename + "x"
    if _is_xlsx(filename):
        import openpyxl

        wb = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True, keep_links=False
        )
        rows = wb.worksheets[0].iter_rows(values_only=True)
        close = wb.close
    elif filename.lower().endswith(".csv"):
        text = data.decode(detect_encoding(data))
        rows = iter(list(csv.reader(io.StringIO(text))))
        close = None
    else:
        raise FetchError(
            f"{filename}: a stack reads workbooks and CSV files, not {filename.rsplit('.', 1)[-1]}"
        )
    if section_match:
        out = _section_rows(rows, filename, header_match, section_match)
        if close:
            close()
        return ["Section", header_match, "Column", "Value"], out
    header: list[str] = []
    for r in rows:
        if r and _cell(r[0]).strip().lstrip("\ufeff") == header_match:
            header = [_cell(v).strip().lstrip("\ufeff") for v in r]
            for _ in range(header_depth - 1):
                below = [_cell(v).strip() for v in next(rows, ())]
                below += [""] * (len(header) - len(below))
                header = [b or h for h, b in itertools.zip_longest(header, below, fillvalue="")]
            header = _distinct(header)
            while header and not header[-1]:
                header.pop()
            break
    if not header:
        raise FetchError(f"{filename}: no row starts with '{header_match}'")
    out = []
    group = ""
    for r in rows:
        vals = [_cell(v).strip() for v in r[: len(header)]]
        if not any(vals):
            continue
        if group_match and not any(vals[1:]):
            if re.search(group_match, vals[0]):
                group = vals[0]
            continue
        vals += [""] * (len(header) - len(vals))
        if footnote_marks:
            mark = re.match(r"^(.*\S)\s+(\d{1,2})$", vals[0])
            vals = [mark[1], *vals[1:], mark[2]] if mark else [*vals, ""]
        out.append([group, *vals] if group_match else vals)
    if close:
        close()
    if footnote_marks:
        header = [*header, "Note"]
    return (["Group", *header] if group_match else header), out


def _section_rows(rows, filename: str, header_match: str, section_match: str) -> list[list[str]]:
    from .normalise import _cell

    rx = re.compile(section_match)
    out: list[list[str]] = []
    section, header = "", []
    for r in rows:
        vals = [_cell(v).strip().lstrip("\ufeff") for v in r]
        if not any(vals):
            continue
        hit = rx.search(vals[0])
        if hit and not any(vals[1:]):
            section = hit.groupdict().get("section") or hit.group(0)
            header = []
        elif vals[0] == header_match:
            if not section:
                raise FetchError(f"{filename}: a '{header_match}' header has no title above it")
            header = vals
        elif header and any(vals[1:]):
            # A row with its first cell alone is a footnote.
            out.extend(
                [section, vals[0], header[i], v]
                for i, v in enumerate(vals[1 : len(header)], 1)
                if v and header[i]
            )
    if not out:
        raise FetchError(f"{filename}: no table under a title matching '{section_match}'")
    return out


def _stack(
    ds: Dataset, s: requests.Session, files: list[dict]
) -> tuple[bytes, list[dict], int, int]:
    """Every file read into one table, its columns laid out as the first file has them, ordered by
    every column with a row two files share kept once. Each file is a dict with url, filename and
    whatever the manifest should say of it."""
    from .register import FILE_SOURCE

    named = any(x.source == FILE_SOURCE for x in ds.fields)
    header: list[str] = []
    rows: list[list[str]] = []
    read = []
    for f in files:
        got = _download(ds, s, f["url"])
        expect_page(ds, f["url"], got, got.content)
        h, body = _stack_rows(
            got.content,
            f["filename"],
            ds.source.header_match,
            ds.source.section_match,
            ds.source.header_depth,
            ds.source.group_match,
            ds.source.footnote_marks,
        )
        if named:
            label = f.get("name") or f["filename"]
            if ds.source.file_match:
                hit = re.search(ds.source.file_match, label)
                if not hit:
                    raise FetchError(f"{ds.slug}: '{label}' does not match the file_match pattern")
                label = hit[1] if hit.groups() else hit[0]
            h = [*h, FILE_SOURCE]
            body = [[*row, label] for row in body]
        if header and sorted(h) != sorted(header):
            raise FetchError(f"{ds.slug}: {f['filename']} has columns {h}, the first file {header}")
        header = header or h
        if h != header:
            at = [h.index(c) for c in header]
            body = [[row[i] for i in at] for row in body]
        rows.extend(body)
        read.append(
            {k: v for k, v in f.items() if k != "filename"}
            | {
                "sha256": hashlib.sha256(got.content).hexdigest(),
                "rows": len(body),
                "http_last_modified": got.headers.get("Last-Modified", ""),
            }
        )
    unique = sorted(set(map(tuple, rows)))
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    w.writerows(unique)
    return out.getvalue().encode("utf-8"), read, len(rows), len(rows) - len(unique)


def _stack_version(
    ds: Dataset,
    store_dir: Path,
    data: bytes,
    changed: str,
    source: dict,
    licence: dict,
    notes: tuple[str, ...] = (),
):
    digest = hashlib.sha256(data).hexdigest()
    existing = store.manifests(store_dir, ds.slug)
    if existing and existing[-1].sha256 == digest:
        return None, existing[-1], licence
    today = dt.datetime.now(TZ).date()
    version, note = free_version(_date_local(changed), {m.version for m in existing}, today)
    m = store.Manifest(
        dataset=ds.slug,
        version=version,
        as_at="",
        fetched_at=_now(),
        sha256=digest,
        bytes=len(data),
        filename=f"{ds.slug}.csv",
        encoding="utf-8",
        source=source,
        licence=licence,
        notes=[n for n in (*notes, note, STACK_NOTE) if n],
    )
    return data, m, licence


def ckan_stack(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """A series a CKAN portal holds as many workbooks across many packages, read whole into one
    table. `package` is the search text, `package_match` the packages to take by name,
    `resource_match` the resources by name (every workbook when blank), `header_match` the first
    cell of each workbook's header row. Every package must state the one licence."""
    s = _session(session)
    api = f"{ds.source.portal.rstrip('/')}/api/3/action"
    found = catalogue.get_json(
        s, f"{api}/package_search", {"q": ds.source.package, "rows": 1000}, timeout=120
    )
    if not found.get("success"):
        raise FetchError(f"{ds.slug}: package_search failed: {found.get('error')}")
    rx = re.compile(ds.source.package_match)
    packages = sorted(
        (p for p in found["result"]["results"] if rx.search(p.get("name", ""))),
        key=lambda p: p["name"],
    )
    if not packages:
        raise FetchError(f"{ds.slug}: no package matches '{ds.source.package_match}'")
    stated = {
        (p.get("license_id", ""), p.get("license_title", ""), p.get("license_url", ""))
        for p in packages
    }
    if len(stated) != 1:
        raise LicenceDrift(f"{ds.slug}: the packages state different licences: {sorted(stated)}")
    lic_id, lic_title, lic_url = next(iter(stated))
    licence = _licence(
        ds,
        lic_id,
        normalise_licence_id(lic_id, ds.source.portal),
        lic_title,
        lic_url,
        f"{api}/package_search?q={urllib.parse.quote(ds.source.package)}",
    )
    rrx = re.compile(ds.source.resource_match) if ds.source.resource_match else None
    resources = []
    for p in packages:
        for r in p.get("resources") or []:
            if rrx and not rrx.search((r.get("name") or "").strip()):
                continue
            name = resource_filename(r)
            if not (_is_workbook(name) or name.lower().endswith(".csv")):
                continue
            resources.append((p, r))
    if not resources:
        raise FetchError(f"{ds.slug}: the packages hold no workbooks to read")
    resources.sort(key=lambda pr: (pr[1].get("created") or "", pr[1]["id"]))
    files = [
        {
            "url": r["url"],
            "filename": resource_filename(r),
            "package": p["name"],
            "resource": r["id"],
            "name": r.get("name", ""),
        }
        for p, r in resources
    ]
    changed = max(
        _normal_iso(r.get("last_modified") or r.get("created") or p["metadata_modified"])
        for p, r in resources
    )
    if ds.source.manual and ds.slug not in MANUAL:
        existing = store.manifests(store_dir, ds.slug)
        was = existing[-1].source if existing else {}
        if was.get("newest_resource") == changed and sorted(
            w["resource"] for w in was.get("workbooks", [])
        ) == sorted(f["resource"] for f in files):
            return None, existing[-1], licence
        raise ManualDue(
            f"{ds.slug}: download {ds.source.url} (each of its {len(files)} files, into one folder) "
            "and run the fetch with --file"
        )
    data, read, n, repeated = _stack(ds, s, files)
    source = {
        "url": ds.source.url,
        "portal": ds.source.portal,
        "search": ds.source.package,
        "packages": len(packages),
        "workbooks": read,
        "rows_read": n,
        "rows_repeated": repeated,
        "newest_resource": changed,
    }
    return _stack_version(ds, store_dir, data, changed, source, licence)


LINK_RE = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["']""", re.I)


def page_links(page: bytes, base: str, pattern: str) -> list[str]:
    """Every link on a page whose address, decoded, matches the pattern: absolute, once each, in
    the order the page gives them."""
    rx = re.compile(pattern)
    out: list[str] = []
    for href in LINK_RE.findall(page.decode("utf-8", "replace")):
        url = urllib.parse.urljoin(base, html.unescape(href.strip()))
        if rx.search(urllib.parse.unquote(url)) and url not in out:
            out.append(url)
    return out


def file_stack(ds: Dataset, store_dir: Path, session: requests.Session | None = None):
    """A series a publisher lists on its own page as one file per period, read whole into one
    table. `url` is the page, `resource_match` the pattern each file's link matches, and
    `header_match` the first cell of each file's header row. The licence is the evidence page's
    own words, and the version is dated by the newest file's Last-Modified."""
    s = _session(session)
    licence = statement_licence(ds, s)
    page = _download(ds, s, ds.source.url)
    links = page_links(page.content, ds.source.url, ds.source.resource_match)
    if not links:
        raise FetchError(
            f"{ds.slug}: {ds.source.url} links to no file matching '{ds.source.resource_match}'"
        )
    files = [
        {"url": u, "filename": urllib.parse.unquote(u.split("?", 1)[0].rsplit("/", 1)[-1])}
        for u in links
    ]
    data, read, n, repeated = _stack(ds, s, files)
    changed = ""
    for f in read:
        try:
            when = email.utils.parsedate_to_datetime(f["http_last_modified"])
            changed = max(changed, when.astimezone(dt.UTC).isoformat())
        except UNREADABLE_DATE:
            pass
    source = {
        "url": ds.source.url,
        "files": read,
        "rows_read": n,
        "rows_repeated": repeated,
        "newest_file": changed or None,
    }
    # No file states a change date, so the version is dated by the fetch, and its notes say so.
    return _stack_version(
        ds, store_dir, data, changed or _now(), source, licence, () if changed else (FILE_NOTE,)
    )


ADAPTERS = {
    "ckan-stack": ckan_stack,
    "file-stack": file_stack,
    "file": http_file,
    "kiwis": kiwis,
    "aihw": aihw,
    "zenodo": zenodo,
    "ckan-resource": ckan_resource,
    "socrata": socrata_view,
    "opendatasoft": opendatasoft,
    "arcgis-hub": arcgis_hub,
    "arcgis-feature": arcgis_feature,
    "ala": ala,
}


class LicenceDrift(RuntimeError):
    pass


class FetchError(RuntimeError):
    """The portal answered, but not with the file."""


def expect_page(ds: Dataset, url: str, r, data: bytes) -> None:
    """An empty body or an HTML page where a data file should be is refused. Some portals answer
    requests from cloud addresses with an empty 200; that must never become a version."""
    ctype = r.headers.get("Content-Type", "") if r.headers else ""
    status = getattr(r, "status_code", "?")
    if not data:
        raise FetchError(
            f"{ds.slug}: {url} returned no bytes (HTTP {status}, {ctype or 'no content type'})"
        )
    name = url.split("?", 1)[0].lower()
    if "html" in ctype.lower() and not name.endswith((".html", ".htm")):
        raise FetchError(
            f"{ds.slug}: {url} returned an HTML page instead of the file (HTTP {status}): {data[:120]!r}"
        )


# What each portal's own licence codes mean, from the portal's license_list. A generic code such
# as cc-by names a different version on each portal, and on some none at all.
PORTAL_LICENCES = {
    "data.gov.au": {
        "CC-BY": "CC-BY-3.0-AU",
        "CC-BY-SA": "CC-BY-SA-3.0-AU",
        "CC-BY-2.5": "CC-BY-2.5-AU",
    },
    "catalogue.data.wa.gov.au": {"CC-BY": "CC-BY-4.0", "CC-BY-SA": "CC-BY-SA-4.0"},
    "data.wa.gov.au": {"CC-BY": "CC-BY-4.0", "CC-BY-SA": "CC-BY-SA-4.0"},
    "data.sa.gov.au": {"CC-BY": "CC-BY-4.0", "CC-BY-SA": "CC-BY-SA-4.0"},
    "discover.data.vic.gov.au": {"CC-BY": "CC-BY-4.0", "CC-BY-SA": "CC-BY-SA-4.0"},
    "www.data.qld.gov.au": {
        "CC-BY": "CC-BY-3.0-AU",
        "CC-BY-SA": "CC-BY-SA-3.0-AU",
        "CC-BY-3.0": "CC-BY-3.0-AU",
        "CC-BY-SA-3.0": "CC-BY-SA-3.0-AU",
        "CC-BY-4": "CC-BY-4.0",
        "CC-BY-SA-4": "CC-BY-SA-4.0",
    },
    "data.nsw.gov.au": {"CC-AT-4": "CC-BY-4.0", "CC-AT-3": "CC-BY-3.0-AU"},
}
# A code that names no version anywhere it appears unqualified. On a portal the map above does not
# settle, it is refused, and the entry names the version from the dataset page with portal_id.
UNVERSIONED = {"CC-BY", "CC-BY-SA", "CC-BY-ND", "CC-BY-NC"}
EXACT = {"CC-ZERO": "CC0-1.0", "CC0": "CC0-1.0"}


def portal_host(portal: str) -> str:
    return urllib.parse.urlsplit(portal if "//" in portal else f"https://{portal}").netloc.lower()


def portal_defines(portal_id: str, portal: str) -> str:
    """The register id a portal's own license_list gives this code, or "" when the map is silent."""
    s = (portal_id or "").strip().upper().replace("_", "-")
    return PORTAL_LICENCES.get(portal_host(portal), {}).get(s, "") if portal else ""


def normalise_licence_id(portal_id: str, portal: str = "") -> str:
    """A portal's licence code onto the register's ids, read as that portal's license_list defines
    it. A generic code the portal does not version gives "", which no register id matches."""
    s = (portal_id or "").strip().upper().replace("_", "-")
    if s in EXACT:
        return EXACT[s]
    host = portal_host(portal) if portal else ""
    known = PORTAL_LICENCES.get(host, {})
    if s in known:
        return known[s]
    return "" if s in UNVERSIONED else s


def _normal_iso(iso: str) -> str:
    """Portal timestamps carry no zone and are UTC."""
    x = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (x if x.tzinfo else x.replace(tzinfo=dt.UTC)).astimezone(dt.UTC).isoformat()


def _is_xlsx(filename: str) -> bool:
    return filename.lower().endswith((".xlsx", ".xlsm"))


def _is_workbook(filename: str) -> bool:
    return _is_xlsx(filename) or filename.lower().endswith(".xls")


def _encoding(filename: str, data: bytes, preferred: str) -> str:
    """xlsx and zip name their container; a text file names its character set."""
    if _is_xlsx(filename):
        return "xlsx"
    if filename.lower().endswith(".xls"):
        return "xls"
    if filename.lower().endswith(".zip"):
        return "zip"
    return detect_encoding(data, preferred)


def check_licence(ds: Dataset, licence: dict) -> None:
    """Runs on every fetch, changed bytes or not: a licence the portal now states differently
    from the register stops the run before anything is written. A register entry may name the
    portal's exact licence id when the portal's id is ambiguous and the evidence page settles it."""
    if ds.licence.portal_id:
        defined = portal_defines(ds.licence.portal_id, getattr(ds.source, "portal", ""))
        if defined and defined != ds.licence.id:
            raise LicenceDrift(
                f"{ds.slug}: {portal_host(ds.source.portal)} defines '{ds.licence.portal_id}' as "
                f"{defined}, but the register says {ds.licence.id}"
            )
        if licence.get("id", "") != ds.licence.portal_id:
            raise LicenceDrift(
                f"{ds.slug}: portal states licence '{licence.get('id')}' ({licence.get('title')}) "
                f"but the register expects '{ds.licence.portal_id}'; review before publishing"
            )
        return
    # An adapter that worked the id out from the portal's words has it in final form already.
    found = (
        licence["normalised"].upper()
        if "normalised" in licence
        else normalise_licence_id(licence.get("id", ""), ds.source.portal)
    )
    if not found:
        raise LicenceDrift(
            f"{ds.slug}: the portal states '{licence.get('id')}' ({licence.get('title')}), which names "
            "no licence version there; read the version on the dataset page and set licence.portal_id"
        )
    if found != ds.licence.id.upper():
        raise LicenceDrift(
            f"{ds.slug}: portal states licence '{licence.get('id')}' ({licence.get('title')}) "
            f"but the register says {ds.licence.id}; review before publishing"
        )


def fetch(ds: Dataset, store_dir: Path) -> store.Manifest | None:
    """Fetch one dataset into the store. Returns the new manifest, or None if unchanged."""
    if ds.source.adapter not in ADAPTERS:
        raise RuntimeError(f"{ds.slug}: no adapter '{ds.source.adapter}'")
    data, m, licence = ADAPTERS[ds.source.adapter](ds, store_dir)
    # Every version records where and when its licence was read, so the record can be audited.
    for k in ("read_from", "read_at"):
        if not licence.get(k):
            raise FetchError(f"{ds.slug}: the {ds.source.adapter} adapter did not record {k}")
    check_licence(ds, licence)
    if data is None:
        return None
    m.rows_sha256, n, misfit = _rows(ds, m, data)
    from .serialise.profile import layout

    m.parquet = layout(ds)
    existing = store.manifests(store_dir, ds.slug)
    if m.rows_sha256 and existing and existing[-1].rows_sha256 == m.rows_sha256:
        return None
    # Portals sometimes serve an export with its header and nothing else for a while.
    if n == 0 and existing:
        raise FetchError(f"{ds.slug}: the portal served no rows; the newest version has some")
    if misfit:
        # Held here, so one release that outgrows a declared INT32 field never stops a deploy.
        raise FetchError(
            f"{ds.slug}: {'; '.join(misfit)}; take the field out of int32 before this version "
            "is stored"
        )
    if ds.source.feed:
        m = feed_version(m, store.manifests(store_dir, ds.slug), dt.datetime.now(TZ).date())
        if m is None:
            return None
    store.write(store_dir, m, data)
    return m


def rows_digest(ds: Dataset, m: store.Manifest, data: bytes) -> str:
    """SHA-256 of the normalised rows taken in sorted order, or "" when the file does not
    normalise here; the build then reports why. Some portals re-sort an export on every reload."""
    return _rows(ds, m, data)[0]


def _rows(ds: Dataset, m: store.Manifest, data: bytes) -> tuple[str, int | None, list[str]]:
    """The rows digest, the row count, which is None when the file does not normalise here, and
    the declared INT32 fields the rows do not fit."""
    from .normalise import normalise
    from .serialise.profile import misfits

    if ds.kind != "table":
        return "", None, []
    try:
        tbl = normalise(ds, m, data)
    except Exception:  # noqa: BLE001 - any failure falls back to the byte comparison
        return "", None, []
    cols = [tbl.table.column(n).to_pylist() for n in tbl.table.column_names]
    if tbl.geometry is not None:
        cols.append(tbl.geometry.to_pylist())
    rows = sorted(hashlib.sha256(repr(r).encode()).digest() for r in zip(*cols, strict=True))
    h = hashlib.sha256(repr(tbl.table.schema).encode())
    for r in rows:
        h.update(r)
    return h.hexdigest(), len(rows), misfits(tbl.table, ds.int32)


FEED_NOTE = (
    "The publisher serves this as a live feed of what is current, with no change date, so this "
    "version is what the feed held when it was fetched, dated by the fetch."
)


def feed_version(m: store.Manifest, existing: list[store.Manifest], today: dt.date):
    """A feed's version is the day it was fetched. A second change on a day that already has a
    version waits for the next day's fetch, so a day is never split or rewritten."""
    if any(x.version == today.isoformat() for x in existing):
        return None
    m.version = today.isoformat()
    m.notes = [FEED_NOTE]
    return m
