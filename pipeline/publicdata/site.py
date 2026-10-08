"""Render the HTML site, its Markdown twins and the discovery files from the build output."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import html
import json
import re
import shutil
import urllib.parse
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from . import OPERATOR, REPO, SITE, brand, explorer, figures
from . import api_text as at
from . import ard as ardspec
from .build import DatasetOut, VersionOut, dataset_url, version_url
from .cache import BuildCache
from .cost import fleet_from_build
from .d1 import KEEP, queryable
from .provenance import (
    CITE_REQUEST,
    NOT_ENDORSED,
    OPERATOR_ORG,
    OPERATOR_URL,
    attribution,
    cite,
    landing,
    long_date,
)
from .records import connect
from .register import NEWEST, WHERE_OPS, Dataset
from .serialise import (
    FORMAT_LABEL,
    FORMATS,
    MEDIA,
    SHAPE_FORMATS,
    formats_for,
    pretty,
    profile,
    reasons,
)
from .serialise.geo import geo_kind
from .spine import ATTRIBUTION as SPINE_ATTRIBUTION
from .spine import DATUM as SPINE_DATUM
from .spine import LAYERS as SPINE_LAYERS
from .topics import TOPICS

HOST = SITE.replace("https://", "")
# One entity for the site, which the home page describes and every dataset is included in.
CATALOG_ID = f"{SITE}/#catalog"
CATALOG = {"@type": "DataCatalog", "@id": CATALOG_ID, "name": HOST, "url": f"{SITE}/catalog.json"}
# The GitHub mark (Octicons, MIT), drawn in the text colour so it follows the theme.
GH_MARK = '<svg viewBox="0 0 16 16" width="18" height="18" aria-hidden="true" focusable="false"><path fill="currentColor" d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>'
# The code is its own entity; the repository identifies it, not the catalogue.
SOURCE_JSONLD = {
    "@context": "https://schema.org",
    "@type": "SoftwareSourceCode",
    "@id": f"{SITE}/#source",
    "name": f"{HOST} source code",
    "description": "The pipeline that fetches, versions and serialises the datasets, and the site, query API and MCP server built from them.",
    "codeRepository": REPO,
    "url": REPO,
    "license": "https://www.gnu.org/licenses/agpl-3.0.html",
    "programmingLanguage": ["Python", "JavaScript"],
    "author": OPERATOR_ORG,
    "maintainer": OPERATOR_ORG,
}
GITHUB_ORG = REPO.rsplit("/", 1)[0]
# Cloudflare Web Analytics: first-party, no cookies, no other tracking.
BEACON = "b3b3d9ce88104e7e965284919b4d556e"
# The query API over D1 is documented only once it answers; flip with the D1 binding.
QUERY_API = True
# Chrome origin-trial token for WebMCP on https://publicdata.au, expires 17 Nov 2026.
ORIGIN_TRIAL = "AjNME/wcGH0gN0bKPlUW/RnNBsnvgDJqUAJBSXr/NmNtgDFPw2YA2JMAr4bemG54zRprx27PTtKf7HWYxD79Ag4AAABNeyJvcmlnaW4iOiJodHRwczovL3B1YmxpY2RhdGEuYXU6NDQzIiwiZmVhdHVyZSI6IldlYk1DUCIsImV4cGlyeSI6MTc5NDg3MzYwMH0="
JUR_LONG = {
    "Cth": "Commonwealth",
    "NSW": "New South Wales",
    "Vic": "Victoria",
    "Qld": "Queensland",
    "WA": "Western Australia",
    "SA": "South Australia",
    "Tas": "Tasmania",
    "ACT": "Australian Capital Territory",
    "NT": "Northern Territory",
    "Local": "Local government",
}
STATUS_LABEL = {"blocked": "blocked by licence", "assessing": "licence under review"}
# The home page map: the datasets drawn, each with the condition that keeps the rows drawn (or
# None for every row) and the states its rows cover ("all", or None for the publisher's own).
# A state no dataset covers is drawn hatched, so it reads as unpublished rather than empty.
HERO = {
    "datasets": {"au-eucalypt-records": {"where": None, "states": "all"}},
    "lede": "{total} eucalypt records from the herbaria and flora atlases of every state and territory, since 1770.",
    "what": "eucalypt records",
    "why": "no records published",
}
# The dataset the home page's query and agent examples answer from, when it is served.
SHOWCASE = "au-road-deaths"
FORMAT_NOTES = {
    "parquet": "Columnar and typed. The smallest download and the fastest to query. Reads directly in DuckDB, pandas, Polars, Arrow and Spark. The provenance header is in the file metadata.",
    "json": 'One object with a <span class="mono">publicdata</span> header (publisher, licence, attribution, version, source hash), a <span class="mono">fields</span> list and a <span class="mono">records</span> array of {rows} objects with typed values. Blank cells are <span class="mono">null</span>.',
    "csv": "UTF-8 without a byte-order mark, RFC 4180 quoting, one header row using the normalised field names. Opens in Excel, Numbers and Sheets.",
    "sqlite": 'One database with a <span class="mono">records</span> table, a <span class="mono">fields</span> table and a <span class="mono">publicdata</span> metadata table. Open it with any SQLite client.',
    "duckdb": 'One DuckDB database with a <span class="mono">records</span> table of typed columns, a <span class="mono">fields</span> table and a <span class="mono">publicdata</span> metadata table. It attaches read-only over HTTPS, so DuckDB, Python and R can query it without downloading it.',
    "ndjson": "The first line is the provenance header, then one JSON object per line. For streaming and for tools that read line by line.",
    "geojson": "A FeatureCollection with one Point per row and every field as a property. Rows without coordinates have a null geometry. Ready for QGIS, Leaflet, MapLibre and geopandas.",
    "xlsx": 'One workbook with a <span class="mono">records</span> sheet of {rows} typed rows, a <span class="mono">fields</span> sheet and a <span class="mono">publicdata</span> sheet with the provenance. Dates are real Excel dates. Opens in Excel, Numbers, LibreOffice and Sheets.',
    "gpkg": 'A GeoPackage with one point layer, <span class="mono">records</span>, in the publisher\'s coordinate system, plus the <span class="mono">fields</span> and <span class="mono">publicdata</span> tables. Drag it into QGIS or ArcGIS.',
    "geo.parquet": 'GeoParquet: the Parquet table with a WKB <span class="mono">geometry</span> column and its coordinate system in the file\'s geo metadata. Reads directly in DuckDB, GeoPandas, QGIS and GDAL.',
    "pmtiles": "Vector tiles in one PMTiles file, zoom 0 to {maxzoom}, every field a property. MapLibre and Leaflet read it over HTTPS with range requests, so a map needs no tile server.",
    "arrow": "Arrow IPC file (Feather v2), zstd compressed, with the provenance in the schema metadata. Memory-maps in pyarrow, Polars, Arrow JS and R without parsing.",
    "csv.gz": "The same CSV, gzipped, at about a tenth of the size. curl, pandas, DuckDB and R read it directly.",
    "partition": 'One JSON file per value of <span class="mono">{field}</span>, {count} files. The example is <span class="mono">{example}</span>. Every file carries the same header and a <span class="mono">partition</span> note.',
}


def download_name(slug: str, version: str, rel: str) -> str:
    """The name a version's file saves under: data.csv of qld-x 2026-04-24 is
    qld-x_2026-04-24.csv. functions/_download.js does the same."""
    tail = rel[4:] if rel.startswith("data.") else "_" + rel.replace("/", "_")
    return f"{slug}_{version}{tail}"


def fmt_size(n: int | None) -> str:
    if n is None:
        return ""
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            v = n / div
            return f"{v:.1f} {unit}" if v < 10 else f"{v:.0f} {unit}"
    return f"{n} B"


def fmt_int(n: int) -> str:
    return f"{n:,}"


URL_RE = re.compile(r"https://[^\s<>\"]+")


def linkify(text: str) -> str:
    """Escape the prose and link each bare https URL. Trailing punctuation stays outside the link."""
    out, pos = [], 0
    text = str(text)
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:)")
        out.append(html.escape(text[pos : m.start()]))
        out.append(f'<a href="{html.escape(url)}">{html.escape(url)}</a>')
        out.append(html.escape(m.group(0)[len(url) :]))
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def env() -> Environment:
    e = Environment(
        loader=PackageLoader("publicdata", "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    e.filters["linkify"] = linkify
    e.globals["cadence_words"] = cadence_words
    e.globals["download_name"] = download_name
    return e


def _write(out: Path, rel: str, text: str) -> None:
    p = out / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _change_words(c: dict | None) -> str:
    """A diff without a key, or a database's, compares row counts and has no added or removed."""
    if not c:
        return ""
    if "added" not in c:
        return f" ({c['rows_from']} rows before)"
    return f" ({c['added']} added, {c['removed']} removed, {c['changed']} changed)"


def _version_view(ds: Dataset, v, change: dict | None) -> dict:
    m = v.manifest
    return {
        "version": m.version,
        "as_at": m.as_at,
        "as_at_long": long_date(m.as_at) if m.as_at else "",
        "rows": v.rows,
        "rows_fmt": fmt_int(v.rows),
        "fields": ds.field_count,
        "tables": v.tables,
        "encoding": m.encoding,
        "sha256": m.sha256,
        "backfilled": m.backfilled,
        "notes": m.notes,
        "change": change,
        "fetched_long": long_date(m.fetched_at),
        "ext": m.ext,
        "source_url": m.source.get("url", ""),
        "portal_licence": (m.licence or {}).get("title") or (m.licence or {}).get("id", ""),
    }


def collection_url(collection: str) -> str:
    return f"{SITE}/c/{collection}/"


def _years(ds: Dataset, m, span: str = "") -> str:
    """'2001 to 2026' when both ends are the publisher's own, else the full years the chart
    draws from the rows, else 'since 1960' when only the start is known, else the as-at year."""
    start = ds.temporal_start[:4] if ds.temporal_start else ""
    end = (m.as_at or "")[:4]
    if start and end and start != end:
        return f"{start} to {end}"
    if span:
        # The chart may draw fewer rows than the table holds, so a stated start still leads.
        first, _, last = span.partition(" to ")
        if start and start < first:
            return f"{start} to {last or first}"
        return span
    if start and not end:
        return f"since {start}"
    return end


# Most used first: the office formats, then the analysts' files, then the mapping ones.
SITE_ORDER = (
    "csv",
    "xlsx",
    "json",
    "geojson",
    "parquet",
    "sqlite",
    "duckdb",
    "gpkg",
    "geo.parquet",
    "pmtiles",
    "ndjson",
    "arrow",
    "csv.gz",
)


def _file_formats_text() -> str:
    """Which formats a version has, from api.json, with the limits filled in from CAPS."""
    from .serialise import CAPS, EXCEL_MAX_ROWS, JSON_MAX_ROWS

    mb = {f"{f}_mb": CAPS[f][1] // 1_000_000 for f in CAPS}
    return at.fill(
        at.spec()["site"]["file_formats"],
        {**mb, "excel_rows": EXCEL_MAX_ROWS, "json_rows": JSON_MAX_ROWS},
    )


def _fmts(ds: Dataset, v: VersionOut) -> list[str]:
    have = set(formats_for(v.rows, geo_kind(ds), v.left_out))
    return [f for f in SITE_ORDER if f in have]


def _left_out(ds: Dataset, v: VersionOut) -> list[str]:
    """Why each format a table version lacks is not there, one sentence each, in site order."""
    if ds.kind == "database":
        return []
    gone = reasons(v.rows, geo_kind(ds), v.left_out)
    return [gone[f] for f in SITE_ORDER if f in gone]


def _format_names(ds: Dataset, v: VersionOut) -> list[str]:
    return [FORMAT_LABEL[f] for f in _fmts(ds, v) if f != "csv.gz"]


def _short_title(ds: Dataset) -> str:
    """The title without its trailing place or years, for a question: "Births, deaths and
    marriages registered by calendar year" from the title that ends ", Victoria"."""
    head, _, tail = ds.title.rpartition(", ")
    return head if head and len(tail.split()) <= 4 else ds.title


def cadence_words(ds: Dataset) -> tuple[str, str]:
    """The register's cadence as the page's "Updated" value and as a sentence, so a closed or
    irregular series reads as words ("releases it irregular" did not)."""
    c, pub = ds.source.cadence, ds.publisher.short
    rest = c.removeprefix("historical, ")
    if rest in ("closed", "closed year", "no longer updated", "not updated"):
        return "no longer", f"{pub} no longer updates it."
    if rest.startswith("not updated since "):
        since = rest.removeprefix("not updated ")
        return f"not {since}", f"{pub} has not updated it {since}."
    if rest.startswith("no new "):
        return rest, f"{pub} has published {rest}."
    if c in ("live", "continual"):
        return "continuously", f"{pub} updates it continuously."
    if c.startswith("irregular"):
        c = "irregularly" + c.removeprefix("irregular")
    if c.startswith("one file"):
        return c, f"{pub} releases {c}."
    return c, f"{pub} releases it {c}."


def _seo_title(ds: Dataset, v: VersionOut, span: str = "") -> str:
    m = v.manifest
    years = _years(ds, m, span)
    have = set(_fmts(ds, v))
    # At most four names keep the title inside what a result page shows; the description lists them all.
    names = ["CSV", *(["JSON"] if "json" in have else []), "Parquet"]
    last = "GeoJSON" if "geojson" in have else "SQLite" if "sqlite" in have else "DuckDB"
    fmts = ", ".join([*names, last])
    lead = ds.search_title or ds.title
    return f"{lead}{f' {years}' if years else ''}: {fmts} download | {HOST}"


def _seo_description(ds: Dataset, v: VersionOut, span: str = "") -> str:
    m = v.manifest
    years = _years(ds, m, span)
    summary = ds.summary.rstrip(".")
    parts = [
        f"{summary}.",
        f"{fmt_int(v.rows)} rows from {ds.publisher.name} under {ds.licence.title}"
        + (f", {years}." if years else "."),
        f"Download as {', '.join(_format_names(ds, v))}"
        + (f", or one file per {ds.partition_by[0].replace('_', ' ')}." if ds.partition_by else ".")
        + " No login, no key.",
    ]
    return " ".join(parts)


def _faq(ds: Dataset, v: VersionOut, partitions: dict, span: str = "") -> list[tuple[str, str]]:
    m = v.manifest
    base = dataset_url(ds.slug)
    vbase = version_url(ds.slug, m.version)
    fmts = _format_names(ds, v)
    short = _short_title(ds)
    out = [
        (
            f"How do I download {short} as a CSV file?",
            f"Open {base}latest/data.csv. It redirects to the newest dated version, which is {vbase}data.csv today. "
            f"The same path serves {', '.join(f for f in fmts if f != 'CSV')}. A dated URL never changes, so use it when the file must stay the same.",
        )
    ]
    why = reasons(v.rows, geo_kind(ds), v.left_out).get("xlsx")
    if not why:
        excel = f"Yes. {vbase}data.xlsx is a workbook with the {fmt_int(v.rows)} rows on a records sheet, the field list on a second sheet and the provenance on a third. The CSV also opens in Excel."
    else:
        excel = f"Not as a workbook. {why} Load the CSV or the Parquet file with Power Query, or take one partition file at a time."
    out.append(
        (
            f"Can I open {short} in Excel?",
            excel + f" {vbase}data.csv.gz is the CSV at about a tenth of the size.",
        )
    )
    years = _years(ds, m, span)
    if years:
        out.append(
            (
                f"What years does {short} cover?",
                f"The current version covers {'the years ' if years.startswith('since') else ''}{years}"
                + (f", as at {long_date(m.as_at)}" if m.as_at else "")
                + f". Each release from {ds.publisher.short} becomes a new dated version here, and earlier versions stay online.",
            )
        )
    if ds.source.cadence:
        out.append(
            (
                f"How often is {short} updated?",
                f"{cadence_words(ds)[1]} This site checks the portal every week and adds a dated version when the file changes.",
            )
        )
    lic = ds.licence
    if lic.condition:
        use = f"Yes, with one condition. {lic.condition} The attribution string is in this page's side column and inside every file."
    elif lic.id.startswith("CC0"):
        use = f"Yes. {lic.title} places it in the public domain, so no attribution is required, although the publisher still appreciates one."
    elif "SA" in lic.id:
        use = f"Yes. {lic.title} allows commercial use as long as the attribution is kept and anything you build from it carries the same licence."
    else:
        use = f"Yes. {lic.title} allows commercial use, redistribution and derived works as long as the attribution is kept. The attribution string is in this page's side column and inside every file."
    out.append((f"Can I use {short} commercially?", use))
    out.append(
        (
            f"Is this the official source for {short}?",
            f"No. The publisher is {ds.publisher.name}, and its page is {landing(ds)}. "
            + (
                f"This site republishes the publisher's file with one change. {ds.source_withheld}"
                if ds.source_withheld
                else "This site republishes the publisher's file without changing its content. "
                f"The original sits beside every version as source.{m.ext} with its SHA-256, so the two can be compared."
            ),
        )
    )
    if ds.partition_by:
        pf = ds.partition_by[0]
        entries = partitions.get(pf, [])
        ex = max(entries, key=lambda x: x["rows"]) if entries else None
        out.append(
            (
                f"How do I get only the rows for one {pf.replace('_', ' ')}?",
                f"Every version has one JSON file per value of {pf}, {len(entries)} files in the current version, listed with row counts at {vbase}by/{pf}/index.json."
                + (
                    f" For example {vbase}{ex['json']} holds the {fmt_int(ex['rows'])} rows where {pf} is {ex['value']}."
                    if ex
                    else ""
                )
                + (" A GeoJSON file sits beside each one." if geo_kind(ds) == "point" else ""),
            )
        )
    if ds.geometry and ds.geometry.get("crs_note"):
        out.append(
            (
                f"What coordinate system does {short} use?",
                ds.geometry["crs_note"]
                + (
                    " The GeoJSON file uses the same coordinates, which is what GeoJSON expects."
                    if geo_kind(ds) == "point"
                    else " The GeoJSON file uses GDA2020 as well, which differs from the WGS84 GeoJSON expects by well under a metre."
                ),
            )
        )
    if ds.enrich:
        from .spine import ATTRIBUTION, LAYERS

        layers = [LAYERS[k] for k in ds.enrich]
        names = [x.name[1] for x in layers]
        what = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]
        nouns = [x.noun for x in layers]
        asked = ", ".join(nouns[:-1]) + f" and {nouns[-1]}" if len(nouns) > 1 else nouns[0]
        out.append(
            (
                f"Which {asked} is each row of {short} in?",
                f"Each row with coordinates carries the {what} its point falls in, with the code of each. "
                "This site joins them by location against the ABS boundaries, "
                + ", ".join(f"{dataset_url(x.slug)}" for x in layers)
                + f", and {ds.publisher.short} did not publish them. The schema marks each one as joined and names the boundary version. "
                "A row without coordinates, or a point outside every area, has them blank. "
                + ATTRIBUTION,
            )
        )
    return out


def _temporal(ds: Dataset, m) -> str | None:
    """Only what the publisher states: a start from the register and the publisher's as-at."""
    if ds.temporal_start and m.as_at:
        return f"{ds.temporal_start}/{m.as_at}"
    return None


def copies_of(record: dict | None, slug: str) -> list[dict]:
    """Where a dataset is also published, in a fixed order, from the record the Hubs job
    commits in store/hubs.json. Only copies the job found are listed."""
    found = ((record or {}).get("datasets") or {}).get(slug) or {}
    out = []
    if found.get("huggingface"):
        repo = found["huggingface"].split("/datasets/", 1)[-1]
        out.append(
            {"hub": "Hugging Face", "url": found["huggingface"], "note": f'load_dataset("{repo}")'}
        )
    if found.get("kaggle"):
        out.append({"hub": "Kaggle", "url": found["kaggle"], "note": "with a starter notebook"})
    if found.get("zenodo"):
        doi = found["zenodo"]
        out.append(
            {"hub": "Zenodo", "url": f"https://doi.org/{doi}", "note": f"DOI {doi}", "doi": doi}
        )
    return out


def _dataset_jsonld(ds: Dataset, o: DatasetOut, copies: list[dict] | None = None) -> dict:
    copies = copies or []
    doi = next((c["doi"] for c in copies if c.get("doi")), None)
    v = o.latest
    m = v.manifest
    base = dataset_url(ds.slug)
    vbase = version_url(ds.slug, m.version)
    dists = []
    for name, fmt in _files_of(ds, v):
        dists.append(
            {
                "@type": "DataDownload",
                "encodingFormat": MEDIA[fmt],
                "contentUrl": f"{vbase}{name}",
                "contentSize": str(v.files.get(name, "")),
            }
        )
    return {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "@id": base,
        "url": base,
        "name": ds.title,
        "description": ds.summary or ds.description,
        "identifier": [
            ds.slug,
            {
                "@type": "PropertyValue",
                "propertyID": "DOI",
                "value": doi,
                "url": f"https://doi.org/{doi}",
            },
        ]
        if doi
        else ds.slug,
        "alternateName": list(ds.also_known_as) or None,
        "version": m.version,
        "datePublished": o.versions[0].manifest.version,
        "dateModified": m.version,
        "temporalCoverage": _temporal(ds, m),
        "spatialCoverage": {"@type": "Place", "name": JUR_LONG.get(ds.publisher.jurisdiction)},
        "citation": cite(ds, m, vbase)["text"],
        "sdPublisher": OPERATOR_ORG,
        "isPartOf": (collection_url(ds.collection) if ds.collection else None),
        "variableMeasured": [
            {"@type": "PropertyValue", "name": name, "description": f.description or f.source}
            for name, f in _all_fields(ds)
        ],
        "license": ds.licence.url,
        "usageInfo": ds.licence.condition or None,
        "isAccessibleForFree": True,
        "creator": {
            "@type": "Organization",
            "name": ds.publisher.name,
            "url": ds.publisher.url,
        },
        "publisher": {
            "@type": "Organization",
            "name": ds.publisher.name,
            "url": ds.publisher.url,
        },
        "provider": OPERATOR_ORG,
        "isBasedOn": m.source.get("url"),
        # The publisher's page, then each copy this site's Hubs job made, so search engines can
        # tell the copies are one dataset.
        "sameAs": [ds.source.url, *(c["url"] for c in copies)] if copies else ds.source.url,
        "keywords": [
            JUR_LONG.get(ds.publisher.jurisdiction, ds.publisher.jurisdiction),
            ds.publisher.short,
            *ds.keywords,
            *([ds.collection_title] if ds.collection_title else []),
        ],
        "distribution": dists,
        "includedInDataCatalog": CATALOG,
        "conditionsOfAccess": NOT_ENDORSED,
    }


def _breadcrumbs(items: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": n, "item": u}
            for i, (n, u) in enumerate(items)
        ],
    }


def _faq_jsonld(faq: list[tuple[str, str]]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in faq
        ],
    }


def _dcat_dataset(ds: Dataset, o: DatasetOut) -> dict:
    v = o.latest
    m = v.manifest
    base = dataset_url(ds.slug)
    vbase = version_url(ds.slug, m.version)
    return {
        "@type": "dcat:Dataset",
        "@id": base,
        "identifier": ds.slug,
        "title": ds.title,
        "description": ds.summary or ds.description,
        "landingPage": base,
        "license": ds.licence.url,
        "publisher": {
            "@type": "foaf:Organization",
            "name": ds.publisher.name,
            "homepage": ds.publisher.url,
        },
        "modified": m.version,
        "accrualPeriodicity": ds.source.cadence or None,
        "spatial": JUR_LONG.get(ds.publisher.jurisdiction),
        "temporal": _temporal(ds, m),
        "source": m.source.get("url"),
        "versionInfo": m.version,
        "keyword": [*ds.keywords, *ds.also_known_as] or None,
        "publicdata:attribution": attribution(ds, m),
        "publicdata:cite": cite(ds, m, vbase)["text"],
        "publicdata:notEndorsed": True,
        "publicdata:versions": f"{base}versions.json",
        "publicdata:datapackage": f"{base}datapackage.json",
        "publicdata:licenceCondition": ds.licence.condition or None,
        "publicdata:kind": ds.kind,
        "publicdata:topics": list(ds.topics),
        "publicdata:jurisdiction": ds.publisher.jurisdiction,
        "distribution": [
            {
                "@type": "dcat:Distribution",
                "format": fmt,
                "mediaType": MEDIA[fmt],
                "accessURL": f"{base}latest/{name}",
                "downloadURL": f"{vbase}{name}",
                "byteSize": v.files.get(name),
                "conformsTo": f"{base}schema.json",
                **({"title": name.split("/")[-1].rsplit(".", 1)[0]} if "/" in name else {}),
            }
            for name, fmt in _files_of(ds, v)
        ],
    }


def _files_of(ds: Dataset, v: VersionOut) -> list[tuple[str, str]]:
    """(file, format) for every data file of a version: the whole-table formats, or a database's
    DuckDB file and the Parquet file of each table."""
    if ds.kind == "database":
        return [
            ("data.duckdb", "duckdb"),
            *((f"tables/{t.name}.parquet", "parquet") for t in ds.tables),
        ]
    # Agents and catalogues take the first file as the main one, and for them that is Parquet.
    return [(f"data.{fmt}", fmt) for fmt in sorted(_fmts(ds, v), key=lambda f: f != "parquet")]


def _all_fields(ds: Dataset) -> list[tuple[str, object]]:
    """(name, field) for every published field; a database's are named table.field."""
    if ds.kind == "database":
        return [(f"{t.name}.{f.name}", f) for t in ds.tables for f in t.fields]
    return [(f.name, f) for f in ds.fields]


