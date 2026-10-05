"""The catalogue: every dataset record on Australia's government open-data portals.

Each portal's own metadata is read and re-keyed onto one record shape. Nothing is scored or
merged. A record data.gov.au copied from a state portal read here is dropped so the original is
the only copy. The snapshot is stored like any source: one file per harvest date, a new version
only when the bytes change.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import html
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from . import store

UA = "publicdata.au catalogue (+https://publicdata.au/about/)"
SLUG = "catalogue"
FILENAME = "catalogue.ndjson.gz"


@dataclass(frozen=True)
class Portal:
    code: str
    host: str
    api: str
    jurisdiction: str
    kind: str = "ckan"
    # A portal run by one body names it here, and every record it lists is that body's.
    publisher: str = ""
    # Organisations on other portals, as portal:org, that copy this one. Their records are
    # dropped whenever this portal has records of its own in the snapshot.
    replaces: tuple[str, ...] = ()

    def landing(self, name: str) -> str:
        if self.kind == "ods":
            return f"https://{self.host}/explore/dataset/{name}/"
        if self.kind == "hub":
            return f"https://{self.host}/datasets/{name}"
        return f"{self.api.removesuffix('/api/3/action')}/dataset/{name}"


def _council(code, host, jur, kind, publisher, replaces=()):
    return Portal(code, host, f"https://{host}", jur, kind, publisher, tuple(replaces))


PORTALS = (
    Portal("gov", "data.gov.au", "https://data.gov.au/data/api/3/action", "cth"),
    Portal("nsw", "data.nsw.gov.au", "https://data.nsw.gov.au/data/api/3/action", "nsw"),
    Portal(
        "vic", "discover.data.vic.gov.au", "https://discover.data.vic.gov.au/api/3/action", "vic"
    ),
    Portal("qld", "www.data.qld.gov.au", "https://www.data.qld.gov.au/api/3/action", "qld"),
    Portal("sa", "data.sa.gov.au", "https://data.sa.gov.au/data/api/3/action", "sa"),
    Portal("wa", "catalogue.data.wa.gov.au", "https://catalogue.data.wa.gov.au/api/3/action", "wa"),
    Portal("nt", "data.nt.gov.au", "https://data.nt.gov.au/api/3/action", "nt"),
    Portal(
        "infra",
        "catalogue.data.infrastructure.gov.au",
        "https://catalogue.data.infrastructure.gov.au/api/3/action",
        "cth",
    ),
    Portal(
        "act", "www.data.act.gov.au", "https://api.us.socrata.com/api/catalog/v1", "act", "socrata"
    ),
    Portal("abs", "data.api.abs.gov.au", "https://data.api.abs.gov.au/rest", "cth", "sdmx"),
    # Councils with a portal of their own. One that data.gov.au or its state portal already lists
    # in full is left to that copy.
    _council("bne", "data.brisbane.qld.gov.au", "qld", "ods", "Brisbane City Council", ["qld:brisbane-city-council"]),
    _council("melb", "data.melbourne.vic.gov.au", "vic", "ods", "City of Melbourne", ["vic:city-of-melbourne", "gov:city-of-melbourne-open-data"]),
    _council("casey", "data.casey.vic.gov.au", "vic", "ods", "City of Casey", ["vic:city-of-casey", "gov:city-of-casey"]),
    _council("ballarat", "data.ballarat.vic.gov.au", "vic", "ods", "City of Ballarat", ["vic:city-of-ballarat", "gov:city-of-ballarat"]),
    _council("geelong", "www.geelongdataexchange.com.au", "vic", "ods", "City of Greater Geelong", ["vic:city-of-greater-geelong", "gov:city-of-greater-geelong"]),
    _council("corangamite", "data.corangamite.vic.gov.au", "vic", "ods", "Corangamite Shire Council", ["gov:corangamite-shire-council"]),
    _council("hawkesbury", "data.hawkesbury.nsw.gov.au", "nsw", "ods", "Hawkesbury City Council"),
    _council("maitland", "data.maitland.nsw.gov.au", "nsw", "ods", "Maitland City Council"),
    _council("lakemac", "data.lakemac.com.au", "nsw", "ods", "Lake Macquarie City Council", ["gov:lake-macquarie-city-council", "nsw:lakemac"]),
    _council("camden", "data.camden.nsw.gov.au", "nsw", "ods", "Camden Council"),
    _council("liverpool", "data.liverpool.nsw.gov.au", "nsw", "ods", "Liverpool City Council"),
    _council("bmcc", "data.bmcc.nsw.gov.au", "nsw", "ods", "Blue Mountains City Council"),
    _council("cumberland", "data.cumberland.nsw.gov.au", "nsw", "ods", "Cumberland City Council"),
    _council("wpc", "data.wpcouncils.nsw.gov.au", "nsw", "ods", "Western Parkland Councils"),
    _council("campbelltown", "data.campbelltown.nsw.gov.au", "nsw", "ods", "Campbelltown City Council"),
    _council("fairfield", "data.fairfieldcity.nsw.gov.au", "nsw", "ods", "Fairfield City Council"),
    _council("bayside", "nsw-bayside.opendatasoft.com", "nsw", "ods", "Bayside Council"),
    _council("wollondilly", "data.wollondilly.nsw.gov.au", "nsw", "ods", "Wollondilly Shire Council"),
    _council("darwin", "darwin.opendatasoft.com", "nt", "ods", "City of Darwin", ["nt:darwin-city-council"]),
    _council("sydney", "data.cityofsydney.nsw.gov.au", "nsw", "hub", "City of Sydney"),
    _council("goldcoast", "data-goldcoast.opendata.arcgis.com", "qld", "hub", "City of Gold Coast", ["gov:city-of-gold-coast"]),
    _council("sunshine", "data.sunshinecoast.qld.gov.au", "qld", "hub", "Sunshine Coast Council"),
    _council("townsville", "data-tsvcitycouncil.opendata.arcgis.com", "qld", "hub", "Townsville City Council", ["gov:townsville-city-council"]),
    _council("tweed", "data-tweed.opendata.arcgis.com", "nsw", "hub", "Tweed Shire Council"),
    _council("wodonga", "cow-open-data-hub-cityofwodonga.hub.arcgis.com", "vic", "hub", "City of Wodonga"),
    _council("albany", "city-maps-and-data-albanywa.hub.arcgis.com", "wa", "hub", "City of Albany"),
    _council("parramatta", "open-data-parracity.hub.arcgis.com", "nsw", "hub", "City of Parramatta", ["gov:city-of-parramatta"]),
    _council("perth", "geohub-perth.opendata.arcgis.com", "wa", "hub", "City of Perth"),
    _council("latrobe", "geo-latrobecc.hub.arcgis.com", "vic", "hub", "Latrobe City Council"),
)  # fmt: skip
BY_CODE = {p.code: p for p in PORTALS}
# Hosts data.gov.au harvests from that are read directly here, so its copies are duplicates.
DIRECT_HOSTS = {p.host.removeprefix("www.") for p in PORTALS}

# A resource in one of these can be read and serialised without a browser.
DOWNLOADABLE = {
    "CSV", "TSV", "XLSX", "XLS", "ODS", "JSON", "GEOJSON", "NDJSON", "XML", "PARQUET", "ZIP",
    "SHP", "GPKG", "KML", "KMZ", "GDB", "FGDB", "TAB", "MIF", "SQLITE", "TXT", "API", "SDMX",
}  # fmt: skip
FORMAT_ALIASES = {
    "EXCEL": "XLSX", "MS EXCEL": "XLSX", "EXCEL (.XLSX)": "XLSX", "XLSM": "XLSX",
    "ESRI SHAPEFILE": "SHP", "SHAPEFILE": "SHP", "SHZ": "SHP", "GEO JSON": "GEOJSON",
    "ESRI REST": "API", "REST": "API", "ARCGIS REST": "API", "WFS": "API", "ESRI FILE GEODATABASE": "FGDB",
}  # fmt: skip

# Requested by their Solr names: the harvest origin must be asked for as
# extras_original_harvest_source and comes back keyed original_harvest_source; the type is
# dataset_type. CKAN drops unknown names without an error, so the harvest test pins these.
CKAN_FIELDS = (
    "id,name,title,organization,license_id,metadata_created,metadata_modified,"
    "res_format,dataset_type,notes,extras_original_harvest_source,url"
)
SUMMARY_CHARS = 280


RETRYABLE = (requests.ConnectionError, requests.Timeout)


def _get(s: requests.Session, url: str, params: dict | None = None, **kw) -> requests.Response:
    timeout = kw.pop("timeout", 180)
    for attempt in range(5):
        try:
            r = s.get(url, params=params, timeout=timeout, **kw)
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
                return r
        except RETRYABLE:
            if attempt == 4:
                raise
        time.sleep(5 * (attempt + 1))
    r.raise_for_status()
    return r


class PortalError(RuntimeError):
    pass


def get_json(s: requests.Session, url: str, params: dict | None = None, **kw):
    r = _get(s, url, params, **kw)
    try:
        return r.json()
    except ValueError:
        raise PortalError(
            f"{url}: HTTP {r.status_code}, {r.headers.get('Content-Type') or 'no content type'}, "
            f"{len(r.content)} bytes, not JSON: {r.content[:120]!r}"
        ) from None


# CKAN's default licence register, by title. Checked before any pattern so a title such as
# "Other (Not Open)" cannot match on its words.
KNOWN_LICENCES = {
    "other (open)": "other-open",
    "other (not open)": "other-closed",
    "other (public domain)": "PDM",
    "other (attribution)": "other-at",
    "other (non-commercial)": "other-nc",
    "open data commons attribution license": "ODC-BY-1.0",
    "open data commons open database license (odbl)": "ODBL-1.0",
    "open data commons public domain dedication and license (pddl)": "PDDL-1.0",
    "license not specified": "",
    "licence not specified": "",
    "notspecified": "",
}
OPEN_IDS = {"CC0-1.0", "PDM", "PDDL-1.0", "ODC-BY-1.0", "ODBL-1.0", "other-open", "other-at"}


def licence_id(title: str) -> str:
    """A portal's licence title onto an SPDX-style id. A title nothing here recognises is kept as
    the portal states it, so it shows up in review rather than being guessed."""
    t = re.sub(r"\s+", " ", (title or "").strip()).lower()
    if not t or t in KNOWN_LICENCES:
        return KNOWN_LICENCES.get(t, "")
    if re.search(r"\bnot open\b|\bclosed\b", t):
        return (title or "").strip()
    if re.search(r"\bodbl\b|odc-odbl|open database licen[cs]e", t):
        return "ODBL-1.0"
    if re.search(r"\bodc-by\b|open data commons attribution", t):
        return "ODC-BY-1.0"
    if re.search(r"\bpddl\b", t):
        return "PDDL-1.0"
    if "zero" in t or t in ("cc0", "cc-zero", "cc0-1.0"):
        return "CC0-1.0"
    if "public domain" in t or t == "pdm":
        return "PDM"
    if re.search(r"creative commons|\bcc\b|^cc-", t):
        parts = ["CC-BY"]
        if re.search(r"non-?commercial|\bnc\b", t):
            parts.append("NC")
        if re.search(r"no[ -]?deriv|\bnd\b", t):
            parts.append("ND")
        if re.search(r"share[ -]?alike|\bsa\b", t):
            parts.append("SA")
        ver = re.search(r"(\d\.\d)", t)
        v = ver.group(1) if ver else ""
        au = "australia" in t or t.endswith("-au") or " au" in t
        if v:
            parts.append(v)
            if au and v in ("2.5", "3.0"):
                parts.append("AU")
        return "-".join(parts)
    return (title or "").strip()


def cc_url(url: str) -> str:
    """The licence a creativecommons.org deed URL names, such as CC-BY-3.0-AU."""
    m = re.search(r"creativecommons\.org/licenses/([a-z-]+)/(\d\.\d)(/au)?", url.lower())
    if not m:
        return ""
    au = "-AU" if m[3] and m[2] in ("2.5", "3.0") else ""
    return f"CC-{m[1].upper()}-{m[2]}{au}"


def is_open(lic: str) -> bool | None:
    """True for licences that allow republication with changes, False for ones that do not,
    None when the portal states no licence."""
    if not lic:
        return None
    if lic in OPEN_IDS:
        return True
    if lic.startswith("CC-BY"):
        return "-NC" not in lic and "-ND" not in lic
    return False


def formats(raw) -> list[str]:
    out = set()
    for f in raw or []:
        for part in re.split(r"[,/;]", str(f)):
            p = part.strip().upper().lstrip(".")
            if p:
                out.add(FORMAT_ALIASES.get(p, p))
    return sorted(out)


def plain_text(notes: str) -> str:
    """A portal's HTML or Markdown description as one line of plain text."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", notes or ""))
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[*_#`>|]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def summary(notes: str) -> str:
    t = plain_text(notes)
    if len(t) <= SUMMARY_CHARS:
        return t
    cut = t[:SUMMARY_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(",;:.") + "…"


def _day(iso: str) -> str:
    return (iso or "")[:10]


def record_id(portal: str, source_id: str) -> str:
    """Stable across renames: CKAN's package UUID, Socrata's four-by-four, the ABS dataflow id.
    Shaped to fit a vote key."""
    return re.sub(r"[^a-z0-9-]+", "-", f"{portal}-{source_id}".lower()).strip("-")[:64]


def _record(portal: Portal, **kw) -> dict:
    fmts = kw.pop("formats")
    lic = kw.pop("licence")
    rec = {
        "id": record_id(portal.code, kw.pop("source_id")),
        "portal": portal.code,
        "licence": lic,
        "open": is_open(lic),
        "formats": fmts,
        "downloadable": bool(set(fmts) & DOWNLOADABLE),
        "source_host": kw.pop("source_host", ""),
        **kw,
    }
    return dict(sorted(rec.items()))


def ckan(portal: Portal, s: requests.Session, log=print) -> tuple[list[dict], int]:
    lic_titles = {
        x["id"]: x.get("title") or x["id"]
        for x in get_json(s, f"{portal.api}/license_list")["result"]
    }
    orgs, off = {}, 0
    while True:
        page = get_json(
            s, f"{portal.api}/organization_list", {"all_fields": "true", "limit": 25, "offset": off}
        )["result"]
        new = [o for o in page if o["name"] not in orgs]
        if not new:
            break
        orgs.update({o["name"]: (o.get("title") or o["name"]).strip() for o in new})
        off += len(page)
    out, dropped, start, rows = [], 0, 0, 1000
    while True:
        res = get_json(
            s,
            f"{portal.api}/package_search",
            {
                "rows": rows,
                "start": start,
                "sort": "id asc",
                "fl": CKAN_FIELDS,
                "include_private": "false",
            },
        )["result"]
        batch = res["results"]
        for p in batch:
            harvest = p.get("original_harvest_source") or p.get("extras_original_harvest_source")
            if isinstance(harvest, str):
                try:
                    harvest = json.loads(harvest)
                except ValueError:
                    harvest = {"site_url": harvest}
            origin = urlparse((harvest or {}).get("site_url") or "").hostname or ""
            host = origin.removeprefix("www.")
            # data.gov.au names itself as the origin of records it holds first hand.
            if (
                portal.code == "gov"
                and host in DIRECT_HOSTS
                and host != portal.host.removeprefix("www.")
            ):
                dropped += 1
                continue
            lic_raw = p.get("license_id") or ""
            org = p.get("organization") or ""
            if isinstance(org, dict):
                org = org.get("name") or ""
            out.append(
                _record(
                    portal,
                    source_id=p["id"],
                    name=p.get("name") or "",
                    title=re.sub(r"\s+", " ", p.get("title") or p.get("name") or "").strip(),
                    org=org,
                    org_title=orgs.get(org, org),
                    kind=p.get("dataset_type") or p.get("type") or "dataset",
                    licence=licence_id(lic_titles.get(lic_raw, lic_raw)),
                    licence_title=lic_titles.get(lic_raw, lic_raw),
                    formats=formats(p.get("res_format")),
                    created=_day(p.get("metadata_created")),
                    modified=_day(p.get("metadata_modified")),
                    url=portal.landing(p.get("name") or p["id"]),
                    summary=summary(p.get("notes") or ""),
                    harvested_from=origin if origin and origin != portal.host else "",
                    source_host=(urlparse(p.get("url") or "").hostname or "").removeprefix("www."),
                )
            )
        start += len(batch)
        log(f"catalogue {portal.code}: {start}/{res['count']}")
        if not batch or start >= res["count"]:
            break
    return out, dropped


def socrata(portal: Portal, s: requests.Session, log=print) -> tuple[list[dict], int]:
    out, off, after = [], 0, ""
    while True:
        # scroll_id pages in id order, starting from an empty one, and stays stable while the
        # catalogue changes. Offsets over the default relevance order repeat and skip rows.
        params = {"domains": portal.host, "limit": 100, "scroll_id": after}
        res = get_json(s, portal.api, params)
        for r in res["results"]:
            x, meta = r["resource"], r.get("metadata") or {}
            kind = x.get("type") or ""
            org = (x.get("attribution") or "").strip()
            lic_title = meta.get("license") or ""
            out.append(
                _record(
                    portal,
                    source_id=x["id"],
                    name=x["id"],
                    title=re.sub(r"\s+", " ", x.get("name") or "").strip(),
                    org=re.sub(r"[^a-z0-9]+", "-", org.lower()).strip("-"),
                    org_title=org,
                    kind=kind,
                    licence=licence_id(lic_title),
                    licence_title=lic_title,
                    formats=["API", "CSV", "JSON"] if kind == "dataset" else [],
                    created=_day(x.get("createdAt")),
                    modified=_day(x.get("data_updated_at") or x.get("updatedAt")),
                    url=r.get("permalink") or r.get("link") or "",
                    summary=summary(x.get("description") or ""),
                    harvested_from="",
                )
            )
        off += len(res["results"])
        log(f"catalogue {portal.code}: {off}/{res['resultSetSize']}")
        if not res["results"]:
            break
        after = res["results"][-1]["resource"]["id"]
    return out, 0


def sdmx(portal: Portal, s: requests.Session, log=print) -> tuple[list[dict], int]:
    """ABS dataflows. The ABS states CC BY 4.0 for its statistics unless a release says otherwise."""
    flows = get_json(
        s,
        f"{portal.api}/dataflow/ABS",
        {"detail": "allstubs"},
        headers={"Accept": "application/vnd.sdmx.structure+json"},
    )["data"]["dataflows"]
    out = []
    for f in flows:
        fid, ver = f["id"], f.get("version") or "1.0"
        out.append(
            _record(
                portal,
                source_id=fid,
                name=fid,
                title=re.sub(r"\s+", " ", f.get("name") or fid).strip(),
                org="abs",
                org_title="Australian Bureau of Statistics",
                kind="dataflow",
                licence="CC-BY-4.0",
                licence_title="Creative Commons Attribution 4.0 International",
                formats=["API", "CSV", "SDMX"],
                created="",
                modified="",
                url=f"https://dataexplorer.abs.gov.au/vis?df[ds]=ABS_ABS_TOPICS&df[id]={quote(fid)}&df[ag]=ABS&df[vs]={quote(ver)}",
                summary=summary(f.get("description") or ""),
                harvested_from="",
            )
        )
    log(f"catalogue {portal.code}: {len(out)} dataflows")
    return out, 0


def _council_record(portal: Portal, **kw) -> dict:
    return _record(
        portal,
        org=portal.code,
        org_title=portal.publisher,
        kind="dataset",
        created=kw.pop("created", ""),
        harvested_from="",
        **kw,
    )


def ods(portal: Portal, s: requests.Session, log=print) -> tuple[list[dict], int]:
    """An Opendatasoft portal. A dataset with records can be exported in every format the
    platform offers; one without is a page of links, which is listed with no files."""
    out, off, total = [], 0, 0
    while True:
        res = get_json(
            s, f"{portal.api}/api/explore/v2.1/catalog/datasets", {"limit": 100, "offset": off}
        )
        total = res["total_count"]
        if total > 10_000:
            raise PortalError(f"{portal.host}: {total} datasets is past the paging limit")
        for x in res["results"]:
            # A dataset federated from another portal carries that portal's name after an @.
            if "@" in x["dataset_id"]:
                continue
            m = (x.get("metas") or {}).get("default") or {}
            lic = m.get("license") or ""
            lid = cc_url(m.get("license_url") or "") or licence_id(lic)
            fmts = []
            if x.get("has_records"):
                fmts = ["API", "CSV", "JSON", "XLSX"]
                if "geo" in (x.get("features") or []):
                    fmts += ["GEOJSON", "SHP", "KML"]
            out.append(
                _council_record(
                    portal,
                    source_id=x.get("dataset_uid") or x["dataset_id"],
                    name=x["dataset_id"],
                    title=re.sub(r"\s+", " ", m.get("title") or x["dataset_id"]).strip(),
                    licence=lid,
                    licence_title=lic,
                    formats=formats(fmts),
                    modified=_day(m.get("modified") or ""),
                    url=portal.landing(x["dataset_id"]),
                    summary=summary(m.get("description") or ""),
                )
            )
        off += len(res["results"])
        log(f"catalogue {portal.code}: {off}/{total}")
        if not res["results"] or off >= total:
            break
    return out, 0


# What each ArcGIS item type can be read as. A feature layer is served by its REST API and Hub
# exports it on request.
HUB_FORMATS = {
    "Feature Service": ["API", "CSV", "GEOJSON", "SHP"],
    "Map Service": ["API"],
    "Image Service": ["API"],
    "Vector Tile Service": ["API"],
    "WFS": ["API"],
    "WMS": ["WMS"],
    "CSV": ["CSV"],
    "CSV Collection": ["CSV"],
    "Microsoft Excel": ["XLSX"],
    "Shapefile": ["SHP"],
    "File Geodatabase": ["FGDB"],
    "GeoJson": ["GEOJSON"],
    "KML": ["KML"],
    "KML Collection": ["KML"],
    "GeoPackage": ["GPKG"],
}


def _epoch_day(ms) -> str:
    s = str(ms if ms is not None else "").strip()
    if not s.isdigit():
        return ""
    try:
        return (dt.date(1970, 1, 1) + dt.timedelta(milliseconds=int(s))).isoformat()
    except OverflowError:
        return ""


def hub_licence(x: dict) -> tuple[str, str]:
    """Hub states a licence id, or "custom" or "none" with the terms, often a link to a Creative
    Commons deed, written out in licenseInfo."""
    lic = (x.get("license") or "").strip()
    raw = x.get("licenseInfo") or ""
    text = summary(raw)
    if lic.lower() not in ("", "none", "custom"):
        return licence_id(lic), text or lic
    found = cc_url(raw)
    if found or lic.lower() in ("", "none"):
        return found, text
    named = licence_id(text)
    return (named if named != text else "custom"), text


def hub(portal: Portal, s: requests.Session, log=print) -> tuple[list[dict], int]:
    """An ArcGIS Hub site, read through its OGC Records search of the site's own catalogue."""
    out, n = [], 0
    url, params = f"{portal.api}/api/search/v1/collections/dataset/items", {"limit": 100}
    while True:
        res = get_json(s, url, params)
        feats = res.get("features") or []
        for f in feats:
            x = f.get("properties") or {}
            lic, lic_title = hub_licence(x)
            kind = x.get("type") or ""
            out.append(
                _council_record(
                    portal,
                    source_id=x["id"],
                    name=x["id"],
                    title=re.sub(r"\s+", " ", x.get("title") or x["id"]).strip(),
                    licence=lic,
                    licence_title=lic_title,
                    formats=formats(HUB_FORMATS.get(kind, [kind] if kind else [])),
                    created=_epoch_day(x.get("created")),
                    modified=_epoch_day(x.get("modified")),
                    url=portal.landing(x["id"]),
                    summary=summary(x.get("description") or x.get("snippet") or ""),
                )
            )
        n += len(feats)
        log(f"catalogue {portal.code}: {n}/{res.get('numberMatched')}")
        nxt = next((x.get("href") for x in res.get("links") or [] if x.get("rel") == "next"), "")
        if not feats or not nxt:
            break
        url, params = nxt, None
    return out, 0


HARVESTERS = {"ckan": ckan, "socrata": socrata, "sdmx": sdmx, "ods": ods, "hub": hub}


def harvest(
    portals=PORTALS, log=print, previous: list[dict] | None = None, previous_version: str = ""
) -> tuple[list[dict], dict]:
    """A portal that cannot be read keeps its records from the previous snapshot and says so in
    the stats, so an outage never reads as datasets withdrawn. With no previous snapshot its
    records are absent and the stats say it was not read."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    records, stats = [], {}
    for p in portals:
        try:
            recs, dropped = HARVESTERS[p.kind](p, s, log)
        except (requests.RequestException, PortalError, KeyError, ValueError, TypeError) as e:
            if previous is None and previous_version:
                raise PortalError(
                    f"{p.host} could not be read and the {previous_version} snapshot is not in the "
                    "store to carry its records forward; run `publicdata store pull --only catalogue`"
                ) from e
            kept = [r for r in previous or [] if r["portal"] == p.code]
            records += kept
            stats[p.code] = {
                "records": len(kept),
                "error": str(e)[:300],
                "carried_from": previous_version if kept else None,
            }
            log(
                f"catalogue: {p.host} could not be read; {len(kept)} records kept from {previous_version or 'no earlier snapshot'}: {e}"
            )
            continue
        records += recs
        stats[p.code] = {"records": len(recs), "dropped_duplicates": dropped}
    records = _drop_copies(records, portals, stats)
    seen, unique = set(), []
    for r in sorted(records, key=lambda r: r["id"]):
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)
    return unique, stats


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def _drop_copies(records: list[dict], portals, stats: dict) -> list[dict]:
    """A record in an organisation a council portal replaces is dropped when the council portal
    lists the same title or the record's source is on the council portal. Anything else that
    organisation holds is kept, since it may come from somewhere the council portal does not."""
    have = {r["portal"] for r in records}
    scope = {o: p for p in portals if p.code in have for o in p.replaces}
    if not scope:
        return records
    titles: dict[str, set[str]] = {}
    for r in records:
        if r["portal"] in {p.code for p in scope.values()}:
            titles.setdefault(r["portal"], set()).add(_title_key(r["title"]))
    kept = []
    for r in records:
        p = scope.get(f"{r['portal']}:{r['org'] or 'unknown'}")
        if p and (
            _title_key(r["title"]) in titles.get(p.code, ())
            or r.get("source_host") == p.host.removeprefix("www.")
        ):
            st = stats.setdefault(r["portal"], {})
            st["dropped_duplicates"] = st.get("dropped_duplicates", 0) + 1
            continue
        kept.append(r)
    return kept


def encode(records: list[dict]) -> bytes:
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    return gzip.compress(body.encode("utf-8"), compresslevel=9, mtime=0)


def decode(data: bytes) -> list[dict]:
    return [json.loads(line) for line in gzip.decompress(data).decode("utf-8").splitlines() if line]


def latest(store_dir: Path) -> store.Manifest | None:
    ms = store.manifests(store_dir, SLUG)
    return ms[-1] if ms else None


def load(store_dir: Path) -> list[dict]:
    m = latest(store_dir)
    if not m:
        return []
    store.verify(store_dir, m)
    return decode(store.source_path(store_dir, m).read_bytes())


def fetch(
    store_dir: Path, log=print, portals=PORTALS, today: str | None = None
) -> store.Manifest | None:
    prev = latest(store_dir)
    previous = load(store_dir) if prev and store.source_path(store_dir, prev).exists() else None
    records, stats = harvest(portals, log, previous, prev.version if prev else "")
    data = encode(records)
    digest = hashlib.sha256(data).hexdigest()
    if prev and prev.sha256 == digest:
        log("catalogue: unchanged")
        return None
    now = dt.datetime.now(dt.UTC)
    version = today or now.date().isoformat()
    if version in {x.version for x in store.manifests(store_dir, SLUG)}:
        # A version is never rewritten. A second harvest on the same day waits for a later date.
        log(f"catalogue: a different snapshot for {version} is already stored; not overwriting")
        return None
    m = store.Manifest(
        dataset=SLUG,
        version=version,
        as_at=version,
        fetched_at=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        sha256=digest,
        bytes=len(data),
        filename=FILENAME,
        encoding="utf-8",
        source={"portals": {p.code: p.host for p in portals}, "stats": stats},
        licence={"id": "various", "note": "Each record carries the licence its portal states."},
    )
    store.write(store_dir, m, data)
    log(f"catalogue: new version {version} ({len(records)} records)")
    return m