def _row(ds: Dataset, o: DatasetOut | None, fig: dict | None = None) -> dict:
    latest = o.latest if o and o.versions else None
    d = {
        "slug": ds.slug,
        "title": ds.title,
        "summary": ds.summary,
        "fields": ds.field_count,
        "kind": ds.kind,
        "spark": (fig or {}).get("spark", ""),
        "spark_caption": (fig or {}).get("spark_caption", ""),
        "publisher": ds.publisher.short,
        "publisher_name": ds.publisher.name,
        "jur": ds.publisher.jurisdiction,
        "status": ds.status,
        "status_label": STATUS_LABEL.get(ds.status, ds.status),
        "licence": ds.licence.title,
        "licence_url": ds.licence.url,
        "planned": ds.planned,
        "blocked_reason": ds.blocked_reason,
        "live": bool(latest),
        "latest": latest.manifest.version if latest else "",
        "rows": fmt_int(latest.rows) if latest else "",
        "done_label": "live" if latest else "soon",
        "collection": ds.collection,
        "collection_title": ds.collection_title,
        "url": dataset_url(ds.slug) if latest else "",
        "source_url": ds.source.url,
        "topics": [TOPICS[t]["name"].lower() for t in ds.topics if t in TOPICS],
    }
    if latest:
        vb = version_url(ds.slug, latest.manifest.version)
        d["quick"] = [
            {"name": f, "url": f"{vb}data.{f}"}
            for f in ("parquet", "json", "csv", "xlsx", "sqlite", "duckdb", "geojson")
            if f"data.{f}" in latest.files
        ]
    return d


# A polygon or line layer's GeoJSON and GeoPackage carry the shapes themselves, moved to GDA2020.
SHAPE_NOTES = {
    "parquet": 'GeoParquet: every field and the {kind} itself as a WKB <span class="mono">geometry</span> column in GDA2020, with the coordinate system in the file\'s geo metadata. Reads directly in DuckDB, GeoPandas, QGIS and GDAL.',
    "geojson": "A FeatureCollection with one feature per row, its {kind} in GDA2020, and every field as a property. Ready for QGIS, Leaflet, MapLibre and geopandas.",
    "gpkg": 'A GeoPackage with one {kind} layer, <span class="mono">records</span>, in GDA2020 (EPSG:7844), plus the <span class="mono">fields</span> and <span class="mono">publicdata</span> tables. Drag it into QGIS or ArcGIS.',
}


def _format_note(ds: Dataset, key: str, rows: int) -> str:
    kind = geo_kind(ds)
    note = (
        SHAPE_NOTES[key]
        if kind in ("polygon", "line") and key in SHAPE_NOTES
        else FORMAT_NOTES[key]
    )
    return note.format(
        rows=fmt_int(rows),
        kind=kind,
        maxzoom=(ds.geometry or {}).get("maxzoom", 10),
    )


# The largest workbook picked first: past this, a spreadsheet takes minutes to open.
XLSX_FIRST_MAX = 50_000_000


def _default_format(ds: Dataset, latest: VersionOut) -> str:
    """The format a visitor gets without choosing: Excel when the workbook is a size an office
    machine opens, else CSV. Parquet stays a click away for the people who know it."""
    have = set(_fmts(ds, latest))
    if "xlsx" in have and 0 < latest.files.get("data.xlsx", 0) <= XLSX_FIRST_MAX:
        return "xlsx"
    return "csv" if "csv" in have else next(iter(_fmts(ds, latest)))


def _picker(ds: Dataset, latest: VersionOut) -> tuple[list[dict], dict]:
    """The format buttons and what site.js needs for each: the file and its note. The default
    format comes first and is the one pressed."""
    first = _default_format(ds, latest)
    order = sorted(_fmts(ds, latest), key=lambda f: f != first)
    formats = [{"key": f, "label": FORMAT_LABEL[f], "file": f"data.{f}"} for f in order]
    for f in formats:
        f["size"] = fmt_size(latest.files.get(f["file"]))
    fmt_data = {
        f["key"]: {
            "file": f["file"],
            "note": _format_note(ds, f["key"], latest.rows),
        }
        for f in formats
    }
    if ds.partition_by:
        pf = ds.partition_by[0]
        entries = latest.partitions.get(pf, [])
        ex = max(entries, key=lambda x: x["rows"]) if entries else None
        if ex:
            formats.append(
                {
                    "key": "partition",
                    "label": f"One {pf.replace('_', ' ')}",
                    "size": f"{len(entries)} files",
                    "file": ex["json"],
                }
            )
            fmt_data["partition"] = {
                "file": ex["json"],
                "note": FORMAT_NOTES["partition"].format(
                    field=pf, count=len(entries), example=ex["json"]
                ),
            }
    return formats, fmt_data


# Rows shown on a dataset page and a place page as a first look at the table.
SAMPLE_ROWS = 10


def _sample(ds: Dataset, db: Path, within: dict | None = None) -> dict:
    """The page's sample rows, with a heading and a note that say how they were picked: the
    register's sample, else the newest rows first with the partition field's values taking
    turns, so a table filed oldest first or one sex after the other shows more than one corner."""
    spec = ds.sample or {}
    yf = figures.year_field(ds)
    order = spec.get("order") or (((yf[0], True),) if yf else ())
    spread = spec.get("spread")
    if spread is None:
        skip = {yf[0] if yf else "", ds.place_field if within else ""}
        spread = next((f for f in ds.partition_by if f not in skip), "")
    where = [*([within] if within else []), *spec.get("where", ())]
    out = figures.sample_rows(
        db,
        [f.name for f in ds.fields],
        SAMPLE_ROWS,
        where=where,
        nulls=True,
        order=order,
        spread=spread,
    )
    # The records hold a boolean as 1 or 0; the table shows it as the JSON does.
    flags = [i for i, f in enumerate(out["fields"]) if ds.field(f).type == "boolean"]
    for r in out["rows"]:
        for i in flags:
            r[i] = {"1": "true", "0": "false"}.get(r[i], r[i])
    n = len(out["rows"])

    def low(name: str) -> str:
        w = ds.field(name).display
        return w if w[1:2].isupper() else w[:1].lower() + w[1:]

    # A version written under a sort: has its rows, and so its sample, in that order.
    sorted_by = [
        f
        for f in (profile.signature(db) if db.exists() else "").split(",")
        if f and f not in ds.key
    ]
    source_order = (
        "sorted by " + ", then ".join(low(f) for f in sorted_by)
        if sorted_by
        else "in the publisher's order"
    )
    if spec.get("label"):
        how = spec["label"]
    elif order or spread:
        how = ", ".join(
            [
                "From the latest version",
                *([f"newest first by {low(order[0][0])}"] if order else []),
                *([f"each {low(spread)} in turn"] if spread else []),
                f"then {source_order}, with every field.",
            ]
        )
    else:
        how = f"From the latest version, {source_order}, with every field."
    plain = not (order or spread or spec.get("where") or spec.get("label"))
    rows = "row" if n == 1 else "rows"
    out["heading"] = f"The first {n} {rows}" if plain else f"A sample of {n} {rows}"
    out["note"] = f"{how} A blank cell is shown as null."
    return out


# Words that name a place, a time or the shape of a table, which say nothing of the subject.
RELATED_STOP = set(
    """a an and as at by data dataset each for from in into its of on or per the to with every
    all latest year years yearly month monthly daily weekly quarter quarterly annual since onwards
    australia australian national state states territory nsw new south wales qld queensland vic
    victoria victorian sa south wa western tas tasmania tasmanian act capital nt northern
    government council councils area areas region regions number numbers list register
    records table""".split()
)
# A page lists at most this many, so the list stays one a reader scans.
RELATED_MAX = 12


def _subject_words(d: Dataset) -> set[str]:
    text = " ".join([d.title, *d.keywords, *d.also_known_as]).lower()
    words = {w.removesuffix("s") for w in re.findall(r"[a-z][a-z']+", text)}
    return {w for w in words if len(w) > 2 and w not in RELATED_STOP}


def _related(ds: Dataset, live: list[DatasetOut]) -> list[dict]:
    """Datasets under the same topic from another publisher that share words of subject with
    this one, most shared first, so a reader who has one state's table finds the others without
    going back to the topic page."""
    mine = _subject_words(ds)
    scored = []
    for o in live:
        d = o.dataset
        if d.slug == ds.slug or (ds.collection and d.collection == ds.collection):
            continue
        if not set(d.topics) & set(ds.topics) or d.publisher.name == ds.publisher.name:
            continue
        shared = len(mine & _subject_words(d))
        if shared:
            scored.append((-shared, d.publisher.jurisdiction, -o.latest.rows, d))
    scored.sort(key=lambda t: t[:3])
    return [
        {"slug": d.slug, "title": d.title, "jur": d.publisher.jurisdiction}
        for *_, d in scored[:RELATED_MAX]
    ]


def _places(ds: Dataset, latest: VersionOut) -> list[dict]:
    """One entry per value of the place field, from the partition index the build wrote."""
    if not ds.place_field:
        return []
    out = []
    for e in latest.partitions.get(ds.place_field, []):
        if e["value"] is None:
            continue
        slug = e["json"].rsplit("/", 1)[-1].removesuffix(".json")
        out.append(
            {
                "value": str(e["value"]),
                "slug": slug,
                "rows": e["rows"],
                "rows_fmt": fmt_int(e["rows"]),
                "href": f"/d/{ds.slug}/in/{slug}/",
                "json": e["json"],
                "geojson": e.get("geojson"),
            }
        )
    return out


def _place_pages(
    ds: Dataset,
    o: DatasetOut,
    latest_view: dict,
    console: dict | None,
    db: Path,
    out: Path,
    page,
    places: list[dict],
    card,
    crumbs: list[tuple[str, str]],
    links: dict,
    citation: dict,
) -> list[str]:
    """A page for every value of the place field: its rows counted and drawn, its files, the
    first rows and the other places. Returns the page URLs for the sitemap."""
    if not places:
        return []
    latest = o.latest
    m = latest.manifest
    base = dataset_url(ds.slug)
    vbase = version_url(ds.slug, m.version)
    field = ds.field(ds.place_field)
    label = field.display
    plural = label + ("" if label.endswith("s") else "s")
    what = ds.row_label or "Rows"
    total = fmt_int(latest.rows)
    urls = []
    for p in places:
        within = {"field": ds.place_field, "op": "=", "value": p["value"]}
        fig = figures.dataset_figures(ds, m, console, db, out, within=within)
        years = fig.get("years", "")
        heading = f"{what} in {p['value']}"
        title = (
            f"{p['value']} {what.lower()}{f' {years}' if years else ''}: "
            f"{p['rows_fmt']} rows from {ds.title} | {HOST}"
        )
        intro = (
            f"{p['rows_fmt']} of the {total} {what.lower()} in {ds.title} are in {p['value']}"
            + (f", {years}" if years else "")
            + f". Published by {ds.publisher.name} under {ds.licence.title}, version {m.version}"
            + (f" as at {latest_view['as_at_long']}" if latest_view["as_at"] else "")
            + "."
        )
        description = (
            f"{intro} Download the rows as JSON{' or GeoJSON' if p['geojson'] else ''}, "
            f"or query them from a URL. No login, no key."
        )
        json_url = f"{vbase}{p['json']}"
        geojson_url = f"{vbase}{p['geojson']}" if p["geojson"] else ""
        rows_url = agg_url = ""
        if console:
            q = urllib.parse.quote(p["value"], safe="")
            api = f"/api/v1/datasets/{ds.slug}/"
            rows_url = f"{api}rows?{ds.place_field}=eq.{q}&limit=100"
            yf = figures.year_field(ds)
            if yf and yf[1] == "integer":
                agg_url = f"{api}aggregate?{ds.place_field}=eq.{q}&group={yf[0]}&metric=count"
        sample = _sample(ds, db, within)
        rel = f"d/{ds.slug}/in/{p['slug']}/index.html"
        href = SITE + p["href"]
        others = [x for x in places if x["slug"] != p["slug"]]
        md = "\n".join(
            [
                "---",
                f"title: {json.dumps(heading, ensure_ascii=False)}",
                f"resource: {href}",
                f"dataset: {base}",
                f"publisher: {ds.publisher.name}",
                f"licence: {ds.licence.title}",
                f"version: {m.version}",
                f"rows: {p['rows']}",
                "not_endorsed: true",
                "---",
                "",
                f"# {heading}",
                "",
                intro,
                "",
                f"{ds.publisher.name} has not endorsed this site. The whole table is {base}",
                "",
                "## Files",
                "",
                f"- JSON: {json_url}",
                *([f"- GeoJSON: {geojson_url}"] if geojson_url else []),
                *([f"- Query API rows: {SITE}{rows_url}"] if rows_url else []),
                *([f"- Query API counts by year: {SITE}{agg_url}"] if agg_url else []),
                "",
                "## Attribution and citation",
                "",
                attribution(ds, m),
                "",
                citation["harvard"],
                "",
                f"## Other {plural.lower()}",
                "",
                *[f"- [{x['value']}]({SITE}{x['href']}): {x['rows_fmt']} rows" for x in others],
                "",
            ]
        )
        jsonld = {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "@id": href,
            "url": href,
            "name": heading,
            "description": description,
            # A bare reference: the dataset page carries the full Dataset node under this @id.
            "about": {"@id": base},
            "isPartOf": {"@type": "WebSite", "name": HOST, "url": SITE + "/"},
            "license": ds.licence.url,
            "provider": OPERATOR_ORG,
        }
        page(
            rel,
            "place.html",
            md,
            title=title,
            description=description,
            nav="datasets",
            og=card,
            **links,
            jsonld=json.dumps(jsonld, ensure_ascii=False),
            extra_jsonld=[
                json.dumps(_breadcrumbs([*crumbs, (p["value"], href)]), ensure_ascii=False)
            ],
            ds=ds,
            base=base,
            latest=latest_view,
            heading=heading,
            intro=intro,
            place={
                **p,
                "json_url": json_url,
                "json_name": p["json"].rsplit("/", 1)[-1],
                "geojson_url": geojson_url,
                "geojson_name": (p["geojson"] or "").rsplit("/", 1)[-1],
            },
            place_label=label,
            place_plural=plural,
            total_fmt=total,
            fig=fig,
            rows_url=rows_url,
            agg_url=agg_url,
            console=console,
            sample_rows=sample,
            others=others,
            citation=citation,
            cite_request=CITE_REQUEST,
            jur_long=JUR_LONG[ds.publisher.jurisdiction],
            portal_host=(m.source.get("url") or "").split("/")[2] if m.source.get("url") else "",
        )
        urls.append(href)
    return urls


def licence_record(ds: Dataset, m) -> dict:
    """Where and when the licence was read: by a person, from the register, and by the fetch
    that made the latest version, from its manifest. A manifest from before fetches recorded the
    address carries the time only."""
    lic = m.licence or {}
    read_from = lic.get("read_from") or ""
    return {
        "evidence": ds.licence.evidence,
        "reviewed": long_date(ds.licence.reviewed) if ds.licence.reviewed else "",
        "read_at": long_date(lic["read_at"]) if lic.get("read_at") else "",
        "read_from": read_from,
        "read_from_host": read_from.split("/")[2] if read_from.count("/") >= 2 else "",
        "version": m.version,
    }


def serialise_dictionary(ds: Dataset, latest: VersionOut, path: Path) -> None:
    from .provenance import header
    from .serialise import write_dictionary

    m = latest.manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    write_dictionary(ds, header(ds, m, latest.rows, f"{version_url(ds.slug, m.version)}"), path)


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _words(ds: Dataset, name: str) -> str:
    try:
        return ds.field(name).display.lower()
    except KeyError:
        return name.replace("_", " ")


def _asked(ds: Dataset, console: dict) -> str:
    """What the example query counts or sums, in words."""
    ex = console["example"]
    return figures.measure(ds, ex["metric"], ex.get("label", ""))


def _example_title(ds: Dataset, console: dict) -> str:
    """The example query in words: the register's label when it is a whole phrase, else what it
    counts, by its group, where its filters hold."""
    ex = console["example"]
    if figures.PHRASE.search(ex.get("label", "")):
        return ex["label"]
    return f"{_asked(ds, console)} by {_words(ds, ex['group'][0])}{_filter_words(ds, console)}"


def _filter_words(ds: Dataset, console: dict) -> str:
    parts = [
        f"{_words(ds, f['field'])} {OP_WORDS[f['op']]} {f['value']}"
        for f in console["example"]["filters"]
    ]
    return " where " + " and ".join(parts) if parts else ""


OP_WORDS = {op: figures.VERBS[sql] for op, sql in WHERE_OPS.items()}
NO_FRAMES = "'none'"
SITE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' https://static.cloudflareinsights.com; connect-src 'self' https://cloudflareinsights.com; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Access-Control-Allow-Origin": "*",
    "Permissions-Policy": "interest-cohort=()",
}
# Only these paths run a function. A dataset page, its Markdown twin and its JSON are Pages files
# and are served without one. The count stays the same whatever the number of datasets.
ROUTES = (
    "/api/*",
    "/mcp",
    "/d/*/latest/*",
    "/d/*/v/*",
    "/d/*/diff/*",
    "/d/*/history.tar.zst",
)


SKILL_PATH = f"skills/{at.spec()['skill']['name']}/SKILL.md"
SKILL_URL = f"{SITE}/{SKILL_PATH}"
RFC9727 = "https://www.rfc-editor.org/info/rfc9727"


def _skill(for_agents: str, query_line: str) -> str:
    """The Agent Skill the discovery manifest lists, built from the words llms.txt and the MCP
    server already use."""
    s = at.spec()
    return "\n".join(
        [
            "---",
            f"name: {s['skill']['name']}",
            f"description: {s['skill']['description']}",
            "---",
            "",
            f"# {s['skill']['title']}",
            "",
            s["site"]["summary"],
            "",
            "## With the MCP server",
            "",
            s["mcp"]["intro"],
            "",
            s["mcp"]["instructions"],
            "",
            "## With the files and the query API",
            "",
            for_agents,
            "",
            query_line,
            "",
            f"Every live dataset is listed in {SITE}/llms.txt with its latest download URLs, and {SITE}/llms-full.txt adds each field. {SITE}/catalog.json is the same list as a DCAT catalogue.",
            "",
        ]
    )


def _api_catalog() -> dict:
    """The RFC 9727 API catalogue: the query API and the MCP server, each with its description."""
    return {
        "linkset": [
            {
                "anchor": f"{SITE}/api/v1/",
                "service-desc": [
                    {"href": f"{SITE}/openapi.json", "type": "application/vnd.oai.openapi+json"}
                ],
                "service-doc": [{"href": f"{SITE}/agents/", "type": "text/html"}],
                "status": [{"href": f"{SITE}/health.json", "type": "application/json"}],
            },
            {
                "anchor": f"{SITE}/mcp",
                "service-desc": [
                    {"href": f"{SITE}/mcp/server-card", "type": "application/mcp-server-card+json"}
                ],
                "service-doc": [{"href": f"{SITE}/agents/#mcp", "type": "text/html"}],
                "status": [{"href": f"{SITE}/health.json", "type": "application/json"}],
            },
        ]
    }


# The map chart fetches OpenStreetMap tiles from its worker, and only when a map panel is shown.
MAP_TILES = "https://tile.openstreetmap.org"


def explorer_csp(frame_ancestors: str) -> str:
    return (
        "default-src 'self'; script-src 'self' 'wasm-unsafe-eval' blob: https://static.cloudflareinsights.com; "
        f"worker-src 'self' blob:; connect-src 'self' blob: https://cloudflareinsights.com {MAP_TILES}; img-src 'self' data: blob:; "
        f"style-src 'self' 'unsafe-inline'; font-src 'self' data:; frame-ancestors {frame_ancestors}; base-uri 'self'; form-action 'self'"
    )


def _escape_script(text: str) -> str:
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _script_json(value) -> str:
    """JSON for a script block. Values come from the data, so nothing in them may close it."""
    return _escape_script(json.dumps(value, ensure_ascii=False))


def _md_twin_explore(ds: Dataset, explore: dict, attr: str) -> str:
    page = SITE + explore["page"]
    return "\n".join(
        [
            "---",
            f"title: Explore {ds.title}",
            f"resource: {page}",
            f"dataset: {dataset_url(ds.slug)}",
            "not_endorsed: true",
            "---",
            "",
            f"# Explore {ds.title}",
            "",
            f"An interactive dashboard over {ds.title}, worked out in the browser with DuckDB-WASM and Perspective from the Parquet file of a dated version. It needs a browser; an agent is better served by the query API or the files on {dataset_url(ds.slug)}index.md.",
            "",
            f"The dashboard state sits in the URL fragment. A saved dashboard is at {page}?view=<id>, and POST {SITE}/api/v1/views stores one. The frame for embedding is {SITE}{explore['embed_page']}?view=<id>, read-only unless the address adds &edit=1.",
            "",
            f"Versions: {', '.join(v['version'] + ' ' + SITE + v['parquet'] for v in explore['versions'])}",
            "",
            "## Attribution",
            "",
            attr,
            "",
            f"Published by {ds.publisher.name} under {ds.licence.title}. {ds.publisher.name} has not endorsed this site.",
            "",
        ]
    )


def _db_view(ds: Dataset, v: VersionOut) -> dict:
    """What the database page lists: each table with its rows, size, key and references, the
    views, and the files."""
    vb = version_url(ds.slug, v.manifest.version)
    tables = []
    for t in ds.tables:
        name = f"tables/{t.name}.parquet"
        tables.append(
            {
                "name": t.name,
                "source": t.source,
                "description": t.description,
                "rows": v.tables.get(t.name, 0),
                "rows_fmt": fmt_int(v.tables.get(t.name, 0)),
                "key": list(t.key),
                "fields": [
                    {
                        "name": f.name,
                        "type": f.type,
                        "source": f.source,
                        "description": f.description,
                        "references": f.references,
                    }
                    for f in t.fields
                ],
                "refs": sorted({f.references.split(".", 1)[0] for f in t.fields if f.references}),
                "parquet": vb + name,
                "size": fmt_size(v.files.get(name)),
            }
        )
    largest = max(tables, key=lambda t: t["rows"]) if tables else None
    return {
        "tables": tables,
        "views": [{"name": x.name, "description": x.description, "sql": x.sql} for x in ds.views],
        "duckdb": vb + "data.duckdb",
        "duckdb_size": "about " + fmt_size(v.files.get("data.duckdb")),
        "parquet_size": fmt_size(
            sum(v.files.get(f"tables/{t.name}.parquet", 0) for t in ds.tables)
        ),
        "schema_sql": vb + "schema.sql",
        "schema_json": vb + "schema.json",
        "source": vb + f"source.{v.manifest.ext}",
        "source_size": fmt_size(v.files.get(f"source.{v.manifest.ext}")),
        "largest": largest,
        "start": ds.views[0].name if ds.views else (largest["name"] if largest else ""),
        "references": sum(1 for t in ds.tables for f in t.fields if f.references),
    }


def _use_tabs(ds: Dataset, v: VersionOut, aggregate: str = "") -> list[dict]:
    """How to open the dataset from Excel, Power BI, R, Python, DuckDB and a script, as the
    connect tabs show. Code uses the dated version's URL, since a pinned file never changes under
    a script; Excel and Power BI use latest/, since their refresh is how a workbook follows a new
    version."""
    vb = version_url(ds.slug, v.manifest.version)
    name = ds.slug.replace("-", "_")
    if ds.kind == "database":
        start = ds.views[0].name if ds.views else ds.tables[0].name
        first = ds.tables[0]
        group = (ds.views[0].example if ds.views else "") or next(
            (
                f.name
                for f in first.fields
                if f.type == "string" and not f.references and f.name not in first.key
            ),
            first.fields[0].name,
        )
        q = f"SELECT {group}, count(*) AS n FROM {start} GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
        return [
            {
                "label": "R",
                "kind": "command",
                "value": f'library(publicdataau)\ncon <- pd_connect("{ds.slug}")\nDBI::dbGetQuery(con, "{q}")\n# dplyr: pd_tbl("{ds.slug}", "{start}")',
                "note": "pd_connect() attaches the version read-only over HTTPS, so the query runs without a download. Needs the duckdb and DBI packages.",
            },
            {
                "label": "Python",
                "kind": "command",
                "value": f'import publicdata_au as pd_au\ncon = pd_au.connect("{ds.slug}")\ncon.sql("{q}").df()',
                "note": "pip install publicdata-au duckdb. The connection has the version attached read-only; every table and view is in it.",
            },
            {
                "label": "DuckDB",
                "kind": "command",
                "value": f"INSTALL httpfs; LOAD httpfs;\nATTACH '{vb}data.duckdb' AS {name} (READ_ONLY);\n{q.replace(f'FROM {start}', f'FROM {name}.{start}')};",
                "note": "In the DuckDB shell or any client. Only the blocks the query touches are read over HTTPS.",
            },
            {
                "label": "Parquet",
                "kind": "command",
                "value": f"import pandas as pd\ndf = pd.read_parquet('{vb}tables/{(ds.tables[0].name)}.parquet')",
                "note": "One Parquet file per table under tables/, for pandas, Polars, Arrow, Spark and R's arrow package.",
            },
        ]
    key = ds.partition_by[0] if ds.partition_by else (ds.key[0] if ds.key else ds.fields[0].name)
    latest_csv = f"{dataset_url(ds.slug)}latest/data.csv"
    script = (
        [
            {
                "label": "JavaScript",
                "kind": "command",
                "value": f'const res = await fetch("{SITE}{aggregate}");\nconst {{ rows, publicdata }} = await res.json();\nconsole.log(rows, publicdata.attribution);',
                "note": "The query API answers a page on any site as well as Node, with no key. It returns the rows and the attribution the licence asks for.",
            }
        ]
        if aggregate
        else []
    )
    return [
        {
            "label": "Excel",
            "kind": "command",
            "value": latest_csv,
            "note": "In Excel choose Data, then From Web, and paste this address. Excel keeps it, so Refresh All reads the newest version. A sheet holds about a million rows; past that, load the query to the Data Model.",
        },
        {
            "label": "Power BI",
            "kind": "command",
            "value": latest_csv,
            "note": "In Power BI Desktop choose Get data, then Web, paste this address and choose Anonymous when asked how to sign in. A scheduled refresh reads the same address, so the report follows each new version.",
        },
        {
            "label": "R",
            "kind": "command",
            "value": f'library(publicdataau)\ndf <- pd_read("{ds.slug}")\npd_attribution(df)',
            "note": 'install.packages("publicdataau"). pd_read() fetches the version\'s Parquet file; pd_rows() and pd_aggregate() ask the query API instead, and pd_connect() attaches the DuckDB file.',
        },
        {
            "label": "Python",
            "kind": "command",
            "value": f'import publicdata_au as pd_au\ndf = pd_au.read("{ds.slug}")\ndf.attrs["publicdata"]["attribution"]',
            "note": 'pip install "publicdata-au[pandas]". read() fetches the version\'s Parquet file; rows() and aggregate() ask the query API, and connect() attaches the DuckDB file.',
        },
        {
            "label": "DuckDB",
            "kind": "command",
            "value": f"INSTALL httpfs; LOAD httpfs;\nATTACH '{vb}data.duckdb' AS {name} (READ_ONLY);\nSELECT {key}, count(*) FROM {name}.records GROUP BY 1 ORDER BY 2 DESC;",
            "note": "The DuckDB file attaches read-only over HTTPS and only the blocks a query touches are read. Parquet works the same way: FROM read_parquet(url).",
        },
        *script,
    ]


def _db_seo_title(ds: Dataset, v: VersionOut) -> str:
    lead = ds.search_title or ds.title
    return f"{lead}: DuckDB and Parquet | {HOST}"


def _db_seo_description(ds: Dataset, v: VersionOut) -> str:
    summary = ds.summary.rstrip(".")
    return (
        f"{summary}. {fmt_int(v.rows)} rows in {len(ds.tables)} tables from {ds.publisher.name} under {ds.licence.title}. "
        "One DuckDB database that attaches over HTTPS, one Parquet file per table. No login, no key."
    )


def _db_faq(ds: Dataset, v: VersionOut) -> list[tuple[str, str]]:
    m = v.manifest
    base = dataset_url(ds.slug)
    vbase = version_url(ds.slug, m.version)
    short = _short_title(ds)
    start = ds.views[0].name if ds.views else ds.tables[0].name
    out = [
        (
            f"How do I query {short} without downloading it?",
            f"Attach {vbase}data.duckdb read-only from DuckDB, R or Python and query any table or view, such as {start}. DuckDB reads only the blocks a query touches over HTTPS. "
            f"{base}latest/data.duckdb redirects to the newest version; a dated URL never changes.",
        ),
        (
            f"How do I get one table of {short}?",
            f"Every table is a Parquet file under {vbase}tables/, for example {vbase}tables/{ds.tables[0].name}.parquet, which pandas, R, Polars, Spark and DuckDB read directly. "
            f"{vbase}schema.sql has the CREATE TABLE statements with the keys and references, and {vbase}schema.json the same as Table Schema.",
        ),
    ]
    if ds.source.cadence:
        out.append(
            (
                f"How often is {short} updated?",
                f"{cadence_words(ds)[1]} This site checks the portal every week and adds a dated version when the release changes.",
            )
        )
    lic = ds.licence
    if lic.condition:
        use = f"Yes, with one condition. {lic.condition} The attribution string is in this page's side column and inside every file."
    elif "SA" in lic.id:
        use = f"Yes. {lic.title} allows commercial use as long as the attribution is kept and anything you build from it carries the same licence."
    else:
        use = f"Yes. {lic.title} allows commercial use, redistribution and derived works as long as the attribution is kept. The attribution string is in this page's side column and inside every file."
    out.append((f"Can I use {short} commercially?", use))
    out.append(
        (
            f"Is this the official source for {short}?",
            f"No. The publisher is {ds.publisher.name}, and its page is {landing(ds)}. This site republishes the publisher's release without changing its content: the files are typed and the columns named in lower case. "
            f"The publisher's archive sits beside every version as source.{m.ext} with its SHA-256, so the two can be compared.",
        )
    )
    return out


def _md_twin_database(
    ds: Dataset,
    o: DatasetOut,
    views: list[dict],
    faq: list[tuple[str, str]] = (),
    citation: dict | None = None,
    related: list[dict] = (),
) -> str:
    v = o.latest
    m = v.manifest
    base = dataset_url(ds.slug)
    vbase = version_url(ds.slug, m.version)
    lines = [
        "---",
        f"title: {ds.title}",
        f"resource: {base}",
        f"publisher: {ds.publisher.name}",
        f"licence: {ds.licence.title}",
        f"licence_url: {ds.licence.url}",
        *([f"licence_condition: {ds.licence.condition}"] if ds.licence.condition else []),
        "kind: database",
        f"version: {m.version}",
        *([f"as_at: {m.as_at}"] if m.as_at else []),
        f"rows: {v.rows}",
        f"tables: {len(ds.tables)}",
        f"fields: {ds.field_count}",
        f"source: {m.source.get('url')}",
        f"source_sha256: {m.sha256}",
        "language: en-AU",
        f"operator: {OPERATOR}",
        "not_endorsed: true",
        "---",
        "",
        f"# {ds.title}",
        "",
        ds.summary,
        "",
        f"Published by {ds.publisher.name} under {ds.licence.title} and republished here as one DuckDB database and one Parquet file per table, with cells typed and columns named in lower case. {ds.publisher.name} has not endorsed this site.",
        "",
    ]
    if ds.licence.condition:
        lines += [f"Condition of use: {ds.licence.condition}", ""]
    lines += [
        *([f"Also called: {', '.join(ds.also_known_as)}.", ""] if ds.also_known_as else []),
        "## Files",
        "",
        f"- DuckDB, every table and view: {vbase}data.duckdb (about {fmt_size(v.files.get('data.duckdb'))}). Attach it read-only over HTTPS: ATTACH '{vbase}data.duckdb' AS db (READ_ONLY);",
        f"- Parquet, one file per table: {vbase}tables/<table>.parquet",
        f"- SQL: {vbase}schema.sql (CREATE TABLE with keys, references and the views)",
        f"- Schema: {vbase}schema.json",
        f"- The publisher's archive: {vbase}source.{m.ext} ({fmt_size(v.files.get(f'source.{m.ext}'))})",
        "",
        f"{base}latest/ redirects to the newest version. Dated versions keep their content.",
        "",
        "## Tables",
        "",
    ]
    for t in ds.tables:
        lines.append(
            f"- {t.name} ({fmt_int(v.tables.get(t.name, 0))} rows{', key ' + ', '.join(t.key) if t.key else ''}): {t.description} Fields: "
            + ", ".join(
                f"{f.name} ({f.type}{' → ' + f.references if f.references else ''})"
                for f in t.fields
            )
        )
    if ds.views:
        lines += ["", "## Views", ""]
        for x in ds.views:
            lines += [f"- {x.name}: {x.description}"]
    lines += ["", "## Versions", ""]
    for view in reversed(views):
        lines.append(
            f"- {view['version']}: {view['rows_fmt']} rows, {len(view['tables'])} tables, fetched {view['fetched_long']}, source sha256 {view['sha256']}"
        )
    if related:
        lines += ["", "## Same subject, other publishers", ""]
        lines += [f"- {r['title']} ({r['jur']}): {dataset_url(r['slug'])}" for r in related]
    if faq:
        lines += ["", "## Questions", ""]
        for q, a in faq:
            lines += [f"### {q}", "", a, ""]
    lines += ["## Attribution", "", attribution(ds, m), ""]
    if citation:
        lines += ["Citation: " + citation["text"], ""]
    lines += [f"{NOT_ENDORSED}", ""]
    return "\n".join(lines)


def _md_twin_dataset(
    ds: Dataset,
    o: DatasetOut,
    views: list[dict],
    siblings: list[Dataset],
    faq: list[tuple[str, str]] = (),
    citation: dict | None = None,
    console: dict | None = None,
    explore: bool = False,
    related: list[dict] = (),
    places: list[dict] = (),
) -> str:
    v = o.latest
    m = v.manifest
    base = dataset_url(ds.slug)
    vbase = version_url(ds.slug, m.version)
    lines = [
        "---",
        f"title: {ds.title}",
        f"resource: {base}",
        f"publisher: {ds.publisher.name}",
        f"licence: {ds.licence.title}",
        f"licence_url: {ds.licence.url}",
        f"version: {m.version}",
        *([f"as_at: {m.as_at}"] if m.as_at else []),
        f"rows: {v.rows}",
        f"fields: {len(ds.fields)}",
        f"source: {m.source.get('url')}",
        f"source_sha256: {m.sha256}",
        "language: en-AU",
        f"operator: {OPERATOR}",
        "not_endorsed: true",
        "---",
        "",
        f"# {ds.title}",
        "",
        ds.summary,
        "",
        f"Published by {ds.publisher.name} under {ds.licence.title} and republished here in other formats, with cells typed and suppressed values made null. {ds.publisher.name} has not endorsed this site.",
        "",
        *([f"Condition of use: {ds.licence.condition}", ""] if ds.licence.condition else []),
        *([f"Also called: {', '.join(ds.also_known_as)}.", ""] if ds.also_known_as else []),
        "## Download",
        "",
        f"Latest version, redirects to `{vbase}`:",
        "",
    ]
    for fmt in _fmts(ds, v):
        lines.append(f"- {fmt}: {base}latest/data.{fmt} ({fmt_size(v.files.get(f'data.{fmt}'))})")
    if why := _left_out(ds, v):
        lines += ["", *why]
    lines += [
        "",
        f"Pinned version {m.version}: `{vbase}data.<format>`. Dated versions keep their content.",
        "",
    ]
    if console:
        api = f"{SITE}/api/v1/datasets/{ds.slug}/"
        A = at.spec()["api"]
        lines += [
            "## Query",
            "",
            f"{A['summary']} Rows: {api}rows. Counts and sums: {api}aggregate.",
            "",
            f"- Rows: {SITE}{_example_query(ds.slug, console, 'rows')}",
            f"- Aggregate: {SITE}{_example_query(ds.slug, console, 'aggregate')}",
            "",
            FILTER_HELP,
            "",
            *[f"- `{n}`: {at.param_text(n)}" for n in at.query_params()],
            "",
            f"{A['versions']} {A['rate_limit']} OpenAPI for this dataset: {base}openapi.json",
            "",
        ]
    if explore:
        lines += [
            "## Explore",
            "",
            f"Charts and dashboards in the browser: {base}explore/. A dashboard can be shared as a link or embedded.",
            "",
        ]
    if ds.partition_by:
        lines += ["## Smaller files", ""]
        for fname in ds.partition_by:
            lines.append(
                f"- by `{fname}`: {vbase}by/{fname}/index.json lists {len(v.partitions.get(fname, []))} files"
            )
        lines.append("")
    lines += ["## Fields", "", "| field | type | publisher's header | note |", "|---|---|---|---|"]
    for f in ds.fields:
        lines.append(f"| `{f.name}` | {f.type} | {f.source} | {f.description} |")
    if ds.key:
        lines += ["", f"Key: `{', '.join(ds.key)}`."]
    lines += ["", "## Versions", ""]
    for vv in reversed(views):
        chg = _change_words(vv["change"])
        lines.append(
            f"- {vv['version']}{' as at ' + vv['as_at_long'] if vv['as_at'] else ''}: {vv['rows']} rows, {vv['fields']} fields, sha256 {vv['sha256'][:12]}{chg}. {version_url(ds.slug, vv['version'])}"
        )
    lines += [
        "",
        f"Machine-readable: {base}versions.json, {base}changes.json, {base}datapackage.json, {base}schema.json, {base}history.tar.zst",
        "",
    ]
    if siblings:
        lines += [f"## Part of {ds.collection_title}", ""]
        for s in siblings:
            lines.append(f"- [{s.title}]({dataset_url(s.slug)})")
        lines.append("")
    if related:
        lines += ["## Same subject, other publishers", ""]
        for r in related:
            lines.append(f"- [{r['title']}]({dataset_url(r['slug'])}) ({r['jur']})")
        lines.append("")
    if places:
        lines += [f"## By {ds.field(ds.place_field).display.lower()}", ""]
        for p in places:
            lines.append(f"- [{p['value']}]({SITE}{p['href']}): {p['rows_fmt']} rows")
        lines.append("")
    lines += [
        "## Attribution and citation",
        "",
        attribution(ds, m),
        "",
        CITE_REQUEST,
        "",
        *(
            [
                f"Plain text: {citation['text']}",
                "",
                f"Harvard: {citation['harvard']}",
                "",
                f"Markdown: {citation['markdown']}",
                "",
            ]
            if citation
            else []
        ),
    ]
    if faq:
        lines += ["## Questions", ""]
        for q, a in faq:
            lines += [f"### {q}", "", a, ""]
    lines += [
        "## What this site did to the data",
        "",
        "Cells were typed, headers were renamed to snake_case and the encoding was made UTF-8. Rows were left alone. Nothing is derived. The publisher's file is beside every version as `source."
        + m.ext
        + "`.",
        "",
        "## Improve this dataset",
        "",
        f"This page is built from [{register_path(ds)}]({REPO}/blob/main/{register_path(ds)}) on GitHub, and a fix to it is welcome as a pull request. {SITE}/contribute/ explains how.",
        "",
    ]
    return "\n".join(lines)


def html_to_md(body: str) -> str:
    s = body
    s = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n## \1\n", s, flags=re.S)
    s = re.sub(r"<h3[^>]*>(.*?)</h3>", r"\n### \1\n", s, flags=re.S)
    s = re.sub(r"<pre[^>]*>(.*?)</pre>", r"\n```\n\1\n```\n", s, flags=re.S)
    s = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1", s, flags=re.S)
    s = re.sub(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", r"- \1: \2\n", s, flags=re.S)
    s = re.sub(
        r"<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
        lambda m: (
            f"[{m.group(2)}]({m.group(1) if m.group(1).startswith('http') else SITE + m.group(1)})"
        ),
        s,
        flags=re.S,
    )
    s = re.sub(r"<(code|span class=\"mono\")>(.*?)</(code|span)>", r"`\2`", s, flags=re.S)
    s = re.sub(r"<(b|strong)>(.*?)</(b|strong)>", r"**\2**", s, flags=re.S)
    s = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip() + "\n"


def register_path(ds: Dataset) -> str:
    """The entry's path in the repository, for the link that offers a fix to it."""
    parts = Path(ds.path).parts
    if "register" not in parts:
        return f"register/{ds.slug}.yaml"
    return "/".join(parts[len(parts) - 1 - parts[::-1].index("register") :])


PROSE = {
    "publishers": (
        "For publishers",
        "How this site treats a publisher's data, and what a publisher can do about it.",
        """
<p>This site republishes datasets that Australian governments already publish under an open licence. It does not change the content. It types the cells, renames the headers, converts the encoding and serves the result at a URL that never changes, in the formats developers and analysts ask for.</p>
<h2>What you get without asking</h2>
<ul>
<li>Your attribution string inside every file, in the form your licence asks for.</li>
<li>A version for every release you make, with a diff against the release before, so your own history is visible.</li>
<li>Your original file beside every version, byte for byte, so anyone can check our work.</li>
<li>A statement on every page and in every catalogue record that you have not endorsed this site.</li>
<li>No traffic to your servers from our readers. We fetch once per release and serve everything from here.</li>
</ul>
<h2>What we would like from you</h2>
<ul>
<li>A download URL that does not change between releases. Portals that rename the file each quarter still work, but a stable URL means we never need a browser to fetch it. <a href="/publishers/stable-urls/">Publishing a dataset at a stable URL</a> sets out a layout that gives this and keeps the history.</li>
<li>A licence stated on the dataset page. A dataset with no licence stated waits in the backlog until you confirm one in writing.</li>
<li>A contact for corrections. If a reader finds a problem in the content, we send it to you.</li>
</ul>
<h2>If you would rather we did not</h2>
<p>Write to us and say so. We will remove the dataset and record the removal. The licence allows republication, but we would rather work with you than around you.</p>
<h2>If you would like more</h2>
<p>The <a href="/backlog/">backlog</a> shows which of your datasets people have asked for and how many votes each has. We build in vote order. If you want one built sooner, or want the same treatment for data you have not published yet, <a href="https://nationaldigital.com.au/contact/">talk to National Digital</a>, the company that runs this site.</p>
<p class="muted">Publishers with datasets here: {publishers}.</p>
""",
    ),
    "publishers/stable-urls": (
        "Publishing a dataset at a stable URL",
        "A layout for releasing a data file so that every link keeps working, a program can fetch each release on its own, and a change to the columns breaks nothing it need not.",
        """
<p>This page is for the team inside an agency that publishes a data file and replaces it at each release. It sets out a layout for the files and their URLs that keeps every link working, lets a program fetch the newest release without a person, and survives a change to the columns. It is the layout this site uses for every dataset it serves, and it comes from what breaks across the government sources the site reads. Nothing here needs a portal, a database or an API. A folder on an ordinary web server is enough.</p>
<h2>Two URLs for every file</h2>
<p>A dataset that changes needs two kinds of URL. A dated URL names one release and returns the same bytes for as long as the site exists. A current URL always returns the newest release and never changes. Each does a job the other cannot. A report cites the dated URL, so a reader years later opens the file the author used. A program reads the current URL, so it picks up each release without anyone editing it.</p>
<p>Most sites publish one or the other. A file that is overwritten in place gives a current URL and loses the history. A file with the release date in its name, such as <code>enrolments_26052026.xlsx</code>, gives a dated URL and breaks every link at the next release, because the new file has a new name and the old one is usually taken down.</p>
<p>The layout below gives both.</p>
<pre>/data/senior-enrolments/
  index.html                        the dataset page
  latest/senior-enrolments.csv      the current URL, a redirect to the newest dated file
  latest/senior-enrolments.xlsx
  v/2026-05-26/senior-enrolments.csv
  v/2026-05-26/senior-enrolments.xlsx
  v/2026-05-26/schema.json
  v/2025-06-20/senior-enrolments.csv
  v/2025-06-20/senior-enrolments.xlsx
  v/2025-06-20/schema.json
  versions.json                     every release, newest first
  schema.json                       the current column list
  changes.md                        what changed at each release</pre>
<p>The date goes in the path and the file name stays the same, so a saved file has the same name whichever release it came from, and the folder it sits in says when it was made. The date is the day of the release as <code>YYYY-MM-DD</code>, which sorts into order in any file listing.</p>
<h2>The current URL</h2>
<p>The current URL should redirect to the newest dated file with status 302 or 307, so that a program which follows it can see from the final address which release it received. If the web server cannot redirect, overwrite the file at the current URL with a copy of the newest release instead. The bytes change and the address does not, and a program can tell releases apart by the <code>Last-Modified</code> and <code>ETag</code> headers, so send both.</p>
<p>Publish <code>versions.json</code> beside it. It lists each release with its date, its dated URL, its SHA-256 hash, its row count and the schema version it follows. A program that reads this file can tell whether there is a new release without downloading one, and can fetch an older release when it needs it.</p>
<pre>{
  "dataset": "senior-enrolments",
  "schema_version": "1.3",
  "versions": [
    {"date": "2026-05-26", "schema_version": "1.3", "rows": 48213,
     "sha256": "9f2c…", "url": "/data/senior-enrolments/v/2026-05-26/senior-enrolments.csv"},
    {"date": "2025-06-20", "schema_version": "1.2", "rows": 46990,
     "sha256": "41b0…", "url": "/data/senior-enrolments/v/2025-06-20/senior-enrolments.csv"}
  ]
}</pre>
<h2>The dated URL</h2>
<p>A dated file is never edited and never removed. If a release turns out to be wrong, publish a corrected release under a new date and say in the change log which release it replaces and why. The wrong file stays where it is, because reports already cite it. Send <code>Cache-Control: public, max-age=31536000, immutable</code> on a dated file, so that browsers and proxies keep it, and a short <code>max-age</code> on the current URL, so that a new release is seen within minutes.</p>
<h2>Headers and access</h2>
<p>Serve each file with its correct <code>Content-Type</code> (<code>text/csv; charset=utf-8</code> for CSV), a <code>Content-Disposition</code> header carrying the file name, <code>Last-Modified</code>, <code>ETag</code>, and support for <code>HEAD</code> and range requests, so a program can check for a new release cheaply. Nothing on the data path should need a login or JavaScript.</p>
<p>If the site runs a bot challenge, exempt the data paths from it. A challenge exists to stop programs, and the programs it stops include every one the data is published for. A program cannot pass a challenge and should not try, so a dataset behind one is read by hand or left alone. A rate limit on the data paths protects the server and still lets the readers through.</p>
<h2>When the columns change</h2>
<p>A dataset's schema is the list of its columns, with the name, the type and the meaning of each. Rows change at every release, and that is expected. The schema should change rarely, and when it does the change is either compatible or breaking, and each kind has its own rule.</p>
<p>A compatible change adds a column, or widens what a column can hold without changing what it means. A program that reads the file by column name keeps working. Make the change at the current URL, add the column at the end, raise the minor part of the schema version (1.2 to 1.3) and note it in the change log.</p>
<p>A breaking change renames a column, removes one, changes its type, its unit, its code list or the population it counts, or changes what a column means while keeping its name. That last one does harm because nothing fails and the numbers are quietly wrong. Every program that reads the file is affected, so a breaking change gets a new major version at a new path. Publish the new series under <code>/data/senior-enrolments/v2/</code>, keep the old series where it is, and say on the page which old column maps to which new one. If the old series can be produced for one more release, do that, so readers have a release to compare.</p>
<p>A few habits avoid most breaking changes. Keep column names plain, with ASCII letters, digits and underscores, and keep units and years out of them. Put a value that varies into a row: a column per year, such as <code>2024</code> and <code>2025</code>, is a schema change every year, where a <code>year</code> column is a row change. Never put text in a numeric column. A suppressed count is a blank cell with a second column that says it was suppressed, or a documented token such as <code>&lt;5</code> that is used the same way in every release.</p>
<p>The schema version is a number on the dataset page and in <code>schema.json</code>, separate from the release date. Two releases a year apart with the same columns share a schema version, and a reader can tell from the number alone whether the file they wrote code for still reads.</p>
<h2>Describing the columns</h2>
<p>Publish the column list as a file a program can read as well as a table a person can. The format this site uses and recommends is <a href="https://specs.frictionlessdata.io/table-schema/">Table Schema</a>, a short JSON document naming each column with its type, a one-line description, the list of codes it holds where that applies, and the column that identifies a row. A workbook can carry the same information on a sheet of its own, one row per column, and some Australian publishers already do this well.</p>
<p>Identify a row by a code that does not change, and publish the lookup from code to name beside the data. Schools, councils, stations and hospitals are renamed and merged, and a name that was unique in one release is ambiguous in the next. A dataset keyed on a stable code can be joined to its own earlier releases and to other datasets. The name belongs in the file as well, for the reader, and the code is the key.</p>
<h2>The dataset page</h2>
<p>One page per dataset, at a URL that does not change, holds the description, the licence with the attribution wording the agency wants, a contact for corrections, the known caveats such as a preliminary period or a change of method, the current and dated download links, the schema version and the change log. Give the page schema.org <code>Dataset</code> markup, and give the portal record the same facts pointing at the current URL, so catalogues and search engines find it. On a CKAN portal, update the existing resource at each release instead of adding a new one, so that the resource id and its URL hold.</p>
<h2>A check list</h2>
<ol>
<li>Every release has a dated URL that never changes and is never removed.</li>
<li>A current URL redirects to, or holds a copy of, the newest release, and the file name is the same at both.</li>
<li><code>versions.json</code> lists every release with its date, URL, hash, row count and schema version.</li>
<li>Files are served with a content type, <code>Last-Modified</code>, <code>ETag</code> and range support, and need no login or challenge.</li>
<li>Column names are plain and stable, values that vary are rows, and numeric columns hold only numbers.</li>
<li><code>schema.json</code> describes every column, and a schema version separate from the release date says when the columns changed.</li>
<li>A compatible change adds a column at the end. A breaking change starts a new major version at a new path and keeps the old one.</li>
<li>Rows carry a stable code, and the lookup from code to name is published.</li>
<li>The dataset page states the licence, the attribution and a contact, and carries the change log.</li>
</ol>
<h2>What this site does with it</h2>
<p>When a publisher follows this layout, this site reads the current URL each week, and a release it has not seen becomes a dated version here with no person involved, with a diff against the version before and the publisher's own file beside it. A column the register does not name is reported and held until a person reviews it, so a breaking change at the source pauses the mirror until it is understood. The <a href="/publishers/">publishers page</a> says what else a publisher gets. <a href="https://nationaldigital.com.au/contact/">National Digital</a>, which runs this site, will talk through a layout with any agency that asks. No government agency has endorsed this site.</p>
<h2>Further reading</h2>
<ul>
<li><a href="https://www.w3.org/TR/dwbp/">Data on the Web Best Practices</a> from the W3C, in particular the practices on persistent URIs, version indicators and version history.</li>
<li><a href="https://specs.frictionlessdata.io/table-schema/">Table Schema</a>, the column description format.</li>
<li><a href="https://www.w3.org/TR/vocab-dcat-3/">DCAT 3</a>, the catalogue vocabulary the portals use, which has a field for the current download URL and one for the licence.</li>
</ul>
""",
    ),
    "government": (
        "For government staff",
        "What a public servant needs to know before using publicdata.au in a brief, a report or a model: what it is, what it is not, and how to check it.",
        """
<p>This page is for people who work in an Australian government agency and want to use a dataset from this site in their work. It says what the site is, what it is not, and how to check anything on it against the source.</p>
<h2>It is not an official source</h2>
<p>publicdata.au is a private website run by <a href="https://nationaldigital.com.au/">National Digital</a>, a software company in Burleigh Heads, Queensland. No government agency runs it, funds it or has endorsed it. The official source for every dataset is the publisher's own portal, and each dataset page links to it. When a figure matters, cite the publisher and use this site for the file.</p>
<h2>What you can rely on</h2>
<p>Every version keeps the publisher's own file beside it, byte for byte, with its SHA-256 hash. The manifest beside it records the portal URL, the date the file was fetched and the licence the portal stated at that moment. You can download the source file from here or from the portal and compare the hashes.</p>
<p>The rows are the publisher's rows. Cells are typed, headers are renamed to a plain form with the original header kept beside each one, and the encoding is made UTF-8. A cell the publisher suppressed, such as "&lt;5", becomes null with a flag that says so. Nothing is added, removed, ranked, joined or summarised. The count charts on a dataset page are counts of rows worked out in the build, and the caption says so.</p>
<h2>A URL you can put in a brief</h2>
<p>A dated version keeps the same content. A link of the form <code>/d/&lt;dataset&gt;/v/&lt;date&gt;/</code> returns the same data for as long as the site exists, so a reader of your brief can open the file you used. The <code>latest/</code> link moves to the newest release, so use a dated link when the figure must stay the same.</p>
<p>Each dataset page has a citation ready to copy in author-date form, the form most departmental style guides ask for, as well as plain text, HTML, Markdown and BibTeX. The licence requires the publisher's attribution, which is inside every file. We ask that you also name publicdata.au and link to the version, and you are free to decline.</p>
<h2>Before you use a dataset</h2>
<p>Each dataset page has a box under the description that states the publisher's own caveats, such as which months are preliminary and which years are not comparable. Read it before you quote a figure. The publisher's release notes on the portal remain the authority.</p>
<h2>On a departmental network</h2>
<p>The site is plain HTTPS. Nothing needs a login, a key, a plug-in or JavaScript to download a file. The default download on every dataset page is Excel when the workbook is a size an office machine opens, and CSV otherwise. Excel and Power BI can read a CSV or a workbook straight from its URL through Data, then Get Data, then From Web, and the dataset page shows the exact steps for the format you pick.</p>
<p>The site sets no cookies and runs no advertising or third-party tracking. The only analytics is a first-party Cloudflare page count, and there are no accounts. The <a href="/privacy/">privacy page</a> lists everything the site records. Cloudflare hosts the site and serves it from its network in Australia and elsewhere. The disclosure policy for security reports is at <a href="/.well-known/security.txt"><code>/.well-known/security.txt</code></a>.</p>
<h2>Who runs it</h2>
<p>National Digital, ABN 13 744 838 758, Unit 11, 76 Township Drive, Burleigh Heads QLD 4220. The <a href="/terms/">terms of use</a> set out the liability position, including the Australian Consumer Law. Questions and corrections go through the <a href="https://nationaldigital.com.au/contact/">contact page</a>.</p>
<h2>If a value looks wrong</h2>
<p>It is almost always wrong in the publisher's file as well, because the site publishes what the publisher publishes. Send it to the publisher, whose page is linked from the dataset. If the serialisation is wrong, for example a column typed badly, <a href="/about/#corrections">tell us</a> and we will fix it, publish a new build and record the correction in the version's notes.</p>
<h2>Asking for a dataset</h2>
<p>Every dataset listed on an Australian government open-data portal can be searched from the box at the top of any page, and one with an open licence and a download can be voted for. Votes set the build order. Nobody is asked who they are. If your agency would like a dataset built sooner, or wants the same treatment for data it has not published yet, the <a href="/publishers/">publishers page</a> says how that works.</p>
<h2>If your agency publishes</h2>
<p>A dataset listed on your portal with an open licence can be served here without a request, and you can ask for it to be removed at any time. Your attribution travels inside every file, your original file sits beside every version, and every page states that you have not endorsed the site. The <a href="/publishers/">publishers page</a> has the detail. If your team is working out how to release a file at a URL that holds, or how to change its columns without breaking the people who read it, <a href="/publishers/stable-urls/">publishing a dataset at a stable URL</a> sets out the layout this site recommends.</p>
""",
    ),
    "agents": (
        "For agents",
        "How an AI agent or a script should read this site.",
        """
<p>Everything here is meant to be read by a program first. There are no keys and no accounts, and no data file needs JavaScript. Files have no download limit, and the query API's limit is set out below.</p>
<h2>Start with one of these</h2>
<ul>
<li id="query-api">{query_api}</li>
<li><a href="/catalogue/publishers.json"><code>/catalogue/publishers.json</code></a> lists every publisher on Australia's government portals with its page and counts. Each publisher page has a <code>catalogue.json</code> of the datasets it lists, with licence, formats and the portal URL.</li>
<li><a href="/llms.txt"><code>/llms.txt</code></a> lists every dataset with its latest download URLs. <a href="/llms-full.txt"><code>/llms-full.txt</code></a> adds the field list for each.</li>
<li><a href="/.well-known/ard.json"><code>/.well-known/ard.json</code></a> is the Agentic Resource Discovery manifest. It lists the MCP server, an agent skill at <a href="/{skill_path}"><code>/{skill_path}</code></a>, a bundle of the catalogues and the API description, and a bundle for each dataset that holds its files and the server that queries it, with representative queries for each. The same document is at <code>/.well-known/ai-catalog.json</code>, and <a href="/.well-known/api-catalog"><code>/.well-known/api-catalog</code></a> lists the APIs as RFC 9727 asks.</li>
<li><a href="/catalog.json"><code>/catalog.json</code></a> is a DCAT catalogue in JSON-LD, one record per live dataset with a distribution per format.</li>
<li><a href="/openapi.json"><code>/openapi.json</code></a> is an OpenAPI 3.1 document for every public path, with the dataset slugs as an enum. Load it into any client generator or an agent's tool list.</li>
<li><a href="/backlog.json"><code>/backlog.json</code></a> is every register entry with its status, licence and the reason if it is blocked. <code>/api/v1/votes</code> has the current vote counts.</li>
</ul>
<h2>Every page has a Markdown twin</h2>
<p>Add <code>index.md</code> to any page URL, or follow the <code>rel="alternate" type="text/markdown"</code> link in its head. The twin has the same facts as the page in plain Markdown with front matter.</p>
<h2>Per dataset</h2>
<ul>
<li><code>/d/&lt;slug&gt;/datapackage.json</code> is a Frictionless data package pointing at the latest version.</li>
<li><code>/d/&lt;slug&gt;/schema.json</code> is a Table Schema. Types are string, integer, number, boolean, date and datetime. Beside each version, <code>schema.sql</code> is the same as a CREATE TABLE and <code>data.csv-metadata.json</code> is W3C CSV on the Web metadata.</li>
<li><code>/d/&lt;slug&gt;/fields.json</code> lists each queryable field with its type, the publisher's description, its range and the values it holds when it has few. The MCP server offers it as a resource.</li>
<li><code>/d/&lt;slug&gt;/versions.json</code> lists every version with its date, row count, source hash and URL.</li>
<li><code>/d/&lt;slug&gt;/changes.json</code> summarises each consecutive diff. <code>/d/&lt;slug&gt;/diff/&lt;a&gt;..&lt;b&gt;.json</code> compares two consecutive versions by key.</li>
<li><code>/d/&lt;slug&gt;/latest/data.&lt;format&gt;</code> redirects with a 302 to the newest dated version. Follow redirects.</li>
<li><code>/d/&lt;slug&gt;/v/&lt;date&gt;/data.&lt;format&gt;</code> keeps its content and is cached for a year. Formats: csv, csv.gz, ndjson, parquet and duckdb on every version, with xlsx, json and sqlite while the table is within their size limits. A dataset with coordinates or shapes adds gpkg, geo.parquet for points and geojson within its size limit, and a boundary layer adds pmtiles vector tiles. Versions whose manifest has no caps field were fetched before the size limits and also carry arrow. A version page says why a format is not there.</li>
<li><code>/d/&lt;slug&gt;/v/&lt;date&gt;/by/&lt;field&gt;/&lt;value&gt;.json</code> is a smaller file for one value of a partition field. <code>by/&lt;field&gt;/index.json</code> lists them.</li>
<li><code>/d/&lt;slug&gt;/v/&lt;date&gt;/SHA256SUMS</code> lists the SHA-256 of every file in the version, each under the name it downloads as, such as <code>&lt;slug&gt;_&lt;date&gt;.csv</code>. Run <code>sha256sum -c --ignore-missing SHA256SUMS</code> beside the files (<code>shasum -a 256 -c --ignore-missing SHA256SUMS</code> on a Mac), or save them with <code>curl -OJ</code> so the names match. Each list carries a GitHub artifact attestation from the deploy that wrote it, which <code>gh attestation verify SHA256SUMS --repo National-Digital/publicdata.au --source-ref refs/heads/main</code> checks.</li>
</ul>
<h2>Inside every data file</h2>
<p>JSON, NDJSON, GeoJSON, Parquet and SQLite each carry a <code>publicdata</code> header with the publisher, licence, attribution string, a <code>cite</code> string, version, source URL and source SHA-256. The CSV has no room for a header, so read <code>manifest.json</code> beside it.</p>
<h2>When you show the data to a person</h2>
<p>Use the publisher's attribution string, which the licence requires. Then say the file came from publicdata.au and link to the version URL, which we ask for. The <code>cite</code> string in the header does both in one sentence, and each dataset page has the same sentence as HTML, Markdown and BibTeX.</p>
<h2 id="mcp">MCP server</h2>
{mcp}
<h2>In the browser</h2>
{webmcp}
<h2>Robots</h2>
<p><a href="/robots.txt"><code>/robots.txt</code></a> allows every crawler, including AI crawlers, everywhere except <code>/api/</code>. <a href="/sitemap.xml"><code>/sitemap.xml</code></a> is a sitemap index with one sitemap per government and one for the other pages.</p>
""",
    ),
    "about": (
        "About this site",
        "Who runs publicdata.au, why, and how to get something fixed.",
        """
<p>publicdata.au is built and operated by <a href="https://nationaldigital.com.au/">National Digital</a>, a software company in Burleigh Heads, Queensland. It is a private website. No government agency runs it, funds it or has endorsed it.</p>
<h2>Why it exists</h2>
<p>Australian governments publish a great deal of data under Creative Commons licences, and most of it arrives as a CSV or an Excel file on a portal. The file is replaced when the next release comes out, the URL often changes, and the history is gone. Anyone who wants JSON, Parquet or a database has to convert it themselves, and anyone who wants last year's release has to have kept a copy.</p>
<p>This site does that once, for everyone. Each release becomes a dated version at a URL that never changes. Each version carries its schema, its provenance and a diff against the release before. The publisher's own file sits beside it.</p>
<h2>What it does to the data</h2>
<p>Cells are typed. Headers are renamed to snake_case, with the original header kept in the schema. The encoding is made UTF-8. Blank cells become null. A cell the publisher suppressed, such as "&lt;5", becomes null with a flag that says so. Rows are never added, removed, ranked, joined or summarised.</p>
<h2>Licences</h2>
<p>Each dataset is republished under the licence its publisher chose, and every file carries the attribution the licence asks for. A dataset whose licence does not allow derivative works is listed as blocked and never built. A dataset with no licence stated waits until the publisher confirms one.</p>
<p>The data licences are separate from the site's code, and stay that way. The code is open source under the GNU Affero General Public License, on <a href="{repo}">GitHub</a>, and the <a href="/contribute/">contributing page</a> explains how to add a dataset or improve the site.</p>
<h2 id="corrections">Corrections</h2>
<p>If a value is wrong, it is almost always wrong in the publisher's file as well, because this site publishes what the publisher publishes. Send those to the publisher, whose contact is on each dataset page.</p>
<p>If the serialisation is wrong, for example a column typed badly or a row missing, <a href="{repo}/issues/new?template=data-problem.yml">open an issue on GitHub</a> or <a href="https://nationaldigital.com.au/contact/">tell National Digital</a>. We fix it, publish a new build, and record the correction in the version's notes. A dated version keeps the same content once it is published. Its files change only to correct a fault in our conversion or in the publisher's attribution, to comply with the law, or when a publisher asks us to remove its dataset, and the change is recorded in that version's notes. The <a href="{repo}/blob/main/docs/CORRECTIONS.md">corrections policy</a> sets out each step and keeps a log of every correction.</p>
<h2 id="cite">Citing the files</h2>
<p>The licence on each dataset requires the publisher's attribution, and it is inside every file. We ask for one thing more: say that the file came from publicdata.au and link to the version you used. The link lets a reader fetch the same data, and it is how other people find this site. Every dataset page has the sentence ready to copy as text, HTML, Markdown and BibTeX.</p>
<h2>Privacy</h2>
<p>The site sets no cookies. Page views are counted by Cloudflare Web Analytics, which is served from this site's own provider, stores nothing in the browser and does not follow anyone across sites. There is no other tracking. Votes in the backlog are counted once per browser per day using a salted hash that changes every day. Nobody is asked who they are. The <a href="/privacy/">privacy page</a> sets out everything the site records.</p>
<h2>Security</h2>
<p>The disclosure policy is at <a href="/.well-known/security.txt"><code>/.well-known/security.txt</code></a>.</p>
""",
    ),
    "contribute": (
        "Contribute",
        "How to add a dataset, a file format or a fix to publicdata.au, and how a change is reviewed and released.",
        """
<p>The code that builds publicdata.au is open source. It is on GitHub under the GNU Affero General Public License, and anyone can propose a change to it. A maintainer at National Digital reviews every pull request, and a merged change goes live with the next release.</p>
<p><a class="gh" href="{repo}">{gh}National-Digital/publicdata.au</a></p>
<h2 id="add-a-dataset">Add a dataset</h2>
<p>Any dataset in the <a href="/backlog/">backlog</a> with an open licence can be added by anyone. A dataset is one YAML file in the <code>register/</code> folder, which names the source, the licence with the publisher's own statement as evidence, the attribution and the fields to publish. <code>python -m publicdata register draft</code> writes a first draft from the dataset's portal page. You finish it by hand and build it locally to check it, and the <a href="{repo}/blob/main/CONTRIBUTING.md#add-a-dataset">contributing guide</a> has the steps.</p>
<p>The licence is the only thing that stops a dataset. A non-commercial or no-derivatives licence, or none at all, means it cannot be published here, however useful it is.</p>
<h2 id="add-a-format">Add a file format</h2>
<p>Each format is a writer that takes one normalised table and its provenance and returns the bytes of the file. A writer reads no network, clock or random value, so two builds of one version give identical files. A new format is added to every dataset at the next deploy. The guide's section on <a href="{repo}/blob/main/CONTRIBUTING.md#add-a-serialisation">serialisation</a> lists what a writer needs.</p>
<h2 id="add-an-adapter">Read a new kind of portal</h2>
<p>A source adapter fetches from one kind of portal or file host. It reads the licence on every run and dates each version by the publisher's own change date. Datasets on a portal the fetch cannot read yet wait in the backlog, so one adapter can open up many of them. The guide explains <a href="{repo}/blob/main/CONTRIBUTING.md#add-a-source-adapter">how to add one</a>.</p>
<h2 id="fix">Fix something</h2>
<p>Every dataset page links to the register entry it is built from, so a clearer description or a field the publisher renamed is a small pull request. Faults in the site, the query API, the MCP server or the Python and R clients go in <a href="{repo}/issues">the issues</a>, along with any file that differs from the publisher's own. Improvements to the documentation are as welcome as code.</p>
<h2>Without writing code</h2>
<p>Votes in the <a href="/backlog/">backlog</a> decide which datasets are built next. A report of a file that differs from the publisher's, or of a page that reads badly, helps as much as a pull request. A publisher that confirms a licence in writing can move a blocked dataset onto the list. Questions and ideas that are not a fault go in <a href="{repo}/discussions">the discussions</a>.</p>
<h2>How a change is accepted</h2>
<p>Sign off each commit with <code>git commit -s</code>, which certifies under the <a href="https://developercertificate.org/">Developer Certificate of Origin</a> that you may submit it. Title the pull request as a Conventional Commit, such as <code>data(register): add &lt;what it is&gt;</code>. The checks build and test the site and need no credentials, so they run on a pull request from a fork. A maintainer then reviews it and squash-merges it, and the release notes on GitHub name the people whose changes each release carries.</p>
<p>The rules every change is held to are in <a href="{repo}/blob/main/CONTRIBUTING.md#ground-rules">the guide</a>. The site publishes what the publisher published and derives nothing from it, and a version keeps its content once it is out. By taking part you agree to the <a href="{repo}/blob/main/CODE_OF_CONDUCT.md">code of conduct</a>. Report a security issue privately, as the <a href="{repo}/blob/main/SECURITY.md">security policy</a> describes.</p>
<h2>Licences</h2>
<p>Each dataset stays under its publisher's licence. The code is under the AGPL and the register's own text is under CC BY 4.0. The name publicdata.au and its mark are outside both licences, and <a href="{repo}/blob/main/BRAND.md">BRAND.md</a> says what a copy of the site may use.</p>
""",
    ),
    "privacy": (
        "Privacy",
        "What publicdata.au records about the people and agents that use it.",
        """
<p>publicdata.au has no accounts and sets no cookies. Nobody is asked who they are, on the pages, in the query API or through the MCP server. The site is run by <a href="https://nationaldigital.com.au/">National Digital</a>.</p>
<h2>Page views</h2>
<p>Cloudflare Web Analytics counts page views. It is served from this site's own provider, stores nothing in the browser and does not follow anyone across sites. There is no other tracking.</p>
<h2>Votes and dataset requests</h2>
<p>A vote is stored under a salted hash of the date, the dataset, the address and the browser's user agent, so each browser counts once a day. The salt stays in a private store and is never published, and the date is part of the hash, so the published counts cannot be used to link votes on different days. Only the total for each dataset is published.</p>
<p>A link pasted into the backlog is looked up in the catalogue and is not stored. When it names a dataset there, it counts as a vote for that dataset in the same way. When it does not, the page suggests sending it to National Digital, which is up to you.</p>
<h2>The query API and the MCP server</h2>
<p>The query API answers from the data and records nothing about who asked. {mcp_privacy}</p>
<h2>Hosting</h2>
<p>Cloudflare hosts the site and handles every request, including the address it came from, to deliver it and to block abuse. The <a href="https://www.cloudflare.com/privacypolicy/">Cloudflare privacy policy</a> covers that handling.</p>
<h2>Contact</h2>
<p>Questions about privacy go to <a href="https://nationaldigital.com.au/contact/">National Digital</a>. Security reports go to the address in <a href="/.well-known/security.txt"><code>/.well-known/security.txt</code></a>.</p>
""",
    ),
    "terms": (
        "Terms of use",
        "The terms for using publicdata.au: its pages, data files, query API and MCP server.",
        """
<p>These terms cover publicdata.au: its pages, the data files, the query API and the MCP server at <code>/mcp</code>. The site is run by <a href="https://nationaldigital.com.au/">National Digital</a>. They apply to anyone who uses the site, including through a program or an AI agent, and if a program uses the site for you, you are responsible for how it uses it. If you do not accept these terms, do not use the site.</p>
<h2>The data</h2>
<p>Each dataset belongs to its publisher and is republished under the licence the publisher chose, which is named on the dataset page and inside every file. That licence governs what you may do with the data, and these terms do not narrow it.</p>
<p>Most datasets are under a Creative Commons Attribution licence. When you share the data, or something made from it, the licence requires you to credit the publisher in a reasonable way, and the attribution string in every file does this in the publisher's words. We also ask you to say the data came from publicdata.au and link to the version you used. The licence does not require this, and you are free to decline. The <code>cite</code> string in every file does both.</p>
<h2>The site's code and text</h2>
<p>The code that builds and runs the site is open source under the <a href="https://www.gnu.org/licenses/agpl-3.0.html">GNU Affero General Public License v3.0 or later</a>, and its source is <a href="{repo}">on GitHub</a>. If you run a modified copy as a service, the licence requires you to offer its users the source of your changes. The text we write for each dataset's register entry is under <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>. The names publicdata.au and National Digital, and their marks, are outside both licences, and <a href="{repo}/blob/main/BRAND.md">BRAND.md</a> says what a copy of the site must change. The rest of the site's own text belongs to National Digital.</p>
<h2>No endorsement</h2>
<p>publicdata.au is a private website with no connection to any government agency. No publisher has endorsed it, and nothing on it speaks for a publisher.</p>
<h2>Accuracy</h2>
<p>The site republishes each publisher's file in other formats. We type each cell and rename headers, and a cell the publisher suppressed, such as "&lt;5", becomes null with a flag, as the <a href="/about/">about page</a> sets out. A value that is wrong in the publisher's file is wrong here too. Before a version is published, an automated check confirms that every format was written and that each file carries the licence and the publisher's attribution. Nothing compares each value with the publisher's file.</p>
<p>Counts, sums and averages from the query API and the MCP server leave out null cells, so a total can be lower than the publisher's own. We give no warranty that the data, or an answer a program or an AI agent draws from it, is complete or accurate, or that it is current or suits your purpose. The publisher's original file is kept beside every version with its SHA-256. Check anything that matters against that file or the publisher's current release. This does not affect your rights under the Australian Consumer Law, as the liability section sets out.</p>
<h2>Using the query API and the MCP server</h2>
<p>Programs, scripts and AI agents may use the site, the files, the query API and the MCP server. {terms_limits} Do not try to get around a limit, for example by spreading requests across many addresses, and do not use the site in a way that slows it for other people. We may slow or block traffic that does. These limits apply to our servers. What you may do with data you already hold is governed by the dataset's licence. <a href="/robots.txt"><code>/robots.txt</code></a> asks crawlers not to index <code>/api/</code>, and it does not stop a program from using the API.</p>
<h2>Votes and dataset requests</h2>
<p>A vote is stored under a salted hash of your address and browser, and no name or account is attached to it. The <a href="/privacy/">privacy page</a> sets out how this works. A vote asks for a dataset to be built sooner and does not oblige us to build it.</p>
<h2>Versions and changes</h2>
<p>A dated version keeps the same content once it is published. We change the files at a version URL only to correct a fault in our conversion or in the publisher's attribution, to comply with the law, or when a publisher asks us to remove its dataset, and we record the change in that version's notes.</p>
<p>The site is free and may be unavailable at times. Paths other than dated versions may change, and so may the tools. We may change these terms. A change applies only to use after the date it was made, and these terms were last changed on {terms_changed}.</p>
<h2>Liability</h2>
<p>To the extent the law allows, National Digital is not liable for any loss or damage that arises from using the site, the data, the query API or the MCP server. This includes loss caused by our negligence and loss from an answer a program or an AI agent gives from the data.</p>
<p>You may have rights under the Australian Consumer Law that these terms cannot exclude, restrict or modify, and these terms do not affect them. Where the law lets us limit our liability for a breach of a consumer guarantee, our liability is limited to supplying the service again.</p>
<h2>Governing law</h2>
<p>These terms are governed by the law of Queensland. The courts of Queensland, and federal courts sitting in Queensland, may hear any dispute about them. This does not stop you from relying on the Australian Consumer Law. If part of these terms cannot be enforced, the rest still applies.</p>
<h2>Concerns and contact</h2>
<p>To raise a copyright or privacy concern about a dataset, or to ask us to remove one, write to <a href="https://nationaldigital.com.au/contact/">National Digital</a>. Errors in the serialisation go through <a href="/about/#corrections">corrections</a>.</p>
""",
    ),
}


# The date the terms last changed, and a hash of their text. The gate fails when the text changes
# and the hash does not, so the date on the page cannot fall behind the wording.
TERMS_CHANGED = (
    "8 October 2026",
    "9c980b65ff9a94242796d36470ef0ea4558f56c1ab299c5829c6d2443003bfec",
)


def terms_hash() -> str:
    return hashlib.sha256(PROSE["terms"][2].encode("utf-8")).hexdigest()


FILTER_HELP = at.filter_help()


API_SLOT = "/*API_SPEC*/null"

# The public half of the key that publishes server.json to the MCP Registry, which the registry
# reads from /.well-known/mcp-registry-auth. The private half is kept outside the repository.
MCP_REGISTRY_KEY = "D+bNodSgVutRDCBCLQTUim6e900TD+4Fy1tPz9LSokk="
# Glama reads this from /.well-known/glama.json to hand its connector listing to us.
GLAMA_CLAIM = "glama_claim_6RT87z1LNOY2Um2-9t5ats8O7u6Nyz4w"
# OpenAI reads this from /.well-known/openai-apps-challenge to verify the domain of our ChatGPT app.
OPENAI_APPS_CHALLENGE = "WMdjLxFbqZsMxKB4xz4o3sAnuyJu1zFwN8jcBnjG3aw"


def _api_doc() -> dict:
    """The query API's help on each dataset page, as HTML."""
    S = at.spec()
    A = S["api"]
    return {
        "limits": S["limits"],
        "summary": at.as_html(A["summary"], "mono"),
        "filters": at.as_html(A["filters"], "mono"),
        "operators": [
            (o["syntax"], at.as_html(o["description"], "mono")) for o in S["operators"].values()
        ],
        "params": [(n, at.as_html(at.param_text(n), "mono")) for n in at.query_params()],
        "versions": at.as_html(A["versions"], "mono"),
        "rate_limit": at.as_html(A["rate_limit"], "mono"),
        "mcp": at.as_html(S["mcp"]["intro"], "mono"),
    }


def _agents_query_api() -> str:
    A = at.spec()["api"]
    rows = "/api/v1/datasets/au-road-deaths/rows?year=eq.2025&amp;state=eq.QLD"
    agg = "/api/v1/datasets/au-road-deaths/aggregate?group=state&amp;metric=count&amp;year=eq.2025"
    return (
        f'{at.as_html(A["rows"])} For example <a href="{rows}&amp;select=month,road_user,age&amp;limit=5"><code>{rows}</code></a>. '
        f'{at.as_html(A["aggregate"])} For example <a href="{agg}"><code>?group=state&amp;metric=count&amp;year=eq.2025</code></a>. '
        f"{at.as_html(A['versions'])} {at.as_html(at.filter_help())} {at.as_html(A['provenance'])} "
        f"<code>/openapi.json</code> describes every path, and each dataset's <code>/d/&lt;slug&gt;/openapi.json</code> lists its fields as parameters. {at.as_html(A['rate_limit'])}"
    )


def _agents_webmcp() -> str:
    names = [f'<a href="#tool-{n}"><code>{n}</code></a>' for n in at.tool_names()]
    return (
        f"<p>{at.as_html(at.spec()['webmcp']['intro'])}</p>\n"
        f"<p>The tools are {', '.join(names[:-1])} and {names[-1]}.</p>"
    )


def _agents_mcp() -> str:
    url = SITE + "/mcp"
    vscode = "vscode:mcp/install?" + urllib.parse.quote(
        json.dumps({"name": "publicdata", "type": "http", "url": url}, separators=(",", ":"))
    )
    cursor = "cursor://anysphere.cursor-deeplink/mcp/install?" + urllib.parse.urlencode(
        {
            "name": "publicdata",
            "config": base64.b64encode(json.dumps({"url": url}).encode()).decode(),
        }
    )
    return (
        f"<p>{at.as_html(at.spec()['mcp']['intro'])} {at.prompts_note()} {at.listed_note()}</p>\n"
        f'<p><a href="{html.escape(vscode)}">Add to VS Code</a> · <a href="{html.escape(cursor)}">Add to Cursor</a> · '
        '<a href="/mcp/server-card"><code>/mcp/server-card</code></a> describes the server for directories.</p>\n'
        + _agents_tools()
    )


def _agents_tools() -> str:
    """Each tool's name, title and first line, with an anchor the home page's tool names link to."""
    tools = at.spec()["webmcp"]["tools"]
    items = "".join(
        f'<dt id="tool-{n}"><code>{n}</code> {html.escape(t["title"])}</dt>'
        f"<dd>{at.as_html(t['description'].splitlines()[0])}</dd>"
        for n, t in tools.items()
    )
    return f'<dl class="tools-list">{items}</dl>'


def _query_paths(
    live: list[DatasetOut], slug_p: dict, hints: dict | None = None, generic: bool = False
) -> dict:
    """One rows path per live dataset with its fields as typed filters, and one aggregate path."""
    hints = hints or {}

    def hint(name: str) -> str:
        e = hints.get(name) or {}
        if e.get("values") and len(e["values"]) <= 20:
            return " Values: " + ", ".join(str(v) for v in e["values"]) + "."
        if "min" in e:
            return f" From {e['min']} to {e['max']}."
        return ""

    S = at.spec()
    lim, R, P = S["limits"], S["responses"], S["paths"]
    schemas = {
        "select": {"type": "string"},
        "order": {"type": "string"},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": lim["limit_max"],
            "default": lim["limit_default"],
        },
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "format": {"type": "string", "enum": ["json", "ndjson", "csv"], "default": "json"},
        "group": {"type": "string"},
        "metric": {"type": "string", "default": "count"},
    }

    def param(name: str) -> dict:
        return {
            "name": name,
            "in": "query",
            "schema": schemas[name],
            "description": at.param_text(name),
        }

    common = [param(n) for n in ("select", "order", "limit", "offset", "format")]
    ver_p = {
        "name": "version",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "date"},
        "description": at.param_text("version"),
    }
    ok = {
        "200": {"description": R["200"]},
        "400": {"description": R["400"]},
        "404": {"description": R["404"]},
        "429": {
            "description": R["429"],
            "headers": {
                "Retry-After": {
                    "schema": {"type": "integer"},
                    "description": R["retry_after"],
                },
                "RateLimit-Policy": {
                    "schema": {"type": "string"},
                    "description": R["ratelimit_policy"],
                },
            },
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "error": {"type": "string"},
                            "status": {"type": "integer", "const": 429},
                            "limit": {"type": "integer"},
                            "window_seconds": {"type": "integer"},
                            "block_seconds": {"type": "integer"},
                            "docs": {"type": "string", "format": "uri"},
                            "files": {"type": "string"},
                        },
                    }
                }
            },
        },
    }
    paths = {}
    filters = []
    if generic:
        # The site's document stays one size whatever the number of datasets; each dataset's own
        # document names its fields as filters.
        for path, op_id, extra, when in (
            ("/api/v1/datasets/{slug}/rows", "queryRows", [], "the newest loaded version"),
            (
                "/api/v1/datasets/{slug}/versions/{version}/rows",
                "queryRowsVersion",
                [ver_p],
                "one dated version",
            ),
        ):
            paths[path] = {
                "get": {
                    "tags": ["query"],
                    "summary": at.fill(P["rows"]["summary"], {"title": "any dataset"}),
                    "operationId": op_id,
                    "description": at.fill(
                        P["rows"]["description"], {"title": "a dataset", "when": when}
                    )
                    + " "
                    + FILTER_HELP
                    + f" Each field is a filter. {SITE}/d/<slug>/openapi.json lists a dataset's"
                    " fields with their types, ranges and values.",
                    "parameters": [slug_p, *extra, *common],
                    "responses": ok,
                }
            }
    for o in [] if generic else live:
        ds = o.dataset
        op = re.sub(r"[^A-Za-z0-9]", "_", ds.slug)
        filters = [
            {
                "name": f.name,
                "in": "query",
                "schema": {"type": "string"},
                "description": f"{f.type}. {f.description}".strip() + hint(f.name),
                **(
                    {"examples": [f"eq.{hints[f.name]['values'][-1]}"]}
                    if hints.get(f.name, {}).get("values")
                    else {}
                ),
            }
            for f in ds.fields
        ]
        for path, suffix, extra, when in (
            (f"/api/v1/datasets/{ds.slug}/rows", "", [], "the newest loaded version"),
            (
                f"/api/v1/datasets/{ds.slug}/versions/{{version}}/rows",
                "_version",
                [ver_p],
                "one dated version",
            ),
        ):
            paths[path] = {
                "get": {
                    "tags": ["query"],
                    "summary": at.fill(P["rows"]["summary"], {"title": ds.title}),
                    "operationId": f"queryRows_{op}{suffix}",
                    "description": at.fill(
                        P["rows"]["description"], {"title": ds.title, "when": when}
                    )
                    + " "
                    + FILTER_HELP,
                    "parameters": extra + filters + common,
                    "responses": ok,
                }
            }
    # One dataset's document can name its fields on aggregate as well; the site's cannot.
    agg = [param("group"), param("metric"), *(filters if len(live) == 1 else []), *common[1:]]
    for path, op_id, extra, when in (
        ("/api/v1/datasets/{slug}/aggregate", "aggregate", [], "the newest loaded version"),
        (
            "/api/v1/datasets/{slug}/versions/{version}/aggregate",
            "aggregateVersion",
            [ver_p],
            "one dated version",
        ),
    ):
        paths[path] = {
            "get": {
                "tags": ["query"],
                "summary": P["aggregate"]["summary"],
                "operationId": op_id,
                "description": at.fill(P["aggregate"]["description"], {"when": when})
                + " "
                + FILTER_HELP,
                "parameters": [slug_p, *extra, *agg],
                "responses": ok,
            }
        }
    paths["/api/v1/datasets/{slug}/versions"] = {
        "get": {
            "tags": ["query"],
            "summary": P["versions"]["summary"],
            "operationId": "queryVersions",
            "parameters": [slug_p],
            "responses": {
                "200": {"description": R["200_versions"]},
                "404": ok["404"],
                "429": ok["429"],
            },
        }
    }
    return paths


HINT_VALUES = 150  # a field with more distinct values than this gets no value list


def _fields_resource(o: DatasetOut, console: dict) -> dict:
    """A dataset's fields as the MCP server's resource: what the query console knows, as data."""
    ds, m = o.dataset, o.latest.manifest
    api = f"{SITE}/api/v1/datasets/{ds.slug}/"
    return {
        "slug": ds.slug,
        "title": ds.title,
        "publisher": ds.publisher.name,
        "licence": ds.licence.id,
        "attribution": attribution(ds, m),
        "version": m.version,
        "rows": o.latest.rows,
        "dataset_page": dataset_url(ds.slug),
        "rows_url": api + "rows",
        "aggregate_url": api + "aggregate",
        "where": at.plain(at.spec()["webmcp"]["where"]),
        "key": list(ds.key),
        "partition_by": list(ds.partition_by),
        "fields": console["fields"],
    }


def _console(ds: Dataset, parquet: Path) -> dict:
    """Fields with value hints and a first query for the dataset page's query console, read from
    the same rows the query API is loaded from."""
    con = connect(parquet, [f.name for f in ds.fields])
    cols = set(con.columns())
    names = [f.name for f in ds.fields if f.name in cols]
    q = lambda n: '"' + n + '"'  # noqa: E731
    stats = con.execute(
        "SELECT "
        + ", ".join(f"COUNT(DISTINCT {q(n)}), MIN({q(n)}), MAX({q(n)})" for n in names)
        + " FROM records"
    ).fetchone()
    fields = []
    for i, f in enumerate(f for f in ds.fields if f.name in cols):
        distinct, lo, hi = stats[i * 3 : i * 3 + 3]
        e = {"name": f.name, "type": f.type}
        if f.description:
            e["description"] = f.description
        if f.type in ("integer", "number", "date", "datetime") and lo is not None:
            e["min"], e["max"] = lo, hi
        if f.type != "boolean" and 0 < distinct <= HINT_VALUES:
            e["values"] = [
                r[0]
                for r in con.execute(
                    f"SELECT DISTINCT {q(f.name)} FROM records WHERE {q(f.name)} IS NOT NULL ORDER BY 1"
                )
            ]
        e["distinct"] = distinct
        fields.append(e)
    by = {e["name"]: e for e in fields}
    try:
        example = _register_example(ds, con, by) if ds.example else _picked_example(ds, con, fields)
    finally:
        con.close()
    for e in fields:
        del e["distinct"]
    return {"fields": fields, "example": example}


def _register_example(ds: Dataset, con, by: dict) -> dict:
    """The register's example, with each newest standing for the field's newest value."""
    ex = ds.example
    measured = [ex["metric"].split(".", 1)[1]] if ex["metric"] != "count" else []
    for name in (*(f["field"] for f in ex["filters"]), *ex["group"], *measured):
        if name not in by:
            raise ValueError(f"{ds.slug}: the example names {name}, which the version lacks")
    filters = []
    for f in ex["filters"]:
        value = f["value"]
        if value == NEWEST:
            value = figures.newest(con, f["field"])
        filters.append({**f, "value": str(value)})
    out = {"filters": filters, "group": list(ex["group"]), "metric": ex["metric"]}
    if ex["label"]:
        out["label"] = ex["label"]
    return out


# A field named as an identifier or a code is never the picked filter or the summed measure.
ID_NAME = re.compile(r"(^|_)(id|no|number|code)$")


def _picked_example(ds: Dataset, con, fields: list[dict]) -> dict:
    """A first query from the field statistics, for an entry whose register names none: a filter
    on the partition field or a short list of values, a group with more than one value inside it,
    and a count, or the count field's sum for a table of counts."""
    q = lambda n: '"' + n + '"'  # noqa: E731
    by = {e["name"]: e for e in fields}
    unique = set(ds.key) if len(ds.key) == 1 else set()
    # A measure or an identifier makes a filter that matches a row or two.
    listed = (
        e["name"]
        for e in fields
        if e.get("values")
        and e["type"] != "number"
        and e["name"] not in unique
        and not ID_NAME.search(e["name"])
    )
    first = next((n for n in ds.partition_by if n in by), None) or next(listed, None)
    filters, cond, params = [], "", []
    if first:
        e = by[first]
        # The newest value with a page of rows, so the first run shows something.
        newest = e["type"] in ("integer", "number", "date", "datetime")
        row = con.execute(
            f"SELECT {q(first)} FROM records WHERE {q(first)} IS NOT NULL GROUP BY 1"
            f" HAVING COUNT(*) >= 20 ORDER BY {'1 DESC' if newest else 'COUNT(*) DESC, 1'} LIMIT 1"
        ).fetchone()
        if row:
            filters.append({"field": first, "op": "eq", "value": str(row[0])})
            cond, params = f" WHERE {q(first)} = ?", [con.param(first, row[0])]

    def splits(n: str) -> bool:
        # A group that is one value under the filter, such as a state's name under its code,
        # answers with a single bar.
        return (
            con.execute(f"SELECT COUNT(DISTINCT {q(n)}) FROM records{cond}", params).fetchone()[0]
            >= 2
        )

    # A field with a value per row, such as a name or an identifier, splits into ones.
    total = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    strings = [
        e
        for e in fields
        if e["type"] == "string"
        and e["name"] != first
        and e["distinct"] < total
        and not ID_NAME.search(e["name"])
    ]
    group = next(
        (e["name"] for e in strings if 2 <= e["distinct"] <= 30 and splits(e["name"])), None
    ) or next((e["name"] for e in strings if e.get("values") and splits(e["name"])), None)
    # A table of counts, keyed by several dimensions, is summed; a table of events is counted.
    measures = [
        e["name"]
        for e in fields
        if e["type"] == "integer"
        and e["name"] not in (*ds.key, *ds.partition_by)
        and not ID_NAME.search(e["name"])
    ]
    tally = len(ds.key) > 1 and next(
        (n for n in measures if re.search(r"(^|_)count(_|$)", n)), measures[0] if measures else None
    )
    return {
        # With nothing to split inside the filter, the filter's own field is the split, so the
        # tile answers with its values instead of nothing.
        "filters": filters if group or not first else [],
        "group": [group or first] if group or first else [],
        "metric": f"sum.{tally}" if tally else "count",
    }


def _example_query(slug: str, console: dict, op: str) -> str:
    ex = console["example"]
    # A value such as "% Total" or "A&E" is quoted whole, so the URL keeps its meaning.
    parts = [
        f"{f['field']}={f['op']}.{urllib.parse.quote(str(f['value']), safe='')}"
        for f in ex["filters"]
    ]
    if op == "aggregate":
        parts = (
            ([f"group={','.join(ex['group'])}"] if ex["group"] else [])
            + [f"metric={ex['metric']}"]
            + parts
        )
    else:
        parts.append("limit=5")
    return f"/api/v1/datasets/{slug}/{op}?" + "&".join(p.replace(" ", "%20") for p in parts)


def _dataset_openapi(o: DatasetOut, console: dict) -> dict:
    """OpenAPI 3.1 for one dataset's query paths, with each field's values or range."""
    ds = o.dataset
    slug_p = {
        "name": "slug",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "enum": [ds.slug]},
        "description": at.param_text("slug"),
    }
    paths = _query_paths([o], slug_p, {e["name"]: e for e in console["fields"]})
    A = at.spec()["api"]
    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"{ds.title} query API",
            "version": o.latest.manifest.version,
            "summary": f"Filter, page and aggregate {ds.title} from {ds.publisher.name}.",
            "description": f"{A['summary']} {A['rate_limit']} {A['provenance']} {ds.publisher.name} has not endorsed this site. The whole site is described in {SITE}/openapi.json.",
            "license": {"name": ds.licence.title, "url": ds.licence.url},
        },
        "servers": [{"url": SITE}],
        "externalDocs": {"url": dataset_url(ds.slug) + "#query"},
        "tags": [{"name": "query"}],
        "paths": paths,
    }


def _openapi(live: list[DatasetOut], queried: list[DatasetOut]) -> dict:
    """OpenAPI 3.1 for every public path. The dataset slugs are an enum so tools can validate."""
    slugs = [o.dataset.slug for o in live]
    versions = sorted({v.manifest.version for o in live for v in o.versions})
    fmts = list(SITE_ORDER)
    prov = {
        "type": "object",
        "description": "Provenance header carried inside every JSON, NDJSON, GeoJSON, Parquet and SQLite file.",
        "properties": {
            "site": {"type": "string"},
            "dataset": {"type": "string"},
            "title": {"type": "string"},
            "version": {"type": "string", "format": "date"},
            "as_at": {"type": ["string", "null"], "format": "date"},
            "url": {"type": "string", "format": "uri"},
            "publisher": {"type": "object"},
            "licence": {"type": "object"},
            "attribution": {"type": "string"},
            "source": {"type": "object"},
            "rows": {"type": "integer"},
            "fields": {"type": "integer"},
            "not_endorsed": {"type": "string"},
        },
    }
    slug_p = {
        "name": "slug",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "enum": slugs},
        "description": at.param_text("slug"),
    }
    ver_p = {
        "name": "version",
        "in": "path",
        "required": True,
        "description": "A dated version from versions.json.",
        "schema": {"type": "string", "format": "date", "examples": versions[-1:]},
    }
    fmt_p = {
        "name": "format",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "enum": fmts},
        "description": _file_formats_text(),
    }

    def j(desc, schema=None):
        return {
            "description": desc,
            "content": {"application/json": {"schema": schema or {"type": "object"}}},
        }

    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": HOST,
            "version": at.release(),
            "summary": "Australian government open data as dated versions that keep their content.",
            "description": "Every path is public, with no keys or accounts. Files have no download limit. "
            + at.spec()["api"]["rate_limit"]
            + " Files under /v/<date>/ keep their content and change only for the reasons in /terms/. "
            "Every JSON, NDJSON, GeoJSON, Parquet and SQLite file carries a publicdata provenance header. "
            "No government agency runs or has endorsed this site.",
            "contact": {"name": OPERATOR, "url": "https://nationaldigital.com.au/contact/"},
            "license": {
                "name": "Data under each publisher's licence, see backlog.json",
                "url": f"{SITE}/about/",
            },
        },
        "servers": [{"url": SITE}],
        "tags": [
            {"name": "catalogue"},
            {"name": "dataset"},
            {"name": "version"},
            {"name": "backlog"},
            {"name": "explorer"},
        ],
        "paths": {
            "/catalog.json": {
                "get": {
                    "tags": ["catalogue"],
                    "summary": "DCAT catalogue of every live dataset",
                    "operationId": "getCatalog",
                    "responses": {
                        "200": {
                            "description": "DCAT-AP JSON-LD",
                            "content": {"application/ld+json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
            "/backlog.json": {
                "get": {
                    "tags": ["backlog"],
                    "summary": "Every register entry with status, licence and blocked reason",
                    "operationId": "getBacklog",
                    "responses": {
                        "200": j(
                            "Register entries",
                            {
                                "type": "object",
                                "properties": {
                                    "entries": {"type": "array", "items": {"type": "object"}}
                                },
                            },
                        )
                    },
                }
            },
            "/latest.json": {
                "get": {
                    "tags": ["catalogue"],
                    "summary": "Latest version date per dataset",
                    "operationId": "getLatest",
                    "responses": {
                        "200": j(
                            "slug to date",
                            {
                                "type": "object",
                                "additionalProperties": {"type": "string", "format": "date"},
                            },
                        )
                    },
                }
            },
            "/health.json": {
                "get": {
                    "tags": ["catalogue"],
                    "summary": "Build health",
                    "operationId": "getHealth",
                    "responses": {"200": j("ok")},
                }
            },
            "/d/{slug}/datapackage.json": {
                "get": {
                    "tags": ["dataset"],
                    "summary": "Frictionless data package pointing at the latest version",
                    "operationId": "getDatapackage",
                    "parameters": [slug_p],
                    "responses": {"200": j("Data package")},
                }
            },
            "/d/{slug}/schema.json": {
                "get": {
                    "tags": ["dataset"],
                    "summary": "Table Schema for every field",
                    "operationId": "getSchema",
                    "parameters": [slug_p],
                    "responses": {"200": j("Table Schema")},
                }
            },
            "/d/{slug}/versions.json": {
                "get": {
                    "tags": ["dataset"],
                    "summary": "Every version with date, rows, fields and source hash",
                    "operationId": "getVersions",
                    "parameters": [slug_p],
                    "responses": {"200": j("Versions")},
                }
            },
            "/d/{slug}/changes.json": {
                "get": {
                    "tags": ["dataset"],
                    "summary": "Diff summary for every consecutive pair of versions",
                    "operationId": "getChanges",
                    "parameters": [slug_p],
                    "responses": {"200": j("Changes")},
                }
            },
            "/d/{slug}/diff/{from}..{to}.json": {
                "get": {
                    "tags": ["dataset"],
                    "summary": "Rows added, removed and changed between two versions, by key",
                    "operationId": "getDiff",
                    "parameters": [
                        slug_p,
                        {
                            "name": "from",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "date"},
                        },
                        {
                            "name": "to",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "date"},
                        },
                    ],
                    "responses": {"200": j("Diff"), "404": {"description": "No such pair"}},
                }
            },
            "/d/{slug}/history.tar.zst": {
                "get": {
                    "tags": ["dataset"],
                    "summary": "Every version's Parquet and manifest in one archive",
                    "operationId": "getHistory",
                    "parameters": [slug_p],
                    "responses": {
                        "200": {"description": "zstd tar", "content": {"application/zstd": {}}}
                    },
                }
            },
            "/d/{slug}/index.md": {
                "get": {
                    "tags": ["dataset"],
                    "summary": "The dataset page as Markdown",
                    "operationId": "getDatasetMarkdown",
                    "parameters": [slug_p],
                    "responses": {
                        "200": {
                            "description": "Markdown with front matter",
                            "content": {"text/markdown": {}},
                        }
                    },
                }
            },
            "/d/{slug}/latest/data.{format}": {
                "get": {
                    "tags": ["version"],
                    "summary": "Redirects to the newest dated version of the file",
                    "operationId": "getLatestData",
                    "parameters": [slug_p, fmt_p],
                    "responses": {
                        "302": {
                            "description": "Location is the dated file. Cached 5 minutes.",
                            "headers": {
                                "Location": {"schema": {"type": "string", "format": "uri"}}
                            },
                        }
                    },
                }
            },
            "/d/{slug}/v/{version}/data.{format}": {
                "get": {
                    "tags": ["version"],
                    "summary": "The whole dataset in one format. Cached one year. Range requests are honoured.",
                    "operationId": "getData",
                    "parameters": [slug_p, ver_p, fmt_p],
                    "responses": {
                        "200": {"description": "The file", "content": {MEDIA[f]: {} for f in fmts}},
                        "206": {"description": "Partial content"},
                        "404": {"description": "No such version or format"},
                    },
                }
            },
            "/d/{slug}/v/{version}/manifest.json": {
                "get": {
                    "tags": ["version"],
                    "summary": "Where the bytes came from and when",
                    "operationId": "getManifest",
                    "parameters": [slug_p, ver_p],
                    "responses": {"200": j("Manifest")},
                }
            },
            "/d/{slug}/v/{version}/schema.json": {
                "get": {
                    "tags": ["version"],
                    "summary": "Table Schema as at this version",
                    "operationId": "getVersionSchema",
                    "parameters": [slug_p, ver_p],
                    "responses": {"200": j("Table Schema")},
                }
            },
            "/d/{slug}/v/{version}/source.{ext}": {
                "get": {
                    "tags": ["version"],
                    "summary": "The publisher's file, byte for byte",
                    "operationId": "getSource",
                    "parameters": [
                        slug_p,
                        ver_p,
                        {
                            "name": "ext",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "examples": ["csv"]},
                        },
                    ],
                    "responses": {"200": {"description": "The source bytes"}},
                }
            },
            "/d/{slug}/v/{version}/by/{field}/index.json": {
                "get": {
                    "tags": ["version"],
                    "summary": "Every partition file for one field, with row counts",
                    "operationId": "getPartitionIndex",
                    "parameters": [
                        slug_p,
                        ver_p,
                        {
                            "name": "field",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": j("Partition index")},
                }
            },
            "/d/{slug}/v/{version}/by/{field}/{value}.json": {
                "get": {
                    "tags": ["version"],
                    "summary": "The rows for one value of a partition field, with the provenance header",
                    "operationId": "getPartition",
                    "parameters": [
                        slug_p,
                        ver_p,
                        {
                            "name": "field",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "value",
                            "in": "path",
                            "required": True,
                            "description": "The slugified value from index.json",
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": j(
                            "Rows",
                            {
                                "type": "object",
                                "properties": {
                                    "publicdata": prov,
                                    "fields": {"type": "array"},
                                    "records": {"type": "array", "items": {"type": "object"}},
                                },
                            },
                        )
                    },
                }
            },
            "/api/v1/votes": {
                "get": {
                    "tags": ["backlog"],
                    "summary": "Vote counts per backlog dataset",
                    "operationId": "getVotes",
                    "responses": {
                        "200": j(
                            "slug to count",
                            {"type": "object", "additionalProperties": {"type": "integer"}},
                        )
                    },
                }
            },
            "/api/v1/votes/{slug}": {
                "get": {
                    "tags": ["backlog"],
                    "summary": "Vote count for one dataset",
                    "operationId": "getVote",
                    "parameters": [slug_p | {"schema": {"type": "string"}}],
                    "responses": {"200": j("Count")},
                },
                "post": {
                    "tags": ["backlog"],
                    "summary": "Add one vote. No identity is recorded; one per browser per day.",
                    "operationId": "vote",
                    "parameters": [slug_p | {"schema": {"type": "string"}}],
                    "responses": {
                        "200": j(
                            "New count",
                            {
                                "type": "object",
                                "properties": {
                                    "slug": {"type": "string"},
                                    "votes": {"type": "integer"},
                                },
                            },
                        ),
                        "404": {"description": "Not open for votes: live, building or unknown"},
                    },
                },
                "delete": {
                    "tags": ["backlog"],
                    "summary": "Remove today's vote from this browser",
                    "operationId": "unvote",
                    "parameters": [slug_p | {"schema": {"type": "string"}}],
                    "responses": {"200": j("New count")},
                },
            },
            "/api/v1/datasets": {
                "get": {
                    "tags": ["catalogue"],
                    "summary": "Search the datasets served here by title, description, publisher, keyword or field name",
                    "description": "The search behind the search_datasets tool. Words are matched to their stem, and up to 50 datasets come back, best match first.",
                    "operationId": "searchDatasets",
                    "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
                    "responses": {
                        "200": j(
                            "Matching datasets",
                            {
                                "type": "object",
                                "properties": {
                                    "results": {"type": "array", "items": {"type": "object"}}
                                },
                            },
                        ),
                        "429": {
                            "description": "Over the fair-use limit for one address. Wait Retry-After seconds."
                        },
                    },
                }
            },
            "/api/v1/catalogue": {
                "get": {
                    "tags": ["backlog"],
                    "summary": "Search every dataset listed on Australia's government open-data portals",
                    "description": "Words in q are matched against title, description and publisher, with plurals and other endings matched to their stem. Records that can take a vote come first. With ids, returns those records by id or vote key; with url, returns the record a portal URL names, or null. Each row's state is votable, chosen (planned in the register; its vote key is the register slug), served (it has a page here) or closed (no open licence or no file to read, with the reason).",
                    "operationId": "searchCatalogue",
                    "parameters": [
                        {"name": "q", "in": "query", "schema": {"type": "string"}},
                        {
                            "name": "jur",
                            "in": "query",
                            "schema": {
                                "type": "string",
                                "enum": [
                                    "cth",
                                    "nsw",
                                    "vic",
                                    "qld",
                                    "wa",
                                    "sa",
                                    "tas",
                                    "act",
                                    "nt",
                                ],
                            },
                        },
                        {
                            "name": "state",
                            "in": "query",
                            "description": "One or more of votable, chosen, served and closed, comma separated.",
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "ids",
                            "in": "query",
                            "description": "Up to 50 record ids or vote keys, comma separated.",
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "url",
                            "in": "query",
                            "schema": {"type": "string", "format": "uri"},
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 50,
                                "default": 20,
                            },
                        },
                        {
                            "name": "offset",
                            "in": "query",
                            "schema": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 5000,
                                "default": 0,
                            },
                        },
                    ],
                    "responses": {
                        "200": j(
                            "Matching records, the total, the next offset and the date the catalogue was read"
                        ),
                        "400": {"description": "A parameter out of range"},
                        "429": {
                            "description": "Over the fair-use limit for one address. Wait Retry-After seconds."
                        },
                        "503": {"description": "The catalogue index is not loaded yet"},
                    },
                }
            },
            "/api/v1/requests": {
                "post": {
                    "tags": ["backlog"],
                    "summary": "Vote for a dataset by its portal URL. Nothing is stored when the URL is not in the catalogue.",
                    "description": "status is voted (with the new count), served (with its page here), closed (with the reason it cannot take a vote) or not_found (with who to contact).",
                    "operationId": "requestDataset",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["url"],
                                    "properties": {"url": {"type": "string", "format": "uri"}},
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": j("What the URL names and what happened"),
                        "400": {"description": "Not a URL"},
                        "429": {
                            "description": "Over the fair-use limit for one address. Wait Retry-After seconds."
                        },
                        "503": {"description": "The catalogue index is not loaded yet"},
                    },
                }
            },
            "/api/v1/views": {
                "post": {
                    "tags": ["explorer"],
                    "summary": "Save an explorer dashboard and get a short link. The id is a hash of the dashboard, so saving it again returns the same id, and a saved dashboard never changes.",
                    "operationId": "saveDashboard",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["slug", "version", "workspace"],
                                    "properties": {
                                        "slug": {
                                            "type": "string",
                                            "enum": slugs,
                                            "description": "The dataset the dashboard shows.",
                                        },
                                        "version": {
                                            "type": "string",
                                            "format": "date",
                                            "description": "The dated version the dashboard reads.",
                                        },
                                        "workspace": {
                                            "type": "object",
                                            "description": "Perspective workspace settings: panels (1 to 12, keyed by id), layout, masters and global_filters. At most 32,768 bytes.",
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": j(
                            "Saved",
                            {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "16 hex characters"},
                                    "url": {"type": "string", "format": "uri"},
                                    "embed": {"type": "string", "format": "uri"},
                                },
                            },
                        ),
                        "400": {
                            "description": "Unknown dataset or version, or settings the explorer cannot show"
                        },
                        "413": {"description": "Over 32,768 bytes"},
                        "429": {"description": "100 new dashboards a day per browser"},
                    },
                }
            },
            "/api/v1/views/{id}": {
                "get": {
                    "tags": ["explorer"],
                    "summary": "A saved dashboard: its dataset, version and workspace. Cached for good.",
                    "operationId": "getDashboard",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "description": "The id POST /api/v1/views returned.",
                            "schema": {"type": "string", "pattern": "^[a-f0-9]{16}$"},
                        }
                    ],
                    "responses": {
                        "200": j("The dashboard"),
                        "404": {"description": "No such dashboard"},
                    },
                }
            },
        },
        "components": {"schemas": {"Provenance": prov}},
    }

    if QUERY_API and queried:
        doc["tags"].append({"name": "query"})
        q_slug = slug_p | {"schema": {"type": "string", "enum": [o.dataset.slug for o in queried]}}
        doc["paths"].update(_query_paths(queried, q_slug, generic=True))
    return doc


def _served_row(o: DatasetOut, dirx) -> dict:
    """What search_datasets finds a served dataset by, and what it answers with."""
    ds = o.dataset
    from .publishers import JUR_SEGMENT

    words = [ds.search_title, *ds.also_known_as, *ds.keywords, ds.collection_title]
    return {
        "slug": ds.slug,
        "title": ds.title,
        "summary": ds.summary or ds.description,
        "publisher": ds.publisher.name,
        "jur": JUR_SEGMENT.get(dirx.ds_pub[ds.slug].jurisdiction, ""),
        "licence": ds.licence.url,
        "page": dataset_url(ds.slug),
        "latest": f"{dataset_url(ds.slug)}latest/{_files_of(ds, o.latest)[0][0]}",
        "keywords": " ".join(w for w in words if w),
        "fields": " ".join(f"{name} {f.display}" for name, f in _all_fields(ds)),
    }


def _dataset_card(out: Path, ds: Dataset, v, rel: str, cache: BuildCache | None) -> brand.Card:
    return brand.dataset_card(
        out,
        rel,
        ds.title,
        f"{JUR_LONG[ds.publisher.jurisdiction]} · {ds.publisher.name}",
        [
            f"{fmt_int(v.rows)} rows",
            f"{len(ds.tables)} tables" if ds.kind == "database" else f"{ds.field_count} fields",
            ds.licence.title,
            f"Version {v.manifest.version}",
        ],
        ds.publisher.name,
        cache,
    )


def render_site(
    outs: list[DatasetOut],
    out: Path,
    records: list[dict] | None = None,
    curated: list | None = None,
    catalogue_as_at: str = "",
    catalogue_stats: dict | None = None,
    search: Path | None = None,
    cache: BuildCache | None = None,
    hubs: dict | None = None,
) -> None:
    e = env()
    static_src = Path(__file__).parent / "static"
    shutil.copytree(static_src, out / "static", dirs_exist_ok=True)
    (out / "static" / "site.css").unlink()  # inlined into every page
    (out / "static" / "explorer.css").unlink()  # inlined into explorer pages
    vendor = explorer.vendor(out)
    engine_size = fmt_size(
        sum(
            p.stat().st_size
            for p in (out / vendor.strip("/")).rglob("*")
            if p.is_file() and "memory64" not in p.name and p.suffix != ".txt"
        )
    )
    ex_css = (static_src / "explorer.css").read_text(encoding="utf-8").strip()
    ex_v = hashlib.sha256((static_src / "explorer.js").read_bytes()).hexdigest()[:12]
    brand.icons(out)
    datasets = [o.dataset for o in outs]
    by_slug = {o.dataset.slug: o for o in outs}
    live = [o for o in outs if o.versions]
    live_slugs = {o.dataset.slug for o in live}
    built_at = max(
        (v.manifest.fetched_at for o in live for v in o.versions),
        default="1970-01-01T00:00:00+00:00",
    )
    site_card = brand.site_card(
        out, len(live), len({o.dataset.publisher.name for o in live}), cache
    )

    from . import directory
    from .publishers import JUR_NAME, JURISDICTIONS

    dirx = directory.plan(
        datasets, list(records or []), list(curated or []), catalogue_as_at, catalogue_stats or {}
    )
    if search and catalogue_as_at:
        from .d1 import catalogue_sqlite, served_table

        catalogue_sqlite(search, directory.search_rows(dirx), catalogue_as_at)
        served_table(search, [_served_row(o, dirx) for o in live])

    def trail(ds: Dataset) -> tuple[list[tuple[str, str]], dict]:
        pub = dirx.ds_pub[ds.slug]
        jp = dirx.jur_path(pub.jurisdiction)
        crumbs = [
            ("Datasets", SITE + "/"),
            (JUR_NAME[pub.jurisdiction], SITE + jp),
            (ds.publisher.short, SITE + pub.path),
        ]
        return crumbs, {"jur_path": jp, "publisher_path": pub.path}

    css = (static_src / "site.css").read_text(encoding="utf-8").strip()
    js = (static_src / "site.js").read_text(encoding="utf-8")
    assert js.count(API_SLOT) == 1
    js = js.replace(API_SLOT, json.dumps(at.browser_spec(), ensure_ascii=False, sort_keys=True))
    _write(out, "static/site.js", js)
    common = {
        "site": SITE,
        "site_host": HOST,
        "repo": REPO,
        "gh_mark": GH_MARK,
        "beacon": BEACON,
        "origin_trial": ORIGIN_TRIAL,
        "css": css,
        "js_v": hashlib.sha256(js.encode("utf-8")).hexdigest()[:12],
        "api_doc": _api_doc(),
        "og": site_card,
        "theme": {"light": brand.PAGE_LIGHT, "dark": brand.PAGE_DARK},
        "mcp_add": at.spec()["mcp"]["add_command"],
        "connect": at.spec()["mcp"]["connect"],
        "tool_names": at.tool_names(),
        "tool_titles": {n: t["title"] for n, t in at.spec()["webmcp"]["tools"].items()},
    }

    def page(rel: str, template: str, md: str, **ctx) -> None:
        canonical = (
            SITE + "/" + rel.rsplit("index.html", 1)[0]
            if rel.endswith("index.html")
            else SITE + "/" + rel
        )
        md_url = (
            canonical.rsplit("/", 1)[0] + "/index.md"
            if canonical.endswith("/")
            else canonical + ".md"
        )
        h = e.get_template(template).render(**{**common, **ctx}, canonical=canonical, md_url=md_url)
        _write(out, rel, h)
        _write(
            out,
            rel.replace("index.html", "index.md") if rel.endswith("index.html") else rel + ".md",
            md,
        )

    def version_pages(o, ds, views, fig, hints, card, base, latest) -> None:
        """One page per dated version: its files, its change from the version before and its figure."""
        for v, view in zip(o.versions, views, strict=True):
            vfig = (
                fig
                if v is latest
                else figures.dataset_figures(
                    ds,
                    v.manifest,
                    hints,
                    out / "d" / ds.slug / "v" / v.manifest.version / "data.parquet",
                    out,
                )
            )
            files = [
                {
                    "name": k,
                    "size": fmt_size(s),
                    "url": f"{version_url(ds.slug, v.manifest.version)}{k}",
                    "download": download_name(ds.slug, v.manifest.version, k),
                }
                for k, s in v.files.items()
                if "/" not in k
            ]
            md = "\n".join(
                [
                    "---",
                    f"title: {ds.title}, version {v.manifest.version}",
                    f"resource: {version_url(ds.slug, v.manifest.version)}",
                    f"dataset: {base}",
                    f"rows: {v.rows}",
                    f"source_sha256: {v.manifest.sha256}",
                    "---",
                    "",
                    f"# {ds.title}, version {v.manifest.version}",
                    "",
                    f"{v.rows} rows, {ds.field_count} fields, fetched {view['fetched_long']}. This version keeps its content. Its files change only for the reasons the terms give ({SITE}/terms/), and each change is recorded in its notes.",
                    "",
                    *(
                        ["## About this version", "", *v.manifest.notes, ""]
                        if v.manifest.notes
                        else []
                    ),
                    *(
                        [f"Condition of use: {ds.licence.condition}", ""]
                        if ds.licence.condition
                        else []
                    ),
                    *[f"- {f['name']}: {f['url']} ({f['size']})" for f in files],
                    "",
                    *[f"{why}\n" for why in _left_out(ds, v)],
                    f"SHA-256 of every file, under the names they download as: {version_url(ds.slug, v.manifest.version)}SHA256SUMS",
                    "",
                    "## Attribution",
                    "",
                    attribution(ds, v.manifest),
                    "",
                ]
            )
            page(
                f"d/{ds.slug}/v/{v.manifest.version}/index.html",
                "version.html",
                md,
                title=f"{ds.title} {v.manifest.version} | {HOST}",
                description=f"Dated version {v.manifest.version} of {ds.title}.",
                nav="datasets",
                noindex=True,
                og=card
                if v is latest
                else _dataset_card(out, ds, v, f"og/d/{ds.slug}/v/{v.manifest.version}.png", cache),
                ds=ds,
                base=base,
                v=view,
                is_latest=v is latest,
                files=files,
                left_out=_left_out(ds, v),
                partition_dirs=list(v.partitions.keys()),
                change=view["change"],
                fig=vfig,
                others=[x["version"] for x in views if x["version"] != v.manifest.version],
                attribution=attribution(ds, v.manifest),
                portal_host=(v.manifest.source.get("url") or "").split("/")[2]
                if v.manifest.source.get("url")
                else "",
            )

    # Dataset pages and version pages.
    resources = []
    queried: list[DatasetOut] = []
    figs: dict[str, dict] = {}
    consoles: dict[str, dict | None] = {}
    views_by: dict[str, list[dict]] = {}
    explored: set[str] = set()  # slugs whose Parquet is small enough for an explorer page
    place_urls: dict[str, list[str]] = {}
    for o in live:
        ds = o.dataset
        base = dataset_url(ds.slug)
        latest = o.latest
        m = latest.manifest
        changes_by_to = {c["to"]: c for c in o.changes}
        views = [_version_view(ds, v, changes_by_to.get(v.manifest.version)) for v in o.versions]
        siblings = [
            d
            for d in datasets
            if ds.collection
            and d.collection == ds.collection
            and d.slug != ds.slug
            and d.slug in live_slugs
        ]
        if ds.kind == "database":
            fig = figures.dataset_figures(ds, m, None, out / "d" / ds.slug / "nothing", out)
            figs[ds.slug] = fig
            consoles[ds.slug] = None
            views_by[ds.slug] = views
            place_urls[ds.slug] = []
            copies = copies_of(hubs, ds.slug)
            citation = cite(ds, m, version_url(ds.slug, m.version))
            crumbs, links = trail(ds)
            crumbs.append((ds.title, base))
            faq_auto = _db_faq(ds, latest)
            faq = [*faq_auto, *ds.faq]
            card = _dataset_card(out, ds, latest, f"og/d/{ds.slug}.png", cache)
            related = _related(ds, live)
            page(
                f"d/{ds.slug}/index.html",
                "database.html",
                _md_twin_database(ds, o, views, faq, citation, related),
                title=_db_seo_title(ds, latest),
                description=_db_seo_description(ds, latest),
                nav="datasets",
                og=card,
                faq=faq,
                faq_auto=faq_auto,
                related=related,
                citation=citation,
                cite_request=CITE_REQUEST,
                copies=copies,
                **links,
                extra_jsonld=[
                    json.dumps(_breadcrumbs(crumbs), ensure_ascii=False),
                    *([json.dumps(_faq_jsonld(faq), ensure_ascii=False)] if faq else []),
                ],
                ds=ds,
                base=base,
                latest=views[-1],
                versions=views,
                vbase=version_url(ds.slug, m.version),
                db=_db_view(ds, latest),
                use_tabs=_use_tabs(ds, latest),
                mcp_add=at.spec()["mcp"]["add_command"],
                jur_long=JUR_LONG[ds.publisher.jurisdiction],
                description_paras=[p for p in ds.description.split("\n\n") if p.strip()],
                history_size=fmt_size((out / "d" / ds.slug / "history.tar.zst").stat().st_size),
                attribution=attribution(ds, m),
                portal_host=(m.source.get("url") or "").split("/")[2]
                if m.source.get("url")
                else "",
                jsonld=json.dumps(_dataset_jsonld(ds, o, copies), ensure_ascii=False),
            )
            version_pages(o, ds, views, fig, None, card, base, latest)
            continue
        formats, fmt_data = _picker(ds, latest)
        first = json.loads(latest.first or "{}")
        sample = json.dumps(first, ensure_ascii=False, indent=2)
        example_field = (
            ds.partition_by[0] if ds.partition_by else (ds.key[0] if ds.key else ds.fields[0].name)
        )
        console = hints = None
        rows_path = out / "d" / ds.slug / "v" / m.version / "data.parquet"
        if rows_path.exists():
            hints = _console(ds, rows_path)
        if QUERY_API and hints and queryable(ds, latest.files.get("data.csv")):
            queried.append(o)
            console = hints
            _write(out, f"d/{ds.slug}/openapi.json", pretty(_dataset_openapi(o, console)))
            console["api"] = f"/api/v1/datasets/{ds.slug}/"
            console["site"] = SITE
            console["versions"] = [v["version"] for v in reversed(views)][:KEEP]
            fields_body = pretty(_fields_resource(o, console))
            _write(out, f"d/{ds.slug}/fields.json", fields_body)
            resources.append(
                {
                    "uri": f"{SITE}/d/{ds.slug}/fields.json",
                    "name": ds.slug,
                    "title": ds.title,
                    "description": at.resource_text(ds.title, ds.publisher.name),
                    "mimeType": "application/json",
                    "size": len(fields_body.encode("utf-8")),
                    "annotations": {
                        **at.RESOURCE_ANNOTATIONS,
                        "lastModified": f"{m.version}T00:00:00Z",
                    },
                }
            )
        fig = figures.dataset_figures(ds, m, hints, rows_path, out)
        figs[ds.slug] = fig
        consoles[ds.slug] = console
        views_by[ds.slug] = views
        example = figures.example_rows(rows_path, console, key=ds.key) if console else []
        explore = None
        ex_versions = [
            {
                "version": view["version"],
                "as_at_long": view["as_at_long"],
                "rows_fmt": view["rows_fmt"],
                "parquet": f"/d/{ds.slug}/v/{view['version']}/data.parquet",
                "size": v.files["data.parquet"],
            }
            for v, view in reversed(list(zip(o.versions, views, strict=True)))
            if 0 < v.files.get("data.parquet", 0) <= explorer.MAX_PARQUET
        ]
        if hints and ex_versions and ex_versions[0]["version"] == m.version:
            explore = {
                "slug": ds.slug,
                "title": ds.title,
                "site": SITE,
                "page": f"/d/{ds.slug}/explore/",
                "embed_page": f"/d/{ds.slug}/embed/",
                "views": "/api/v1/views",
                "vendor": vendor,
                "versions": ex_versions,
                "int32": explorer.int32_fields(hints),
                "labels": explorer.labels(ds, hints),
                "text": explorer.text_fields(hints),
                "yesno": explorer.yes_no_fields(hints),
                "defaults": explorer.defaults(ds, hints),
            }
        ds_data = json.dumps(
            {
                "slug": ds.slug,
                "title": ds.title,
                "base": base,
                "latest": latest.manifest.version,
                "formats": fmt_data,
                "example_field": example_field,
                "console": console,
            },
            ensure_ascii=False,
        )
        # Console values come from the data, so nothing in them may close the script block.
        ds_data = _escape_script(ds_data)
        # The place field has its own section of pages, so it is not listed twice.
        partitions = [
            {"field": k, "entries": v} for k, v in latest.partitions.items() if k != ds.place_field
        ]
        attr = attribution(ds, m)
        publisher_note = ""
        if ds.collection:
            dated = {o.dataset.slug: o.latest.manifest.version for o in live}
            n = len(siblings) + 1
            if all(dated.get(d.slug) == m.version for d in siblings):
                publisher_note = f"The publisher releases {ds.collection_title} as {n} files at once, so all {n} tables here share a version date."
            else:
                publisher_note = f"{ds.collection_title} is {n} tables here, and each takes its version date from its own file."
        faq_auto = _faq(ds, latest, latest.partitions, fig.get("years", ""))
        faq = [*faq_auto, *ds.faq]
        copies = copies_of(hubs, ds.slug)
        citation = cite(ds, m, version_url(ds.slug, m.version))
        crumbs, links = trail(ds)
        if ds.collection:
            crumbs.append((ds.collection_title, collection_url(ds.collection)))
        crumbs.append((ds.title, base))
        related = _related(ds, live)
        sample_rows = _sample(ds, rows_path)
        serialise_dictionary(ds, latest, out / "d" / ds.slug / "schema.xlsx")
        places = _places(ds, latest)
        card = _dataset_card(out, ds, latest, f"og/d/{ds.slug}.png", cache)
        problem = urllib.parse.urlencode(
            {"template": "data-problem.yml", "dataset": version_url(ds.slug, m.version)}
        )
        page(
            f"d/{ds.slug}/index.html",
            "dataset.html",
            _md_twin_dataset(
                ds, o, views, siblings, faq, citation, console, bool(explore), related, places
            ),
            title=_seo_title(ds, latest, fig.get("years", "")),
            description=_seo_description(ds, latest, fig.get("years", "")),
            nav="datasets",
            og=card,
            faq=faq,
            faq_auto=faq_auto,
            related=related,
            sample_rows=sample_rows,
            places=places,
            place_label=ds.field(ds.place_field).display if ds.place_field else "",
            citation=citation,
            cite_request=CITE_REQUEST,
            licence_record=licence_record(ds, latest.manifest),
            spine_attribution=SPINE_ATTRIBUTION,
            copies=copies,
            collection_href=collection_url(ds.collection) if ds.collection else "",
            entry_path=register_path(ds),
            problem_url=f"{REPO}/issues/new?{problem}",
            **links,
            extra_jsonld=[
                json.dumps(_breadcrumbs(crumbs), ensure_ascii=False),
                *([json.dumps(_faq_jsonld(faq), ensure_ascii=False)] if faq else []),
            ],
            ds=ds,
            base=base,
            latest=views[-1],
            versions=views,
            formats=formats,
            left_out=_left_out(ds, latest),
            ds_data=ds_data,
            console=console,
            explore_url=explore["page"] if explore else "",
            fig=fig,
            example_rows=[(k, figures.fmt(v)) for k, v in example],
            example_words=(_example_title(ds, console) if example else ""),
            rows_example=_example_query(ds.slug, console, "rows") if console else "",
            aggregate_example=_example_query(ds.slug, console, "aggregate") if console else "",
            format_count=len(formats) - (1 if fmt_data.get("partition") else 0),
            use_tabs=_use_tabs(
                ds, latest, _example_query(ds.slug, console, "aggregate") if console else ""
            ),
            mcp_add=at.spec()["mcp"]["add_command"],
            filter_help=FILTER_HELP,
            sample=sample,
            partitions=partitions,
            siblings=siblings,
            jur_long=JUR_LONG[ds.publisher.jurisdiction],
            description_paras=[p for p in ds.description.split("\n\n") if p.strip()],
            has_suppressed=bool(ds.suppression),
            history_size=fmt_size((out / "d" / ds.slug / "history.tar.zst").stat().st_size),
            attribution=attr,
            portal_host=(m.source.get("url") or "").split("/")[2] if m.source.get("url") else "",
            publisher_note=publisher_note,
            jsonld=json.dumps(_dataset_jsonld(ds, o, copies), ensure_ascii=False),
        )
        place_urls[ds.slug] = _place_pages(
            ds,
            o,
            views[-1],
            console,
            rows_path,
            out,
            page,
            places,
            card,
            crumbs,
            links,
            citation,
        )
        if explore:
            explored.add(ds.slug)
            ex_ctx = {
                "ds": ds,
                "base": base,
                "latest": views[-1],
                "attribution": attr,
                "vendor": vendor,
                "explorer_css": ex_css,
                "ex_v": ex_v,
                "explore_url": SITE + explore["page"],
            }
            page(
                f"d/{ds.slug}/explore/index.html",
                "explore.html",
                _md_twin_explore(ds, explore, attr),
                title=f"Explore {ds.title} | {HOST}",
                description=f"Charts and tables over {ds.title} from {ds.publisher.name}, worked out in the browser. Share or embed a dashboard.",
                nav="datasets",
                og=card,
                noindex=True,
                extra_jsonld=[
                    json.dumps(
                        _breadcrumbs([*crumbs, ("Explore", SITE + explore["page"])]),
                        ensure_ascii=False,
                    )
                ],
                ex_data=_script_json({**explore, "embed": False}),
                explore_versions=ex_versions,
                explore_master="masters" in explore["defaults"],
                engine_size=engine_size,
                parquet_url=ex_versions[0]["parquet"],
                parquet_size=fmt_size(ex_versions[0]["size"]),
                jur_long=JUR_LONG[ds.publisher.jurisdiction],
                fig=fig,
                **links,
                **ex_ctx,
            )
            _write(
                out,
                f"d/{ds.slug}/explore/versions.json",
                pretty({"versions": [v["version"] for v in ex_versions]}),
            )
            _write(
                out,
                f"d/{ds.slug}/embed/index.html",
                e.get_template("embed.html").render(
                    **common,
                    canonical=SITE + explore["page"],
                    md_url=SITE + explore["page"] + "index.md",
                    title=f"{ds.title} | {HOST}",
                    description=f"A dashboard over {ds.title} from {ds.publisher.name}.",
                    ex_data=_script_json({**explore, "embed": True}),
                    **ex_ctx,
                ),
            )
        version_pages(o, ds, views, fig, hints, card, base, latest)

    # Collection pages: one per publisher release that arrives as several tables.
    collections: dict[str, list[DatasetOut]] = {}
    for o in live:
        if o.dataset.collection:
            collections.setdefault(o.dataset.collection, []).append(o)
    for coll, members in collections.items():
        first = members[0]
        title = first.dataset.collection_title
        desc = next(
            (o.dataset.collection_description for o in members if o.dataset.collection_description),
            "",
        )
        pub = first.dataset.publisher
        m = first.latest.manifest
        curl = collection_url(coll)
        table = [
            {
                "slug": o.dataset.slug,
                "title": o.dataset.title,
                "summary": o.dataset.summary,
                "rows": fmt_int(o.latest.rows),
                "fields": o.dataset.field_count,
                "version": o.latest.manifest.version,
                "quick": [
                    {
                        "name": f,
                        "url": f"{version_url(o.dataset.slug, o.latest.manifest.version)}data.{f}",
                    }
                    for f in ("parquet", "json", "csv", "xlsx", "sqlite")
                    if f"data.{f}" in o.latest.files
                ],
            }
            for o in members
        ]
        keywords = sorted(
            {k for o in members for k in (*o.dataset.keywords, *o.dataset.also_known_as)}
        )
        aka = sorted({k for o in members for k in o.dataset.also_known_as})
        years = _years(first.dataset, m)
        coll_lead = next(
            (
                o.dataset.collection_search_title
                for o in members
                if o.dataset.collection_search_title
            ),
            title,
        )
        coll_desc = (
            f"{title}: the {len(members)} tables {pub.name} releases together, {years + ', ' if years else ''}"
            f"{fmt_int(sum(o.latest.rows for o in members))} rows in all, as CSV, JSON, Parquet, SQLite and GeoJSON. "
            f"{pub.short} {first.dataset.licence.title}. No login, no key."
        )
        coll_jsonld = {
            "@context": "https://schema.org",
            "@type": "Dataset",
            "@id": curl,
            "url": curl,
            "name": title,
            "description": desc or coll_desc,
            "keywords": keywords or None,
            "license": first.dataset.licence.url,
            "isAccessibleForFree": True,
            "creator": {"@type": "Organization", "name": pub.name, "url": pub.url},
            "publisher": {"@type": "Organization", "name": pub.name, "url": pub.url},
            "sdPublisher": OPERATOR_ORG,
            "includedInDataCatalog": CATALOG,
            "isBasedOn": landing(first.dataset),
            "version": m.version,
            "dateModified": m.version,
            "temporalCoverage": _temporal(first.dataset, m),
            "spatialCoverage": {"@type": "Place", "name": JUR_LONG.get(pub.jurisdiction)},
            "hasPart": [dataset_url(o.dataset.slug) for o in members],
            "conditionsOfAccess": NOT_ENDORSED,
        }
        coll_md = "\n".join(
            [
                "---",
                f"title: {title}",
                f"resource: {curl}",
                f"publisher: {pub.name}",
                f"licence: {first.dataset.licence.title}",
                f"version: {m.version}",
                "---",
                "",
                f"# {title}",
                "",
                *([desc, ""] if desc else []),
                f"Published by {pub.name} under {first.dataset.licence.title}. {pub.name} has not endorsed this site.",
                "",
                "## Tables",
                "",
                *[
                    f"- [{t['title']}]({dataset_url(t['slug'])}): {t['summary']} {t['rows']} rows, {t['fields']} fields. Markdown: {dataset_url(t['slug'])}index.md"
                    for t in table
                ],
                "",
                "## Attribution and citation",
                "",
                attribution(first.dataset, m),
                "",
                CITE_REQUEST,
                "",
            ]
        )
        page(
            f"c/{coll}/index.html",
            "collection.html",
            coll_md,
            title=f"{coll_lead}{f' {years}' if years else ''}: {len(members)} tables as CSV, Excel, JSON, Parquet | {HOST}",
            description=coll_desc,
            nav="datasets",
            heading=title,
            collection_paras=[x.strip() for x in desc.split("\n\n") if x.strip()],
            publisher=pub,
            licence=first.dataset.licence,
            version=m.version,
            as_at_long=long_date(m.as_at) if m.as_at else "",
            years=years,
            tables=table,
            also_known_as=aka,
            attribution=attribution(first.dataset, m),
            cite_request=CITE_REQUEST,
            landing=landing(first.dataset),
            portal_host=landing(first.dataset).split("/")[2]
            if "://" in landing(first.dataset)
            else landing(first.dataset),
            status="live" if all(o.dataset.status == "live" for o in members) else "building",
            jur_long=JUR_LONG[pub.jurisdiction],
            jsonld=json.dumps(coll_jsonld, ensure_ascii=False),
            **trail(first.dataset)[1],
            extra_jsonld=[
                json.dumps(
                    _breadcrumbs([*trail(first.dataset)[0], (title, curl)]), ensure_ascii=False
                )
            ],
        )

    # Home.
    live_rows = {
        d.slug: _row(d, by_slug.get(d.slug), figs.get(d.slug))
        for d in datasets
        if d.status in ("live", "building")
    }
    dir_urls = directory.render(
        dirx, page, lambda rel, text: _write(out, rel, text), live_rows, _breadcrumbs
    )
    rows = list(live_rows.values())
    backlog_rows = [
        _row(d, None) for d in datasets if d.status in ("backlog", "assessing", "blocked")
    ]
    # A database has no format picker, so the hero is a table where there is one.
    hero = max(
        [o for o in live if o.dataset.kind != "database"] or live,
        key=lambda o: (len(_fmts(o.dataset, o.latest)), o.latest.rows),
    )
    governments = sorted({dirx.ds_pub[o.dataset.slug].jurisdiction for o in live})
    stats = {
        "live": len(live),
        "rows": fmt_int(sum(o.latest.rows for o in live)),
        "versions": sum(len(o.versions) for o in live),
        "formats": len(FORMATS) + len(SHAPE_FORMATS),
        "governments": len(governments),
        "publishers": len({o.dataset.publisher.name for o in live}),
        "backlog": fmt_int(dirx.votable) if dirx.votable else len(backlog_rows),
    }
    # The map: every located crash dataset overlaid, states without one hatched.
    parts, hero_total, present, hero_slug = [], 0, [], ""
    for slug, spec in HERO["datasets"].items():
        o = by_slug.get(slug)
        if not o or not o.versions or not o.dataset.geometry or not figs.get(slug, {}).get("cells"):
            continue
        states = (
            list(figures.STATES)
            if spec["states"] == "all"
            else [JUR_LONG[o.dataset.publisher.jurisdiction]]
        )
        present += [x for x in states if x not in present]
        hero_slug = hero_slug or slug
        g = o.dataset.geometry
        db = out / "d" / slug / "v" / o.latest.manifest.version / "data.parquet"
        c = figures.cells(db, g["lon"], g["lat"], spec["where"])
        if c:
            parts.append((states[0], c))
            hero_total += int(round(sum(c.values())))
    hero_map = None
    if parts:
        covered = present
        gaps = [n for n in figures.STATES if n not in covered]
        hero_map = {
            "svg": figures.national_map(
                parts, HERO["what"], HERO["why"], out, covered=set(covered)
            ),
            "lede": HERO["lede"].format(total=fmt_int(hero_total)),
            # The hero draws no outlines; the ABS shapes are used only to hatch a gap.
            "outlines": figures.OUTLINES if gaps else "",
            "covered": _join(covered),
            "gaps": _join(gaps),
            "gap_count": len(gaps),
            # A table too large for the explorer links to its page instead.
            "explore": f"/d/{hero_slug}/explore/" if hero_slug in explored else f"/d/{hero_slug}/",
            "explore_label": "Explore the map" if hero_slug in explored else "See the dataset",
        }
    # The format picker, over the largest table.
    demo_formats, demo_data = _picker(hero.dataset, hero.latest)
    demo = {
        "slug": hero.dataset.slug,
        "title": hero.dataset.title,
        "version": hero.latest.manifest.version,
        "rows": fmt_int(hero.latest.rows),
        "formats": [f for f in demo_formats if f["key"] != "partition"],
        "ds_data": _escape_script(
            json.dumps(
                {
                    "slug": hero.dataset.slug,
                    "title": hero.dataset.title,
                    "base": dataset_url(hero.dataset.slug),
                    "latest": hero.latest.manifest.version,
                    "formats": demo_data,
                    "example_field": hero.dataset.partition_by[0]
                    if hero.dataset.partition_by
                    else (hero.dataset.key[0] if hero.dataset.key else hero.dataset.fields[0].name),
                    "console": None,
                },
                ensure_ascii=False,
            )
        ),
    }
    # The query and agent examples: the console's first aggregate, answered in the build.
    show = next((o for o in queried if o.dataset.slug == SHOWCASE), None) or (
        hero if hero in queried else (queried[0] if queried else None)
    )
    query_demo = agent_demo = None
    if show and consoles.get(show.dataset.slug):
        sd, sm = show.dataset, show.latest.manifest
        con = consoles[sd.slug]
        db = out / "d" / sd.slug / "v" / sm.version / "data.parquet"
        srows = figures.example_rows(db, con, key=sd.key)
        group = (con["example"]["group"] or [None])[0]
        # The demo adds the groups up and hands the agent its filters as exact matches.
        adds = con["example"]["metric"].split(".")[0] in ("count", "sum")
        exact = all(f["op"] == "eq" for f in con["example"]["filters"])
        if srows and group and adds and exact:
            what = _asked(sd, con)
            metric = con["example"]["metric"]
            key = "count" if metric == "count" else metric.split(".", 1)[1]
            words = _example_title(sd, con)
            path = _example_query(sd.slug, con, "aggregate")
            answer = {
                "rows": [{group: k, key: v} for k, v in srows[:3]],
                "publicdata": {
                    "version": sm.version,
                    "licence": sd.licence.title,
                    "attribution": attribution(sd, sm)[:60] + "…",
                },
            }
            query_demo = {
                "path": path,
                "url": SITE + path,
                "svg": figures.hbars_svg(srows, words),
                "caption": f"{words}, {fmt_int(int(sum(v for _, v in srows)))} in all, from version {sm.version}.",
                "json": json.dumps(answer, ensure_ascii=False)
                .replace("}, {", "},\n  {")
                .replace(', "publicdata"', ',\n "publicdata"'),
            }
            top, top_n = srows[0]
            total = int(sum(v for _, v in srows))
            where = _filter_words(sd, con)
            agent_demo = {
                "question": f"Which {_words(sd, group)} had the most {what.lower()}{where}?",
                "answer": f"{top}, with {fmt_int(top_n)} of the {fmt_int(total)} {what.lower()}{where}.",
            }
    # The version history, shown on the dataset with the most releases.
    kept = max(live, key=lambda o: (len(o.versions), o.latest.rows))
    kv = views_by[kept.dataset.slug][-1]
    keeps = {
        "slug": kept.dataset.slug,
        "title": kept.dataset.title,
        "publisher": kept.dataset.publisher.short,
        "count": len(kept.versions),
        "view": kv,
        "key": ", ".join(kept.dataset.key),
        "cadence": kept.dataset.source.cadence,
    }
    # The explorer preview: the largest table that has an explorer page, its chart, its first
    # aggregate and a few rows.
    preview = None
    shown = max(
        (
            o
            for o in live
            if o.dataset.slug in explored and figs.get(o.dataset.slug, {}).get("chart")
        ),
        key=lambda o: (len(_fmts(o.dataset, o.latest)), o.latest.rows),
        default=None,
    )
    if shown:
        hf = figs[shown.dataset.slug]
        hdb = out / "d" / shown.dataset.slug / "v" / shown.latest.manifest.version / "data.parquet"
        hcon = consoles.get(shown.dataset.slug)
        hrows = figures.example_rows(hdb, hcon, key=shown.dataset.key) if hcon else []
        bars_title = _example_title(shown.dataset, hcon) if hrows else ""
        preview = {
            "slug": shown.dataset.slug,
            "title": shown.dataset.title,
            "explore": f"/d/{shown.dataset.slug}/explore/",
            "chart": hf["chart"],
            "chart_title": f"{hf['what']} per year",
            "bars": figures.hbars_svg(hrows, bars_title) if hrows else "",
            "bars_title": bars_title,
            "table": figures.sample_rows(hdb, [f.name for f in shown.dataset.fields][:5]),
        }
    # The MCP server is its own entity; the directories that list it identify it.
    mcp_jsonld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "WebAPI",
            "@id": f"{SITE}/mcp#server",
            "name": f"{HOST} MCP server",
            "description": at.spec()["mcp"]["registry_description"],
            "url": f"{SITE}/mcp",
            "documentation": f"{SITE}/agents/#mcp",
            "provider": OPERATOR_ORG,
            "termsOfService": f"{SITE}/terms/",
            "sameAs": at.listed_at(),
        },
        ensure_ascii=False,
    )
    accounts = sorted({*((hubs or {}).get("accounts") or {}).values(), GITHUB_ORG})
    home_jsonld = {
        "@context": "https://schema.org",
        "@type": "DataCatalog",
        "@id": CATALOG_ID,
        "name": HOST,
        "url": SITE + "/",
        "description": "Versioned republication of Australian open government data as CSV, Excel, JSON, Parquet, SQLite, DuckDB, GeoJSON and GeoPackage, with a query API, a browser explorer and an MCP server.",
        # The operator's accounts on GitHub and on the data hubs that carry copies of these datasets.
        "provider": {**OPERATOR_ORG, "sameAs": accounts},
    }
    home_md = "\n".join(
        [
            "---",
            f"title: {HOST}",
            f"resource: {SITE}/",
            "language: en-AU",
            f"operator: {OPERATOR}",
            "not_endorsed: true",
            "---",
            "",
            f"# {brand.HEADLINE}",
            "",
            f"publicdata.au republishes Australian government datasets as CSV, Excel, JSON, Parquet, SQLite, DuckDB, GeoJSON and GeoPackage. Every release a publisher makes becomes a dated version that keeps its content, with its schema, its provenance and a diff against the release before. A query API answers filters and counts from a URL, an explorer charts every row in the browser, and an MCP server at {SITE}/mcp gives agents the same tools. There are no keys and no accounts. No government agency runs or has endorsed this site.",
            "",
            "## Datasets",
            "",
            *[
                f"- [{o.dataset.title}]({dataset_url(o.dataset.slug)}): {o.dataset.summary} Publisher {o.dataset.publisher.name}, {o.dataset.licence.title}, latest {o.latest.manifest.version}, {o.latest.rows} rows."
                for o in live
            ],
            "",
            "## Most wanted",
            "",
            f"The most-voted datasets are built first. Counts are at {SITE}/api/v1/votes, and every dataset on the portals can be searched at {SITE}/api/v1/catalogue and voted for at {SITE}/backlog/.",
            "",
            *[
                f"- {r['title']} ({r['publisher_name']}, {r['licence']}, {r['status_label']})"
                for r in backlog_rows
            ],
            "",
            f"Machine-readable: {SITE}/catalog.json, {SITE}/backlog.json, {SITE}/llms.txt, {SITE}/.well-known/ard.json, {SITE}/mcp",
            "",
        ]
    )
    # Topics are the way in: each counts what is served under it and what is coming.
    topic_rows = []
    for tslug, tinfo in TOPICS.items():
        members = sorted(
            (o for o in live if tslug in o.dataset.topics), key=lambda o: -o.latest.rows
        )
        coming = [
            {"title": d.title, "slug": d.slug}
            for d in datasets
            if tslug in d.topics and d.status in ("building", "backlog", "assessing")
        ]
        topic_rows.append(
            {
                "slug": tslug,
                "name": tinfo["name"],
                "blurb": tinfo["blurb"],
                "href": f"/topics/{tslug}/",
                "count": len(members),
                "rows": fmt_int(sum(o.latest.rows for o in members)),
                "titles": [o.dataset.title for o in members[:3]],
                "coming": coming[:3],
                "search": f"/backlog/?q={urllib.parse.quote(tinfo['search'])}",
            }
        )
    # The newest versions across every dataset, a line each.
    newest = sorted(
        (
            {
                "version": o.latest.manifest.version,
                "slug": o.dataset.slug,
                "title": o.dataset.title,
                "rows": fmt_int(o.latest.rows),
                "as_at_long": views_by[o.dataset.slug][-1]["as_at_long"],
                "change": views_by[o.dataset.slug][-1]["change"],
                "new": len(o.versions) == 1,
            }
            for o in live
        ),
        key=lambda x: (x["version"], x["slug"]),
        reverse=True,
    )[:8]
    # The showcase: the largest table under each topic, then the largest left, six in all.
    showcase, used = [], set()
    for tslug in TOPICS:
        best = max(
            (o for o in live if tslug in o.dataset.topics and o.dataset.slug not in used),
            key=lambda o: o.latest.rows,
            default=None,
        )
        if best and len(showcase) < 6:
            showcase.append(best)
            used.add(best.dataset.slug)
    for o in sorted(live, key=lambda o: -o.latest.rows):
        if len(showcase) >= 6:
            break
        if o.dataset.slug not in used:
            showcase.append(o)
            used.add(o.dataset.slug)
    # What the hero's file became: the pieces of its page, side by side.
    hm, hfig, hview = (
        hero.latest.manifest,
        figs.get(hero.dataset.slug, {}),
        views_by[hero.dataset.slug][-1],
    )
    hcon = consoles.get(hero.dataset.slug)
    becomes = {
        "slug": hero.dataset.slug,
        "title": hero.dataset.title,
        "publisher": hero.dataset.publisher.name,
        "filename": hm.filename,
        "size": fmt_size(hm.bytes),
        "licence": hero.dataset.licence.title,
        "fetched": long_date(hm.fetched_at),
        "sha": hm.sha256[:12],
        "version": hm.version,
        "rows": fmt_int(hero.latest.rows),
        "formats": _fmts(hero.dataset, hero.latest),
        "map": hfig.get("map", ""),
        "map_caption": hfig.get("map_caption", ""),
        "chart": hfig.get("chart", ""),
        "chart_caption": hfig.get("chart_caption", ""),
        "what": hfig.get("what", "Rows"),
        "years": hfig.get("years", ""),
        "query": _example_query(hero.dataset.slug, hcon, "aggregate") if hcon else "",
        "change": hview["change"],
        "versions": len(hero.versions),
        "key": ", ".join(hero.dataset.key),
        "cite": cite(hero.dataset, hm, version_url(hero.dataset.slug, hm.version))["text"],
    }
    if agent_demo:
        sd, sm = show.dataset, show.latest.manifest
        con = consoles[sd.slug]
        group = con["example"]["group"][0]
        agent_demo["steps"] = [
            {
                "tool": "search_datasets",
                "args": f'query="{sd.keywords[0] if sd.keywords else sd.title}"',
                "result": f"{sd.slug} · {sd.title} · {sd.publisher.short} · {fmt_int(show.latest.rows)} rows",
            },
            {
                "tool": "list_fields",
                "args": f'slug="{sd.slug}"',
                "result": f"{len(sd.fields)} fields · {', '.join(f.name for f in sd.fields[:8])}{' …' if len(sd.fields) > 8 else ''}",
            },
            {
                "tool": "count_rows",
                "args": f'slug="{sd.slug}" where={json.dumps({f["field"]: f["value"] for f in con["example"]["filters"]}, ensure_ascii=False)} group_by=["{group}"]',
                "result": f"{len(srows)} groups · {fmt_int(int(sum(v for _, v in srows)))} rows · version {sm.version}",
            },
        ]
        agent_demo["cite"] = f"{attribution(sd, sm)} Via publicdata.au, version {sm.version}."
        agent_demo["version"] = sm.version
        agent_demo["licence"] = sd.licence.title
    page(
        "index.html",
        "home.html",
        home_md,
        title=f"{HOST}: Australian government open data as CSV, Excel, JSON, Parquet and SQLite",
        description="Australian government open data as dated versions that keep their content, in eleven formats, with a query API, a browser explorer and an MCP server for AI agents. No keys, no accounts.",
        nav="datasets",
        headline=brand.HEADLINE,
        stats=stats,
        backlog_preview=backlog_rows[:5],
        hero_map=hero_map,
        demo=demo,
        query_demo=query_demo,
        agent_demo=agent_demo,
        keeps=keeps,
        preview=preview,
        jur_rows=dirx.jur_rows,
        tool_count=len(at.tool_names()),
        catalogue_total=fmt_int(dirx.listed) if dirx.listed else "",
        topics=topic_rows,
        newest=newest,
        # The home page alone hides the rows past the showcase; the rows are shared with the
        # topic and government pages, so the flag goes on copies.
        showcase=[{**live_rows[o.dataset.slug], "more": False} for o in showcase],
        more_rows=[{**r, "more": True} for r in rows if r["slug"] not in used],
        becomes=becomes,
        jsonld=json.dumps(home_jsonld, ensure_ascii=False),
        extra_jsonld=[mcp_jsonld, json.dumps(SOURCE_JSONLD, ensure_ascii=False)],
    )

    # Topic pages: what is served under each topic, and what is coming.
    for tr in topic_rows:
        members = [
            live_rows[o.dataset.slug]
            for o in sorted(
                (o for o in live if tr["slug"] in o.dataset.topics),
                key=lambda o: -o.latest.rows,
            )
        ]
        coming = [
            _row(d, None)
            for d in datasets
            if tr["slug"] in d.topics and d.status in ("building", "backlog", "assessing")
        ]
        topic_md = "\n".join(
            [
                "---",
                f"title: {tr['name']}",
                f"resource: {SITE}{tr['href']}",
                "---",
                "",
                f"# {tr['name']}",
                "",
                tr["blurb"],
                "",
                *[
                    f"- [{r['title']}]({dataset_url(r['slug'])}): {r['summary']} {r['publisher_name']}, {r['licence']}, latest {r['latest']}, {r['rows']} rows."
                    for r in members
                ],
                "",
                *([f"Coming: {', '.join(r['title'] for r in coming)}.", ""] if coming else []),
                f"Every dataset on the portals can be searched at {SITE}{tr['search']} and voted for.",
                "",
            ]
        )
        page(
            f"topics/{tr['slug']}/index.html",
            "topic.html",
            topic_md,
            title=f"{tr['name']}: Australian government open data | {HOST}",
            description=f"{tr['blurb']} {tr['count']} dataset{'' if tr['count'] == 1 else 's'} served as CSV, JSON, Parquet and more, with a query API and an MCP server.",
            nav="datasets",
            topic=tr,
            cards=members,
            coming=coming,
            extra_jsonld=[
                json.dumps(
                    _breadcrumbs([("Datasets", SITE + "/"), (tr["name"], SITE + tr["href"])]),
                    ensure_ascii=False,
                )
            ],
        )

    # Backlog.
    present = {p.jurisdiction for p in dirx.pubs.values()}
    all_rows = [_row(d, by_slug.get(d.slug)) for d in datasets]
    order = {"live": 0, "building": 1, "backlog": 2, "assessing": 2, "blocked": 3}
    all_rows.sort(key=lambda r: (order[r["status"]], r["title"]))
    backlog_md = "\n".join(
        [
            "---",
            "title: The backlog",
            f"resource: {SITE}/backlog/",
            "---",
            "",
            "# The backlog",
            "",
            "Datasets are built in vote order. One click is one vote. Nobody is asked who they are. Only a licence can block a dataset.",
            "",
            *(
                [
                    f"Every dataset on Australia's government portals, {fmt_int(dirx.listed)} of them, can be searched at {SITE}/api/v1/catalogue?q=<words>. "
                    f"{fmt_int(dirx.votable)} have an open licence and a file or API we can read, and those take a vote. "
                    f"A portal URL sent to POST {SITE}/api/v1/requests becomes a vote for the dataset it names.",
                    "",
                    "## Chosen for building",
                    "",
                ]
                if dirx.listed
                else []
            ),
            *[
                f"- **{r['title']}** ({r['publisher_name']}, {r['licence']}): {r['status_label']}. {r['summary']} {r['planned']} {r['blocked_reason']}".rstrip()
                for r in all_rows
            ],
            "",
            f"Vote with `POST {SITE}/api/v1/votes/<slug>`. Counts at {SITE}/api/v1/votes. Register at {SITE}/backlog.json.",
            "",
        ]
    )
    page(
        "backlog/index.html",
        "backlog.html",
        backlog_md,
        title=f"Backlog: search {fmt_int(dirx.listed)} government datasets and vote | {HOST}"
        if dirx.listed
        else f"Backlog | {HOST}",
        description=(
            f"Search all {fmt_int(dirx.listed)} datasets on Australia's government open-data portals and vote for the ones "
            "you want as CSV, Excel, JSON and Parquet. The most voted are built first."
            if dirx.listed
            else "Every dataset people have asked for, with votes, licence and status. Only a licence blocks a dataset."
        ),
        nav="backlog",
        rows=all_rows,
        catalogue_total=fmt_int(dirx.listed) if dirx.listed else "",
        jurisdictions=[
            {"seg": seg, "name": name} for code, seg, name in JURISDICTIONS if code in present
        ],
    )

    # What the MCP server lists under resources/list.
    _write(out, "mcp/resources.json", pretty({"resources": resources}))

    # Prose pages.
    publishers = sorted({o.dataset.publisher.name for o in live})
    for slug, (heading, desc, body) in PROSE.items():
        body = (
            body.replace("{publishers}", ", ".join(publishers))
            .replace("{query_api}", _agents_query_api())
            .replace("{webmcp}", _agents_webmcp())
            .replace("{mcp}", _agents_mcp())
            .replace("{mcp_privacy}", at.as_html(at.spec()["mcp"]["privacy"]))
            .replace("{terms_limits}", at.as_html(at.spec()["api"]["terms_limits"]))
            .replace("{terms_changed}", TERMS_CHANGED[0])
            .replace("{skill_path}", SKILL_PATH)
            .replace("{repo}", REPO)
            .replace("{gh}", GH_MARK)
        )
        md = "\n".join(
            [
                "---",
                f"title: {heading}",
                f"resource: {SITE}/{slug}/",
                "---",
                "",
                f"# {heading}",
                "",
                html_to_md(body),
            ]
        )
        page(
            f"{slug}/index.html",
            "page.html",
            md,
            title=f"{heading} | {HOST}",
            description=desc,
            nav=slug.split("/")[0],
            heading=heading,
            body=body,
            extra_jsonld=(
                [mcp_jsonld]
                if slug == "agents"
                else [json.dumps(SOURCE_JSONLD, ensure_ascii=False)]
                if slug == "contribute"
                else []
            ),
        )
    _write(
        out,
        "404.html",
        e.get_template("page.html").render(
            **common,
            canonical=SITE + "/404",
            md_url=SITE + "/index.md",
            title=f"Not found | {HOST}",
            description="Not found",
            nav="",
            heading="There is nothing at this address",
            body='<p>The datasets are listed on the <a href="/">home page</a>. Dated versions are kept, so a dataset URL that worked once still works unless its publisher asked for the dataset to be removed. Check the slug and the version date.</p>',
        ),
    )

    # Discovery and machine files.
    _write(
        out,
        "catalog.json",
        pretty(
            {
                "@context": {
                    "dcat": "http://www.w3.org/ns/dcat#",
                    "dct": "http://purl.org/dc/terms/",
                    "foaf": "http://xmlns.com/foaf/0.1/",
                    "publicdata": f"{SITE}/ns#",
                    "title": "dct:title",
                    "description": "dct:description",
                    "identifier": "dct:identifier",
                    "license": "dct:license",
                    "modified": "dct:modified",
                    "publisher": "dct:publisher",
                    "spatial": "dct:spatial",
                    "temporal": "dct:temporal",
                    "source": "dct:source",
                    "accrualPeriodicity": "dct:accrualPeriodicity",
                    "keyword": "dcat:keyword",
                    "landingPage": "dcat:landingPage",
                    "distribution": "dcat:distribution",
                    "accessURL": "dcat:accessURL",
                    "downloadURL": "dcat:downloadURL",
                    "mediaType": "dcat:mediaType",
                    "byteSize": "dcat:byteSize",
                    "format": "dct:format",
                    "conformsTo": "dct:conformsTo",
                    "dataset": "dcat:dataset",
                    "versionInfo": "dcat:version",
                    "name": "foaf:name",
                    "homepage": "foaf:homepage",
                },
                "@type": "dcat:Catalog",
                "@id": f"{SITE}/catalog.json",
                "title": HOST,
                "description": "Versioned republication of Australian open government data. Independent of every publisher listed.",
                "homepage": SITE + "/",
                "publisher": {
                    "@type": "foaf:Organization",
                    "@id": OPERATOR_ORG["@id"],
                    "name": OPERATOR,
                    "homepage": OPERATOR_URL,
                },
                "modified": built_at[:10],
                "publicdata:notEndorsed": True,
                "dataset": [_dcat_dataset(o.dataset, o) for o in live],
            }
        ),
    )
    # The boundary layers rows can be joined to by their code, for the clients' boundary join.
    _write(
        out,
        "places.json",
        pretty(
            {
                "site": SITE,
                "datum": SPINE_DATUM,
                "layers": [
                    {
                        "key": layer.key,
                        "slug": layer.slug,
                        "title": layer.title,
                        "code": layer.code[0],
                        "name": layer.name[0],
                        "noun": layer.noun,
                        "version": by_slug[layer.slug].latest.manifest.version,
                        **(
                            {
                                "gpkg": f"{version_url(layer.slug, by_slug[layer.slug].latest.manifest.version)}data.gpkg"
                            }
                            if "data.gpkg" in by_slug[layer.slug].latest.files
                            else {}
                        ),
                    }
                    for layer in SPINE_LAYERS.values()
                    if layer.slug in by_slug and by_slug[layer.slug].versions
                ],
            }
        ),
    )
    _write(
        out,
        "backlog.json",
        pretty(
            {
                "site": SITE,
                "generated_from": "register",
                "entries": [
                    {
                        "slug": d.slug,
                        "title": d.title,
                        "status": d.status,
                        "status_label": STATUS_LABEL.get(d.status, d.status),
                        "publisher": {
                            "name": d.publisher.name,
                            "jurisdiction": d.publisher.jurisdiction,
                            "url": d.publisher.url,
                        },
                        "licence": {
                            "id": d.licence.id,
                            "title": d.licence.title,
                            "url": d.licence.url,
                            "evidence": d.licence.evidence,
                            "reviewed": d.licence.reviewed or None,
                        },
                        "source": d.source.url,
                        "summary": d.summary,
                        "planned": d.planned or None,
                        "blocked_reason": d.blocked_reason or None,
                        "page": dataset_url(d.slug) if d.slug in live_slugs else None,
                        "vote": f"{SITE}/api/v1/votes/{d.slug}"
                        if d.status not in ("live", "building")
                        else None,
                    }
                    for d in datasets
                ],
            }
        ),
    )
    _write(out, "latest.json", pretty({o.dataset.slug: o.latest.manifest.version for o in live}))
    # The publisher's files a register entry no longer republishes. R2 still holds the copies
    # made before, so the /d/ function refuses each path listed here.
    _write(
        out,
        "withheld.json",
        pretty(
            sorted(
                f"/d/{o.dataset.slug}/v/{v.manifest.version}/source.{v.manifest.ext}"
                for o in live
                if o.dataset.source_withheld
                for v in o.versions
            )
        ),
    )
    _write(out, "openapi.json", pretty(_openapi(live, queried)))
    _write(
        out,
        "health.json",
        pretty(
            {
                "status": "ok",
                "release": at.release(),
                "built_from": built_at,
                "datasets_live": len(live),
                "versions": sum(len(o.versions) for o in live),
                "rows": sum(o.latest.rows for o in live),
                "storage": fleet_from_build(live, dt.date.fromisoformat(built_at[:10])).as_json(),
            }
        ),
    )
    for_agents = (
        "No keys or accounts, and no download limit on files. "
        + at.spec()["api"]["rate_limit"]
        + " `latest/` redirects (302) to the newest dated version; dated versions keep their content. Every JSON, NDJSON, GeoJSON, Parquet and SQLite file carries a `publicdata` header with the publisher, licence, attribution, a ready-made `cite` string and the source SHA-256. "
        + at.spec()["site"]["suppressed"]
        + " When you show the data to a person, use the attribution string, say the file came from publicdata.au and link to the version URL."
    )
    query_line = (
        f"Query API: {SITE}/api/v1/datasets/<slug>/rows?field=eq.value&select=a,b&order=a.desc&limit=100, {SITE}/api/v1/datasets/<slug>/aggregate?group=field&metric=count,sum.field, and the same under /versions/<date>/ for an answer from that dated version alone; loaded versions at {SITE}/api/v1/datasets/<slug>/versions. "
        + FILTER_HELP
    )
    llms = [
        f"# {HOST}",
        "",
        "> " + at.spec()["site"]["summary"],
        "",
        "## For agents",
        "",
        for_agents,
        "",
        f"- Catalogue (DCAT JSON-LD): {SITE}/catalog.json",
        f"- Discovery manifest (ARD): {SITE}/.well-known/ard.json",
        f"- Agent skill: {SKILL_URL}",
        f"- API catalogue (RFC 9727): {SITE}/.well-known/api-catalog",
        f"- OpenAPI 3.1 for every path: {SITE}/openapi.json",
        "- MCP server: " + at.plain(at.spec()["mcp"]["intro"]),
        f"- {query_line}",
        f"- Every dataset on Australia's government portals, by government and publisher: {SITE}/browse/ (as data: {SITE}/catalogue/publishers.json, and catalogue.json on each publisher page)",
        f"- Backlog and licences: {SITE}/backlog.json",
        f"- Vote counts: {SITE}/api/v1/votes",
        f"- How to read the site: {SITE}/agents/index.md",
        "",
        "## Datasets",
        "",
    ]
    full = list(llms)
    for o in live:
        ds, m = o.dataset, o.latest.manifest
        vb = version_url(ds.slug, m.version)
        if ds.kind == "database":
            line = f"- [{ds.title}]({dataset_url(ds.slug)}): {ds.summary} Publisher {ds.publisher.name}. {ds.licence.title}. Latest {m.version}, {fmt_int(o.latest.rows)} rows in {len(ds.tables)} tables. DuckDB {vb}data.duckdb (attaches read-only over HTTPS) · Parquet per table {vb}tables/<table>.parquet · SQL {vb}schema.sql · Markdown {dataset_url(ds.slug)}index.md"
            llms.append(line)
            full += (
                [line]
                + [
                    f"  Table {t.name} ({fmt_int(o.latest.tables.get(t.name, 0))} rows): "
                    + ", ".join(f"{f.name} ({f.type})" for f in t.fields)
                    for t in ds.tables
                ]
                + [f"  View {v.name}: {v.description}" for v in ds.views]
            )
            if ds.licence.condition:
                full.append(f"  Licence condition: {ds.licence.condition}")
            continue
        files = " · ".join(
            f"{FORMAT_LABEL[f]} {vb}data.{f}"
            for f in ("parquet", "json", "csv", "xlsx", "sqlite", "duckdb", "geojson", "gpkg")
            if f"data.{f}" in o.latest.files
        )
        line = f"- [{ds.title}]({dataset_url(ds.slug)}): {ds.summary} Publisher {ds.publisher.name}. {ds.licence.title}. Latest {m.version}, {fmt_int(o.latest.rows)} rows, {len(ds.fields)} fields. {files} · Markdown {dataset_url(ds.slug)}index.md"
        llms.append(line)
        full += [line, "  Fields: " + ", ".join(f"{f.name} ({f.type})" for f in ds.fields)]
        if ds.key:
            full.append("  Key: " + ", ".join(ds.key))
        if ds.partition_by:
            full.append(
                "  Smaller files by: "
                + ", ".join(f"{p} ({vb}by/{p}/index.json)" for p in ds.partition_by)
            )
    for doc, name in ((llms, "llms.txt"), (full, "llms-full.txt")):
        doc += ["", "## Backlog", ""] + [
            f"- {d.title} ({d.publisher.name}, {d.licence.title}): {STATUS_LABEL.get(d.status, d.status)}"
            for d in datasets
            if d.slug not in live_slugs
        ]
        doc += [
            "",
            "## Pages",
            "",
            f"- {SITE}/backlog/index.md",
            f"- {SITE}/publishers/index.md",
            f"- {SITE}/publishers/stable-urls/index.md",
            f"- {SITE}/government/index.md",
            f"- {SITE}/agents/index.md",
            f"- {SITE}/about/index.md",
            f"- {SITE}/contribute/index.md",
            "",
            "## Source",
            "",
            f"- {REPO}: the code, under the AGPL. Contributions are welcome as pull requests.",
            "",
        ]
        _write(out, name, "\n".join(doc))
    _write(out, SKILL_PATH, _skill(for_agents, query_line))
    urn = lambda ns, n: f"urn:air:{HOST}:{ns}:{n}"  # noqa: E731
    host = {"displayName": HOST, "identifier": HOST, "logoUrl": f"{SITE}/icon-512.png"}
    mcp = {
        "identifier": urn("mcp", "server"),
        "displayName": "MCP server",
        "type": "application/mcp-server-card+json",
        "tags": ["mcp", "open-data"],
        "capabilities": at.tool_names(),
        "description": at.plain(at.spec()["mcp"]["intro"]),
        "url": f"{SITE}/mcp/server-card",
        "representativeQueries": [
            "mcp server for australian government data",
            "query australian open data from an agent",
            "count road crashes by year with an mcp tool",
        ],
    }
    skill = at.spec()["skill"]
    resources = [
        {
            "identifier": urn("catalog", "dcat"),
            "displayName": "Dataset catalogue",
            "type": "application/ld+json",
            "tags": ["dcat", "open-data"],
            "description": "DCAT catalogue of every live dataset with a distribution per format.",
            "url": f"{SITE}/catalog.json",
            "representativeQueries": [
                "which Australian government datasets are available as Parquet",
                "list open data on publicdata.au",
                "find a dataset by publisher",
            ],
        },
        {
            "identifier": urn("llms-txt", "index"),
            "displayName": "llms.txt",
            "type": "text/plain",
            "description": "Every dataset with its latest download URLs and instructions for agents.",
            "url": f"{SITE}/llms.txt",
            "representativeQueries": [
                "how do I download a dataset from publicdata.au",
                "what is the latest version of a dataset",
            ],
        },
        {
            "identifier": urn("openapi", "json"),
            "displayName": "OpenAPI description",
            "type": "application/json",
            "tags": ["openapi"],
            "description": "OpenAPI 3.1 document for every public path: catalogue, dataset files, versions, diffs, votes and requests.",
            "url": f"{SITE}/openapi.json",
            "representativeQueries": [
                "what endpoints does publicdata.au have",
                "how do I call the publicdata.au vote API",
                "openapi spec for publicdata.au",
            ],
        },
        {
            "identifier": urn("backlog", "json"),
            "displayName": "Backlog",
            "type": "application/json",
            "description": "Every requested dataset with status, licence and the reason if blocked.",
            "url": f"{SITE}/backlog.json",
            "representativeQueries": [
                "which datasets are blocked by licence",
                "what has been requested on publicdata.au",
            ],
        },
    ]
    ard = {
        "specVersion": "1.0",
        "host": host,
        "entries": [
            mcp,
            {
                "identifier": urn("skill", skill["name"]),
                "displayName": skill["title"],
                "type": ardspec.SKILL,
                "tags": ["agent-skills", "open-data"],
                "description": skill["description"],
                "url": SKILL_URL,
                "representativeQueries": skill["queries"],
            },
            {
                "identifier": urn("catalog", "site"),
                "displayName": "Catalogues and API description",
                "type": ardspec.CATALOGUE,
                "tags": ["dcat", "openapi", "open-data"],
                "description": "The DCAT catalogue, llms.txt, the OpenAPI description and the backlog.",
                "url": f"{SITE}/agents/ai-catalog.json",
                "representativeQueries": [
                    "list open data on publicdata.au",
                    "what endpoints does publicdata.au have",
                    "which datasets are blocked by licence",
                ],
            },
        ],
    }
    # Each dataset is a bundle of its files and the server that queries it, so a search service
    # can match a question to one dataset and still read only agent types at the top level.
    for o in live:
        ds, m = o.dataset, o.latest.manifest
        vb = version_url(ds.slug, m.version)
        ard["entries"].append(
            {
                "identifier": urn("dataset", ds.slug),
                "displayName": ds.title,
                "type": ardspec.CATALOGUE,
                "tags": ["open-data", ds.publisher.jurisdiction.lower(), ds.licence.id.lower()],
                "description": f"{ds.summary} Latest version {m.version}, {fmt_int(o.latest.rows)} rows. "
                + (
                    f"One DuckDB database of {len(ds.tables)} tables, and one Parquet file per table under tables/."
                    if ds.kind == "database"
                    else "Also JSON, CSV, SQLite, DuckDB and NDJSON at the same path."
                ),
                "url": f"{dataset_url(ds.slug)}ai-catalog.json",
                "version": m.version,
                "updatedAt": m.fetched_at,
                "representativeQueries": [
                    ds.title.lower(),
                    f"{ds.publisher.short.lower()} {ds.title.split(',')[0].lower()} data",
                    f"download {ds.title.split(',')[0].lower()} as parquet",
                ],
            }
        )
        files = [
            {
                "identifier": urn(f"dataset:{ds.slug}", name.replace("/", ".")),
                "displayName": f"{ds.title}, "
                + (
                    f"table {name[7:-8]}"
                    if name.startswith("tables/")
                    else FORMAT_LABEL.get(fmt, fmt)
                ),
                "type": MEDIA[fmt],
                "url": f"{vb}{name}",
                "version": m.version,
            }
            for name, fmt in _files_of(ds, o.latest)
        ]
        _write(
            out,
            f"d/{ds.slug}/ai-catalog.json",
            pretty({"specVersion": "1.0", "host": host, "entries": [*files, mcp]}),
        )
    _write(
        out,
        "agents/ai-catalog.json",
        pretty({"specVersion": "1.0", "host": host, "entries": resources}),
    )
    body = pretty(ard)
    _write(out, ".well-known/ard.json", body)
    _write(out, ".well-known/ai-catalog.json", body)
    _write(out, ".well-known/api-catalog", pretty(_api_catalog()))
    # What a connector directory's form asks for, so a submission copies it and never drifts.
    _write(out, "mcp/listing.json", pretty(at.directory_listing()))
    card = pretty(at.server_card())
    _write(out, "mcp/server-card", card)
    _write(out, ".well-known/mcp/server-card.json", card)
    _write(out, ".well-known/mcp-registry-auth", f"v=MCPv1; k=ed25519; p={MCP_REGISTRY_KEY}\n")
    _write(out, ".well-known/openai-apps-challenge", OPENAI_APPS_CHALLENGE)
    _write(
        out,
        ".well-known/glama.json",
        pretty({"$schema": "https://glama.ai/mcp/schemas/connector.json", "claim": GLAMA_CLAIM}),
    )
    _write(
        out,
        ".well-known/security.txt",
        "\n".join(
            [
                f"# Vulnerability disclosure policy for {SITE} (RFC 9116).",
                "Contact: mailto:security@nationaldigital.com.au",
                "Contact: https://nationaldigital.com.au/contact/",
                "Contact: https://github.com/National-Digital/publicdata.au/security/advisories/new",
                "Policy: https://github.com/National-Digital/publicdata.au/blob/main/SECURITY.md",
                f"Expires: {(dt.date.fromisoformat(built_at[:10]) + dt.timedelta(days=365)).isoformat()}T00:00:00.000Z",
                "Preferred-Languages: en-AU",
                f"Canonical: {SITE}/.well-known/security.txt",
                "",
            ]
        ),
    )
    _write(
        out,
        "manifest.webmanifest",
        brand.manifest(
            [
                ("Browse datasets", "/browse/"),
                ("Backlog", "/backlog/"),
                ("For agents", "/agents/"),
            ]
        ),
    )
    _write(
        out,
        "robots.txt",
        f"User-Agent: *\nAllow: /\nDisallow: /api/\n\nHost: {SITE}\nSitemap: {SITE}/sitemap.xml\n",
    )
    urls = [
        SITE + "/",
        f"{SITE}/backlog/",
        f"{SITE}/publishers/",
        f"{SITE}/publishers/stable-urls/",
        f"{SITE}/government/",
        f"{SITE}/agents/",
        f"{SITE}/about/",
        f"{SITE}/contribute/",
        f"{SITE}/privacy/",
        f"{SITE}/terms/",
    ]
    urls += [collection_url(c) for c in collections]
    urls += dir_urls
    # One sitemap per government and one for everything else, under a sitemap index, so no file
    # nears the 50,000 URLs a sitemap may hold.
    from .publishers import JUR_SEGMENT

    segs = set(JUR_SEGMENT.values())
    by_jur: dict[str, list[str]] = {}
    for u in urls:
        head = u.removeprefix(SITE).strip("/").split("/")[0]
        by_jur.setdefault(head if head in segs else "site", []).append(u)
    for o in live:
        coll = collection_url(o.dataset.collection) if o.dataset.collection else None
        seg = JUR_SEGMENT.get(dirx.ds_pub[o.dataset.slug].jurisdiction, "site")
        group = by_jur.setdefault(seg, [])
        if coll and coll in by_jur.get("site", []):
            by_jur["site"].remove(coll)
            group.append(coll)
        group.append(dataset_url(o.dataset.slug))
        group += place_urls.get(o.dataset.slug, [])
    children = []
    for name in sorted(by_jur, key=lambda k: (k != "site", k)):
        rel = f"sitemaps/{name}.xml"
        _write(
            out,
            rel,
            '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join(f"  <url><loc>{html.escape(u)}</loc></url>\n" for u in by_jur[name])
            + "</urlset>\n",
        )
        children.append(f"{SITE}/{rel}")
    _write(
        out,
        "sitemap.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <sitemap><loc>{u}</loc></sitemap>\n" for u in children)
        + "</sitemapindex>\n",
    )
    _write(
        out,
        "_headers",
        "\n".join(
            [
                "/*",
                *(f"  {k}: {v}" for k, v in SITE_HEADERS.items()),
                "",
                # Every static URL carries a content hash, and a map file is named by its bytes.
                "/static/*",
                "  Cache-Control: public, max-age=31536000, immutable",
                "",
                "/maps/*",
                "  Cache-Control: public, max-age=31536000, immutable",
                "",
                # The explorer compiles WebAssembly and starts workers from blob URLs, which the
                # rest of the site never needs. The vendor tree carries it for the DuckDB worker,
                # and its path holds a content hash, so its files never change.
                "/d/:slug/explore/*",
                "  ! Content-Security-Policy",
                f"  Content-Security-Policy: {explorer_csp(NO_FRAMES)}",
                "",
                "/d/:slug/embed/*",
                "  ! Content-Security-Policy",
                f"  Content-Security-Policy: {explorer_csp('*')}",
                "",
                "/static/vendor/*",
                "  ! Content-Security-Policy",
                "  ! Cache-Control",
                f"  Content-Security-Policy: {explorer_csp(NO_FRAMES)}",
                "  Cache-Control: public, max-age=31536000, immutable",
                "",
                "/*.json",
                "  Cache-Control: public, max-age=300",
                "",
                "/og/*",
                "  Cache-Control: public, max-age=86400",
                "",
                "/manifest.webmanifest",
                "  Content-Type: application/manifest+json; charset=utf-8",
                "",
                "/llms.txt",
                "  Content-Type: text/plain; charset=utf-8",
                "/mcp/server-card",
                "  Content-Type: application/mcp-server-card+json",
                "/.well-known/mcp/server-card.json",
                "  Content-Type: application/mcp-server-card+json",
                "/.well-known/ard.json",
                f"  Content-Type: {ardspec.CATALOGUE}",
                "/.well-known/ai-catalog.json",
                f"  Content-Type: {ardspec.CATALOGUE}",
                "/d/:slug/ai-catalog.json",
                f"  Content-Type: {ardspec.CATALOGUE}",
                "/agents/ai-catalog.json",
                f"  Content-Type: {ardspec.CATALOGUE}",
                "/.well-known/api-catalog",
                f'  Content-Type: application/linkset+json; profile="{RFC9727}"',
                '  Link: </.well-known/api-catalog>; rel="api-catalog"',
                "/.well-known/mcp-registry-auth",
                "  Content-Type: text/plain; charset=utf-8",
                "/.well-known/openai-apps-challenge",
                "  Content-Type: text/plain; charset=utf-8",
                "/llms-full.txt",
                "  Content-Type: text/plain; charset=utf-8",
                "",
                "/*.md",
                "  Content-Type: text/markdown; charset=utf-8",
                "",
                "/*.ndjson",
                "  Content-Type: application/x-ndjson",
                "/*.parquet",
                "  Content-Type: application/vnd.apache.parquet",
                "/*.sqlite",
                "  Content-Type: application/vnd.sqlite3",
                "/*.geojson",
                "  Content-Type: application/geo+json",
                "/*.zst",
                "  Content-Type: application/zstd",
                "/*.xlsx",
                "  Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "/*.arrow",
                "  Content-Type: application/vnd.apache.arrow.file",
                "/*.gpkg",
                "  Content-Type: application/geopackage+sqlite3",
                "/*.gz",
                "  Content-Type: application/gzip",
                "/*.sql",
                "  Content-Type: application/sql; charset=utf-8",
                "/*.csv-metadata.json",
                "  Content-Type: application/csvm+json",
                "",
            ]
        ),
    )
    # The function that reads R2 serves pages moved there too, and needs the same headers.
    _write(out, "static/page-headers.json", pretty(SITE_HEADERS))
    _write(out, "_routes.json", pretty({"version": 1, "include": list(ROUTES), "exclude": []}))
