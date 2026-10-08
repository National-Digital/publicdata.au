"""Copies each served dataset's newest version to Hugging Face, Zenodo and Kaggle.

Everything is read from the live site's catalogue, so a dataset or version the site serves is
picked up with no code or register change. Each hub keeps its own record of the newest version it
holds, and a dataset is uploaded only when the site has a newer one.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from html import escape
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from .cadence import kaggle_frequency
from .provenance import NOT_ENDORSED
from .register import GRANTS, LICENCE_CONDITIONS, OPEN_LICENCES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

SITE = "https://publicdata.au"
UA = "publicdata-hubs (+https://publicdata.au/)"
HUBS = ("huggingface", "zenodo", "kaggle")


@dataclass(frozen=True)
class Licence:
    id: str
    title: str
    url: str
    huggingface: str
    zenodo: str
    kaggle: str


# Each open licence the register allows, with its id on each hub: (Hugging Face, Zenodo, Kaggle).
# No hub has an id for an Australian port, and its nearest id is a different licence, so a port
# goes up as "other" (Zenodo's "Other (Attribution)") with the licence named and linked in the text. A test keeps this in step with the register.
HUB_IDS = {
    "CC-BY-4.0": ("cc-by-4.0", "cc-by-4.0", "CC-BY-4.0"),
    "CC-BY-3.0-AU": ("other", "other-at", "other"),
    "CC-BY-2.5-AU": ("other", "other-at", "other"),
    "CC0-1.0": ("cc0-1.0", "cc0-1.0", "CC0-1.0"),
    "CC-BY-SA-4.0": ("cc-by-sa-4.0", "cc-by-sa-4.0", "CC-BY-SA-4.0"),
    "CC-BY-SA-3.0-AU": ("other", "other-at", "other"),
    # Every admitted grant requires attribution, so Zenodo's "other (attribution)".
    **{g.id: (g.hub, f"{g.hub}-at", g.hub) for g in GRANTS.values() if not g.condition},
}
LICENCES = {
    url: Licence(lid, title, url, *HUB_IDS[lid])
    for lid, (title, url) in OPEN_LICENCES.items()
    if lid in HUB_IDS and lid not in LICENCE_CONDITIONS
}


@dataclass(frozen=True)
class Entry:
    slug: str
    title: str
    description: str
    publisher: str
    publisher_url: str
    licence: Licence
    attribution: str
    cite: str
    version: str
    keywords: tuple[str, ...]
    rows: int
    fields: tuple[dict, ...]
    formats: int
    files: dict = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)
    queryable: bool = False
    topics: tuple[str, ...] = ()
    cadence: str = ""
    search_title: str = ""
    sizes: dict = field(default_factory=dict)
    site: str = SITE

    @property
    def page(self) -> str:
        return f"{self.site}/d/{self.slug}/"

    @property
    def version_url(self) -> str:
        return f"{self.page}v/{self.version}/"

    @property
    def source_page(self) -> str:
        s = self.manifest.get("source") or {}
        if s.get("portal") and s.get("package"):
            return f"{s['portal'].rstrip('/')}/dataset/{s['package']}"
        return self.publisher_url


class Refused(Exception):
    """A dataset this job will not copy, with the reason."""


class Excluded(Refused):
    """A dataset the register keeps off the hubs on purpose, which is reported but is no failure."""


def entry(
    record: dict,
    versions: dict,
    schema: dict,
    manifest: dict,
    queryable: bool = False,
    site: str = SITE,
    registered=None,
) -> Entry:
    """One dataset's newest version as the hubs see it.

    `registered` is the dataset's register entry, when there is one, whose labels and descriptions
    fill fields the schema leaves bare.
    """
    lic = LICENCES.get(record.get("license") or "")
    if lic is None:
        msg = f"licence {record.get('license')!r} has no hub mapping"
        raise Refused(msg)
    version = record["versionInfo"]
    row = next((v for v in versions.get("versions", []) if v["version"] == version), None)
    if row is None:
        msg = f"version {version} is not in versions.json"
        raise Refused(msg)
    files = {
        d["format"]: d["downloadURL"]
        for d in record.get("distribution", [])
        if f"/v/{version}/" in d.get("downloadURL", "")
    }
    if "parquet" not in files:
        msg = f"version {version} has no parquet file"
        raise Refused(msg)
    pub = record.get("publisher") or {}
    fields = tuple(_described(schema.get("fields") or (), registered))
    return Entry(
        slug=record["identifier"],
        title=record["title"],
        description=record.get("description") or "",
        publisher=pub.get("name", ""),
        publisher_url=pub.get("homepage", ""),
        licence=lic,
        attribution=record["publicdata:attribution"],
        cite=record["publicdata:cite"],
        version=version,
        keywords=tuple(record.get("keyword") or ()),
        rows=int(row.get("rows") or 0),
        fields=fields,
        formats=len(record.get("distribution") or ()),
        files=files,
        manifest=manifest,
        queryable=queryable,
        topics=tuple(getattr(registered, "topics", ()) or ()),
        cadence=record.get("accrualPeriodicity") or "",
        search_title=getattr(registered, "search_title", "") or "",
        sizes={
            d["format"]: int(d.get("byteSize") or 0)
            for d in record.get("distribution", [])
            if f"/v/{version}/" in d.get("downloadURL", "")
        },
        site=site,
    )


def _described(fields, registered) -> list[dict]:
    """Every field with a description, so no hub shows a column without one.

    The description is the schema's own, else the register's description or label, else a label
    drafted from the field name.
    """
    from .register import draft_label

    reg = {f.name: f for f in getattr(registered, "fields", ()) or ()}
    names = [f["name"] for f in fields]
    out = []
    for f in fields:
        text = _text(f)
        if not text:
            r = reg.get(f["name"])
            text = (r and (r.description or r.label)) or draft_label(f["name"], names)
        out.append({**f, "description": " ".join(str(text).split())})
    return out


def authorised(record: dict, registered) -> None:
    """The register decides what may be copied, whatever the live catalogue says.

    The entry must exist, be live, hold an open licence, and name the same licence the catalogue
    states.
    """
    slug = record.get("identifier", "")
    if registered is None:
        msg = f"{slug} is in the catalogue but not in the register"
        raise Refused(msg)
    if getattr(registered, "status", "") != "live":
        msg = f"{slug} is {getattr(registered, 'status', 'unknown')} in the register, not live"
        raise Refused(msg)
    lid = registered.licence.id
    if lid not in OPEN_LICENCES:
        msg = f"{slug}: register licence {lid} is not open"
        raise Refused(msg)
    if lid in LICENCE_CONDITIONS:
        msg = f"{slug}: licence {lid} carries a condition of use that no hub can state, so it is not copied"
        raise Excluded(msg)
    if getattr(registered, "kind", "table") == "database":
        msg = f"{slug}: a database is served here only, not copied to the hubs"
        raise Excluded(msg)
    if OPEN_LICENCES[lid][1] != record.get("license"):
        msg = f"{slug}: the catalogue states {record.get('license')!r}, the register {OPEN_LICENCES[lid][1]!r}"
        raise Refused(msg)


def provenance(e: Entry) -> dict:
    """publicdata.json, which travels with every copy so the link back survives a re-upload."""
    return {
        "dataset": e.slug,
        "title": e.title,
        "version": e.version,
        "version_url": e.version_url,
        "dataset_page": e.page,
        "publisher": {"name": e.publisher, "url": e.publisher_url},
        "licence": {"id": e.licence.id, "title": e.licence.title, "url": e.licence.url},
        "attribution": e.attribution,
        "cite": e.cite,
        "not_endorsed": NOT_ENDORSED,
        "manifest": e.manifest,
    }


def _cell(s: str) -> str:
    return " ".join(str(s).split()).replace("|", "\\|")


def _text(f: dict) -> str:
    """A field's description, else its title unless the title only names the source cell."""
    for text in (f.get("description"), f.get("title")):
        if text and not text.strip().startswith("("):
            return text.strip()
    return ""


def _about(f: dict) -> str:
    return _cell(_text(f))


def readme(e: Entry, hub: str) -> str:
    """The Markdown body shared by the Hugging Face card and the Kaggle description."""
    held = {
        "huggingface": "This repository tags each version it holds as `v<date>`, so "
        f'`load_dataset("<repo>", revision="v{e.version}")` always returns these rows.',
        "kaggle": "Each Kaggle version of this dataset is one publicdata.au version, named in its notes.",
    }[hub]
    query = [
        "## Query it without downloading",
        "",
        (
            "The publicdata.au query API filters and totals the newest version of this dataset "
            "and needs no key:"
        ),
        "",
        "```",
        f"{e.site}/api/v1/datasets/{e.slug}/rows?limit=10",
        "```",
        "",
    ]
    lines = [
        f"# {e.title}",
        "",
        e.description,
        "",
        (
            f"This is a copy of the {e.version} version of this dataset on publicdata.au, "
            f"where it has {e.rows:,} rows and {len(e.fields)} fields. The same version is kept at "
            f"{e.version_url} in {e.formats} formats, with every earlier version"
            f"{' and a query API' if e.queryable else ''}."
        ),
        "",
        "## Attribution",
        "",
        (
            f"The data is published by {e.publisher} under {e.licence.title}, {e.licence.url}. "
            "The licence requires this attribution:"
        ),
        "",
        f"> {e.attribution}",
        "",
        "To cite this version, including where it was serialised:",
        "",
        f"> {e.cite}",
        "",
        NOT_ENDORSED,
        "",
        "## Versions",
        "",
        (
            "publicdata.au keeps a new version each time the publisher changes the source file, "
            f"and lists them all at {e.page}versions.json. {held}"
        ),
        "",
        *(query if e.queryable else ()),
        "## Fields",
        "",
        "| Field | Type | Description |",
        "| --- | --- | --- |",
    ]
    lines += [f"| `{f['name']}` | {f.get('type', '')} | {_about(f)} |" for f in e.fields]
    lines += [
        "",
        (
            "`publicdata.json` beside the data names the version, licence, attribution and the "
            "SHA-256 of the publisher's file it was built from."
        ),
        "",
    ]
    return "\n".join(lines)


def _size_category(n: int) -> str:
    for limit, label in (
        (1_000, "n<1K"),
        (10_000, "1K<n<10K"),
        (100_000, "10K<n<100K"),
        (1_000_000, "100K<n<1M"),
        (10_000_000, "1M<n<10M"),
        (100_000_000, "10M<n<100M"),
    ):
        if n < limit:
            return label
    return "100M<n<1B"


def _tag(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# A Hugging Face card keeps keywords of up to three words as tags, fourteen tags in all.
HF_TAG_WORDS = 3
HF_TAGS = 14


def hf_card(e: Entry, repo: str) -> str:
    tags = ["australia", "government", "open-data", "publicdata-au"]
    for k in e.keywords:
        t = _tag(k)
        if t and t.count("-") < HF_TAG_WORDS and t not in tags and len(tags) < HF_TAGS:
            tags.append(t)
    meta = {
        "license": e.licence.huggingface,
        **(
            {"license_name": e.licence.id.lower(), "license_link": e.licence.url}
            if e.licence.huggingface == "other"
            else {}
        ),
        "pretty_name": e.title,
        "language": ["en"],
        "tags": tags,
        "size_categories": [_size_category(e.rows)],
        "configs": [{"config_name": "default", "data_files": "data.parquet"}],
    }
    body = readme(e, "huggingface").replace("<repo>", repo)
    return "---\n" + _yaml(meta) + "---\n\n" + body


def _yaml(meta: dict) -> str:
    """Card frontmatter. JSON strings are valid YAML scalars, so no YAML library is needed."""
    out = []
    for k, v in meta.items():
        if isinstance(v, list):
            out.append(f"{k}:")
            for item in v:
                if isinstance(item, dict):
                    first, *rest = item.items()
                    out.append(f"- {first[0]}: {json.dumps(first[1])}")
                    out += [f"  {a}: {json.dumps(b)}" for a, b in rest]
                else:
                    out.append(f"- {json.dumps(item)}")
        else:
            out.append(f"{k}: {json.dumps(v)}")
    return "\n".join(out) + "\n"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _clip(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n]
    for sep in (", ", " "):
        if sep in cut:
            return cut[: cut.rindex(sep)].rstrip(" ,")
    return cut


# The column types Kaggle documents. A type outside them loses the file's description and columns
# on upload, so every field maps onto one of these; coordinates are typed by their name.
KAGGLE_TYPES = {
    "integer": "numeric",
    "number": "numeric",
    "boolean": "boolean",
    "date": "datetime",
    "datetime": "datetime",
}
LATITUDE = re.compile(r"(^|_)lat(itude)?$")
LONGITUDE = re.compile(r"(^|_)(lon|lng|long|longitude)$")


def kaggle_type(f: dict) -> str:
    if f.get("type") in ("number", "integer"):
        if LATITUDE.search(f["name"]):
            return "latitude"
        if LONGITUDE.search(f["name"]):
            return "longitude"
    return KAGGLE_TYPES.get(f.get("type", ""), "string")


def located(e: Entry) -> bool:
    return any(kaggle_type(f) in ("latitude", "longitude") for f in e.fields)


# Each hub's search filters count the formats a dataset carries, so every hub gets the formats
# its filters list beside the Parquet and CSV, except a file too large to be worth the upload.
EXTRA_MAX = 500_000_000
KAGGLE_EXTRA_MAX = EXTRA_MAX
FORMAT_NAMES = {
    "parquet": "Parquet",
    "csv": "CSV",
    "json": "JSON",
    "sqlite": "SQLite",
}
KAGGLE_FILES = {f: FORMAT_NAMES[f] for f in ("parquet", "csv", "json", "sqlite")}
# The Hub loads a repository with one builder, so a second format in it breaks its viewer; the
# Parquet is the one its viewer and load_dataset read.
HF_FORMATS = ("parquet",)
HF_STRAY = ["data.csv", "data.json", "data.jsonl", "data.sqlite", "data.xlsx"]
ZENODO_FORMATS = ("parquet", "csv", "json", "xlsx")


def carried(e: Entry, formats: Iterable[str]) -> list[str]:
    return [
        fmt
        for fmt in formats
        if fmt in e.files and (fmt in ("parquet", "csv") or 0 < e.sizes.get(fmt, 0) <= EXTRA_MAX)
    ]


def kaggle_formats(e: Entry) -> list[str]:
    return carried(e, KAGGLE_FILES)


# Kaggle's limits on a dataset's slug, title and subtitle.
KAGGLE_SLUG_MIN, KAGGLE_SLUG_MAX = 3, 50
KAGGLE_TITLE_MIN, KAGGLE_TITLE_MAX = 6, 50
KAGGLE_SUBTITLE_MIN = 20


def kaggle_metadata(e: Entry, owner: str) -> dict:
    if not KAGGLE_SLUG_MIN <= len(e.slug) <= KAGGLE_SLUG_MAX:
        msg = f"slug {e.slug!r} is outside Kaggle's 3 to 50 characters"
        raise Refused(msg)
    subtitle = _clip(f"{e.publisher}, republished by publicdata.au", 80)
    if len(subtitle) < KAGGLE_SUBTITLE_MIN:
        subtitle = _clip(f"{subtitle}, Australian government open data", 80)
    schema = {
        "fields": [
            {
                "name": f["name"],
                "description": _about(f),
                "type": kaggle_type(f),
            }
            for f in e.fields
        ]
    }
    return {
        # The register's search title is written for search, so it leads when Kaggle's 50
        # characters hold it whole.
        "title": (
            e.search_title
            if KAGGLE_TITLE_MIN <= len(e.search_title) <= KAGGLE_TITLE_MAX
            else _clip(e.title, KAGGLE_TITLE_MAX)
        ),
        "id": f"{owner}/{e.slug}",
        "subtitle": subtitle,
        "description": readme(e, "kaggle"),
        "isPrivate": False,
        "licenses": [{"name": e.licence.kaggle}],
        "keywords": kaggle_tags(e),
        "expectedUpdateFrequency": kaggle_frequency(e.cadence),
        "userSpecifiedSources": kaggle_sources(e),
        "resources": [
            *(
                {
                    "path": f"data.{fmt}",
                    "description": f"{e.title}, version {e.version}, as {KAGGLE_FILES[fmt]}."
                    + (" One table, named data." if fmt == "sqlite" else ""),
                    **({"schema": schema} if fmt in ("parquet", "csv") else {}),
                }
                for fmt in kaggle_formats(e)
            ),
            {
                "path": "publicdata.json",
                "description": "The version, licence, attribution and citation of these rows, and "
                "the URL, fetch time and SHA-256 of the publisher's file they were built from.",
            },
        ],
    }


# Kaggle keeps only tags that already exist and drops the rest, so each topic offers several.
TOPIC_TAGS = {
    "roads": ["transportation", "automobiles and vehicles", "public safety", "road safety"],
    "crime": ["crime", "law", "public safety", "criminology"],
    "housing": ["housing", "real estate", "cities and urban areas"],
    "migration": ["demographics", "social science", "people", "immigration"],
    "nature": ["biology", "plants", "environment", "earth and nature", "animals", "water bodies"],
    "government": ["government", "business", "companies"],
    "economy": ["economics", "finance", "banking", "business"],
    "education": ["education", "schools"],
}


TAG_LIMIT = 10

# Kaggle names a licence one way when a dataset is created and another in a settings update.
# Each name here was sent in a live update and read back as the same licence; a licence without
# one is refused before anything is uploaded.
KAGGLE_SETTINGS_LICENCE = {
    "CC-BY-4.0": "CC BY 4.0",
    "CC-BY-SA-4.0": "CC BY-SA 4.0",
    "CC0-1.0": "CC0 1.0",
    "other": "other",
}


def kaggle_settings_licence(e: Entry) -> str:
    name = KAGGLE_SETTINGS_LICENCE.get(e.licence.kaggle)
    if name is None:
        msg = f"no Kaggle settings name has been verified for {e.licence.kaggle}"
        raise Refused(msg)
    return name


# A keyword of one or two words becomes a Kaggle tag.
KAGGLE_TAG_WORDS = 2


def kaggle_tags(e: Entry) -> list[str]:
    tags = ["australia", "government", "tabular", "public data"]
    if located(e):
        tags.append("geospatial analysis")
    for t in e.topics:
        tags += TOPIC_TAGS.get(t, [t])
    tags += [k.lower() for k in e.keywords if len(k.split()) <= KAGGLE_TAG_WORDS]
    seen: list[str] = []
    for t in tags:
        if t not in seen:
            seen.append(t)
    return seen[:20]


def kaggle_sources(e: Entry) -> str:
    """The provenance Kaggle shows under Sources, as its Markdown."""
    m = e.manifest
    fetched = (m.get("fetched_at") or "")[:10]
    parts = [
        (
            f"Published by [{e.publisher}]({e.publisher_url}) on [the publisher's dataset page]"
            f"({e.source_page}) under [{e.licence.title}]({e.licence.url})."
        ),
        f"These rows are the [{e.version} version]({e.version_url}) on [publicdata.au]({e.page}), "
        f"built from the publisher's file `{m.get('filename', '')}`"
        + (f" fetched on {fetched}" if fetched else "")
        + (f", SHA-256 `{m['sha256']}`" if m.get("sha256") else "")
        + ".",
        f"Attribution: {e.attribution}",
        NOT_ENDORSED,
    ]
    return "\n\n".join(parts)


NOTEBOOK_SLUG_MAX = KAGGLE_TITLE_MAX - len(" quick start")


def _notebook_title(e: Entry) -> str:
    # Kaggle derives a notebook's slug from its title, so the title is the dataset's slug, which
    # is unique, and the notebook's slug is known before the first push. Titles stop at 50
    # characters, so a long slug is cut and keeps a short hash of the whole slug.
    if len(e.slug) <= NOTEBOOK_SLUG_MAX:
        return f"{e.slug} quick start"
    digest = hashlib.sha256(e.slug.encode()).hexdigest()[:4]
    return f"{e.slug[:33].rstrip('-')} {digest} quick start"


def kaggle_notebook(e: Entry, owner: str) -> tuple[dict, dict]:
    """A public starter notebook over the dataset: its kernel-metadata.json and the notebook."""
    title = _notebook_title(e)
    meta = {
        "id": f"{owner}/{title.replace(' ', '-')}",
        "title": title,
        "code_file": "notebook.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": "false",
        "enable_gpu": "false",
        "enable_internet": "false",
        "dataset_sources": [f"{owner}/{e.slug}"],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }
    text_fields = [f["name"] for f in e.fields if f.get("type") == "string"]
    cells = [
        _md(
            f"# {e.title}\n\n{e.description}\n\n"
            f"These rows are the [{e.version} version]({e.version_url}) on "
            f"[publicdata.au]({e.page}), published by {e.publisher} under "
            f"[{e.licence.title}]({e.licence.url}). {NOT_ENDORSED}"
        ),
        _code(
            "import glob, json\n"
            "import pandas as pd\n\n"
            "folder = glob.glob('/kaggle/input/**/data.parquet', recursive=True)[0].rsplit('/', 1)[0]\n"
            "df = pd.read_parquet(f'{folder}/data.parquet')\n"
            "about = json.load(open(f'{folder}/publicdata.json'))\n"
            "print(about['attribution'])\n"
            "df.shape"
        ),
        _code("df.head(10)"),
        _md("## Fields"),
        _code("df.describe(include='all').T"),
    ]
    if text_fields:
        cells += [
            _md("## Rows by category\n\nThe first text field with between 2 and 30 values."),
            _code(
                "cats = [c for c in df.columns if df[c].dtype == object and 1 < df[c].nunique() <= 30]\n"
                "df[cats[0]].value_counts(dropna=False) if cats else 'No field has 2 to 30 values.'"
            ),
        ]
    cells += [
        _md(
            "## More\n\n"
            f"Every version of this dataset is listed at {e.page}versions.json. "
            + (
                f"The query API filters and totals it without a download: "
                f"`{e.site}/api/v1/datasets/{e.slug}/rows?limit=10`. "
                if e.queryable
                else ""
            )
            + "Outside Kaggle, `pip install publicdata-au` reads any publicdata.au dataset by "
            f'its slug, as in `publicdata_au.read("{e.slug}")`.\n\n'
            f"To cite this version: {e.cite}"
        ),
    ]
    nb = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"}
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return meta, nb


def _md(text: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": text}


def _code(text: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {},
        "execution_count": None,
        "outputs": [],
        "source": text,
    }


# Kaggle shows a cover at 2:1.
COVER_ASPECT = 2


def cover_image(card: Path, dest: Path) -> Path:
    """The site's social card cut to 2:1 about its centre and sized for Kaggle.

    Kaggle crops its header at 560 by 280 from the top left and its thumbnail as the middle
    square, so the card is sized to exactly that.
    """
    from PIL import Image

    with Image.open(card) as im:
        im = im.convert("RGB")
        w, h = im.size
        if w / h > COVER_ASPECT:
            nw = h * 2
            im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        else:
            nh = w // 2
            im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
        im.resize((560, 280), Image.LANCZOS).save(dest, "PNG")
    return dest


def zenodo_metadata(e: Entry, community: str | None = None) -> dict:
    p = lambda s: f"<p>{escape(s)}</p>"  # noqa: E731
    link = lambda u: f'<a href="{escape(u)}">{escape(u)}</a>'  # noqa: E731
    description = "".join(
        [
            p(e.description),
            (
                f"<p>This is the {escape(e.version)} version of this dataset on publicdata.au, with "
                f"{e.rows:,} rows and {len(e.fields)} fields. The same version is kept at "
                f"{link(e.version_url)} in {e.formats} formats, with every earlier version"
                f"{' and a query API' if e.queryable else ''}. Each Zenodo version of this record "
                "is one publicdata.au version.</p>"
            ),
            (
                f"<p>The data is published by {escape(e.publisher)} under {escape(e.licence.title)}, "
                f"{link(e.licence.url)}. The licence requires this attribution:</p>"
            ),
            f"<blockquote>{escape(e.attribution)}</blockquote>",
            p(NOT_ENDORSED),
            (
                f"<p>The rows are in {escape(_and([f'data.{f}' for f in carried(e, ZENODO_FORMATS)]))}. "
                "schema.json describes the fields, and publicdata.json names the version, licence, "
                "attribution and the SHA-256 of the publisher's file.</p>"
            ),
        ]
    )
    related = [
        {"identifier": e.version_url, "relation": "isIdenticalTo", "resource_type": "dataset"},
        {"identifier": e.page, "relation": "isVersionOf", "resource_type": "dataset"},
    ]
    if e.source_page:
        related.append(
            {"identifier": e.source_page, "relation": "isDerivedFrom", "resource_type": "dataset"}
        )
    meta = {
        "upload_type": "dataset",
        "title": e.title,
        "publication_date": e.version,
        "version": e.version,
        "creators": [{"name": e.publisher}],
        "contributors": [{"name": "National Digital", "type": "DataCurator"}],
        "description": description,
        "access_right": "open",
        "license": e.licence.zenodo,
        "keywords": ["Australia", "government open data", "publicdata.au", *e.keywords][:20],
        "related_identifiers": related,
        "language": "eng",
        "notes": f"Cite as: {e.cite}",
    }
    if community:
        meta["communities"] = [{"identifier": community}]
    return meta


# Network. Each hub answers two questions: which version it holds, and how to add one.


def _http() -> requests.Session:
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers["User-Agent"] = UA
    # A run reads a few thousand files from the site, and one dropped connection should not cost
    # a dataset its copies.
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    s.mount("https://", requests.adapters.HTTPAdapter(max_retries=retry))
    return s


def read_site(
    site: str = SITE, only: set[str] | None = None, http=None, register: dict | None = None
):
    """Yields (slug, Entry or Refused) for every dataset in the live catalogue.

    `register` maps a slug to its register entry; when it is given, a dataset the register does
    not authorise is refused before anything is read or copied.
    """
    http = http or _http()
    checked = register is not None
    register = register or {}

    def get(url):
        r = http.get(url, timeout=60)
        r.raise_for_status()
        return r.json()

    def served(slug):
        r = http.get(f"{site}/api/v1/datasets/{slug}/versions", timeout=60)
        return r.status_code == HTTPStatus.OK

    for rec in get(f"{site}/catalog.json")["dataset"]:
        slug = rec["identifier"]
        if only and slug not in only:
            continue
        try:
            if checked:
                authorised(rec, register.get(slug))
            base = f"{site}/d/{slug}/"
            v = rec["versionInfo"]
            yield (
                slug,
                entry(
                    rec,
                    get(f"{base}versions.json"),
                    get(f"{base}v/{v}/schema.json"),
                    get(f"{base}v/{v}/manifest.json"),
                    served(slug),
                    site,
                    register.get(slug),
                ),
            )
        except Refused as err:
            yield slug, err
        except (requests.RequestException, KeyError) as err:
            yield slug, Refused(str(err))


def download(url: str, dest: Path, http=None) -> Path:
    http = http or _http()
    with http.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with Path(dest).open("wb") as f:
            f.writelines(r.iter_content(1 << 20))
    return dest


class HuggingFace:
    name = "huggingface"

    def __init__(self, token: str, namespace: str):
        from huggingface_hub import HfApi

        self.api = HfApi(token=token)
        self.namespace = namespace

    def repo(self, e: Entry) -> str:
        return f"{self.namespace}/{e.slug}"

    @property
    def account(self) -> str:
        return f"https://huggingface.co/{self.namespace}"

    def location(self, e: Entry) -> str | None:
        return f"https://huggingface.co/datasets/{self.repo(e)}" if self.held(e) else None

    def held(self, e: Entry) -> set[str]:
        from huggingface_hub.errors import RepositoryNotFoundError

        try:
            refs = self.api.list_repo_refs(self.repo(e), repo_type="dataset")
        except RepositoryNotFoundError:
            return set()
        return {t.name[1:] for t in refs.tags if t.name.startswith("v")}

    def files(self, e: Entry, work: Path, fetch: Callable) -> list[Path]:
        (work / "README.md").write_text(hf_card(e, self.repo(e)), "utf-8")
        _write_json(work / "publicdata.json", provenance(e))
        paths = [work / "README.md", work / "publicdata.json"]
        for fmt in carried(e, HF_FORMATS):
            fetch(e.files[fmt], work / f"data.{fmt}")
            paths.append(work / f"data.{fmt}")
        return paths

    def publish(self, e: Entry, work: Path, fetch: Callable) -> str:
        self.files(e, work, fetch)
        repo = self.repo(e)
        self.api.create_repo(repo, repo_type="dataset", exist_ok=True, private=False)
        self.api.upload_folder(
            repo_id=repo,
            repo_type="dataset",
            folder_path=work,
            commit_message=f"publicdata.au version {e.version}",
            commit_description=e.version_url,
            delete_patterns=HF_STRAY,
        )
        self.api.create_tag(repo, tag=f"v{e.version}", repo_type="dataset", exist_ok=True)
        return f"https://huggingface.co/datasets/{repo}"

    def refresh(self, e: Entry, work: Path, fetch: Callable) -> str:
        """Rewrites the card, publicdata.json and the data files of a version already held.

        That way a format added since the version was published reaches the repository's copy.
        """
        self.files(e, work, fetch)
        self.api.upload_folder(
            repo_id=self.repo(e),
            repo_type="dataset",
            folder_path=work,
            commit_message=f"Card for publicdata.au version {e.version}",
            delete_patterns=HF_STRAY,
        )
        return f"https://huggingface.co/datasets/{self.repo(e)}"


ZENODO_PAGE = 100


class Zenodo:
    name = "zenodo"

    def __init__(self, token: str, base: str = "https://zenodo.org", community: str | None = None):
        self.base = base.rstrip("/")
        self.community = community
        self.pause = 30.0
        self.http = _http()
        self.http.headers["Authorization"] = f"Bearer {token}"
        self._records: list[dict] | None = None

    def _call(self, method: str, path: str, **kw):
        url = path if path.startswith("http") else f"{self.base}/api{path}"
        r = self.http.request(method, url, timeout=kw.pop("timeout", 120), **kw)
        if r.status_code >= HTTPStatus.BAD_REQUEST:
            msg = f"Zenodo {method} {url} answered {r.status_code}: {r.text[:500]}"
            raise RuntimeError(msg)
        return r.json() if r.content else {}

    def _put_file(self, url: str, p: Path, tries: int = 4) -> None:
        # A large upload sometimes meets a 502 or 504 from Zenodo's gateway; the file is sent again.
        for attempt in range(tries):
            try:
                with Path(p).open("rb") as fh:
                    self._call(
                        "PUT",
                        url,
                        data=fh,
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=1800,
                    )
                return
            except RuntimeError as err:
                if attempt + 1 == tries or not re.search(r"answered 5\d\d", str(err)):
                    raise
                time.sleep(self.pause * (attempt + 1))

    def records(self) -> list[dict]:
        if self._records is None:
            out, page = [], 1
            while True:
                batch = self._call(
                    "GET",
                    "/deposit/depositions",
                    params={"size": ZENODO_PAGE, "page": page, "all_versions": "true"},
                )
                out += batch
                if len(batch) < ZENODO_PAGE:
                    break
                page += 1
            self._records = out
        return self._records

    @staticmethod
    def _page(rec: dict) -> set[str]:
        return {
            r.get("identifier")
            for r in (rec.get("metadata") or {}).get("related_identifiers") or ()
            if r.get("relation") == "isVersionOf"
        }

    def _ours(self, e: Entry) -> list[dict]:
        # Oldest first, so a dataset Zenodo holds under two concept records keeps growing the
        # original and is located by it.
        return sorted(
            (r for r in self.records() if e.page in self._page(r)), key=lambda r: int(r["id"])
        )

    def _search(self, e: Entry) -> list[dict]:
        """The records that name this dataset, asked for directly.

        The full listing is paged, and a page boundary can lose a record while Zenodo is still
        indexing the last publish; a query for one dataset has no boundary to lose it at.
        """
        return self._call(
            "GET",
            "/deposit/depositions",
            params={
                "size": 100,
                "all_versions": "true",
                "q": f'metadata.related_identifiers.identifier:"{e.page}"',
            },
        )

    def _remember(self, *recs: dict, forget: Iterable[int] = ()) -> None:
        # Publishing is the only change this run makes, so the listing is kept in step by hand
        # rather than read again while Zenodo's index is still settling.
        gone = set(forget)
        kept = [r for r in self.records() if r["id"] not in gone]
        ids = {r["id"] for r in kept}
        self._records = kept + [r for r in recs if r["id"] not in ids]

    account = None

    def location(self, e: Entry) -> str | None:
        """The concept DOI, which resolves to the newest version and names them all."""
        for r in self._ours(e):
            if r.get("submitted") and r.get("conceptdoi"):
                return r["conceptdoi"]
        return None

    def held(self, e: Entry) -> set[str]:
        return {
            (r.get("metadata") or {}).get("version") for r in self._ours(e) if r.get("submitted")
        } - {None}

    def files(self, e: Entry, work: Path, fetch: Callable) -> list[Path]:
        _write_json(work / "publicdata.json", provenance(e))
        _write_json(work / "schema.json", {"fields": list(e.fields)})
        paths = []
        for fmt in carried(e, ZENODO_FORMATS):
            fetch(e.files[fmt], work / f"data.{fmt}")
            paths.append(work / f"data.{fmt}")
        return [*paths, work / "schema.json", work / "publicdata.json"]

    def publish(self, e: Entry, work: Path, fetch: Callable) -> str:
        paths = self.files(e, work, fetch)
        mine = self._ours(e)
        if not mine:
            found = self._search(e)
            if found:
                self._remember(*found)
                mine = self._ours(e)
        # A draft left by a run that failed before publishing is discarded, so it cannot block
        # the new version and never gets a DOI.
        dropped = []
        for r in mine:
            if not r.get("submitted"):
                self._call("DELETE", f"/deposit/depositions/{r['id']}")
                dropped.append(r["id"])
        published = [r for r in mine if r.get("submitted")]
        if published:
            newest = max(published, key=lambda r: (r.get("metadata") or {}).get("version", ""))
            made = self._call("POST", f"/deposit/depositions/{newest['id']}/actions/newversion")
            draft = self._call("GET", made["links"]["latest_draft"])
            for f in draft.get("files") or ():
                self._call("DELETE", f"/deposit/depositions/{draft['id']}/files/{f['id']}")
        else:
            draft = self._call("POST", "/deposit/depositions", json={})
        # The metadata goes first, so Zenodo checks it before any file is sent, and a draft that
        # fails at any step is deleted, since a draft without metadata is never matched again.
        try:
            self._call(
                "PUT",
                f"/deposit/depositions/{draft['id']}",
                json={"metadata": zenodo_metadata(e, self.community)},
            )
            bucket = draft["links"]["bucket"]
            for p in paths:
                self._put_file(f"{bucket}/{p.name}", p)
            done = self._call("POST", f"/deposit/depositions/{draft['id']}/actions/publish")
            if "metadata" not in done:
                done = self._call("GET", f"/deposit/depositions/{draft['id']}")
        except Exception:
            # The original error is the one worth reporting; a failed cleanup must not replace it.
            with contextlib.suppress(Exception):
                self._call("DELETE", f"/deposit/depositions/{draft['id']}")
            self._remember(forget=dropped)
            raise
        self._remember(done, forget=dropped)
        return done.get("doi_url") or done.get("links", {}).get("html", "")


KAGGLE_PAGE = 200
KAGGLE_TRIES = 4
# At 15 seconds a wait, a new dataset has five minutes to be registered.
KAGGLE_UNREGISTERED_WAITS = 20


class Kaggle:
    name = "kaggle"

    def __init__(self, owner: str, token: str, cli: str = "kaggle"):
        self.owner = owner
        self.token = token
        self.cli = cli
        self.pause = 20.0
        self._mine: set[str] | None = None
        self._bad_tags: set[str] = set()
        self._notebooks_limited = False

    def _run(self, *args: str, tries: int = 6) -> subprocess.CompletedProcess:
        # The CLI reads its token from KAGGLE_API_TOKEN; the legacy KAGGLE_KEY is not read.
        # A caller with its own retries, or one a refusal still counts against, passes tries=1.
        env = {**os.environ, "KAGGLE_API_TOKEN": self.token}
        for attempt in range(tries):
            r = subprocess.run(
                [self.cli, *args], capture_output=True, text=True, timeout=3600, env=env
            )
            if not self._throttled(r) or attempt == tries - 1:
                return r
            time.sleep(self.pause * (attempt + 1))
        return r

    def mine(self) -> set[str]:
        """The account's own dataset slugs.

        Listing them proves the token, and Kaggle answers 403 rather than 404 for a dataset that
        does not exist, so existence is read from here.
        """
        if self._mine is None:
            found, page = set(), 1
            while True:
                r = self._run(
                    "datasets",
                    "list",
                    "--mine",
                    "--csv",
                    "--page-size",
                    str(KAGGLE_PAGE),
                    "-p",
                    str(page),
                )
                out = (r.stdout + r.stderr).strip()
                if self._throttled(r):
                    msg = f"Kaggle kept refusing the dataset list: {out[:300]}"
                    raise RuntimeError(msg)
                if r.returncode != 0 or re.search(
                    r"\b(401|403)\b|forbidden|unauthori[sz]ed", out, re.IGNORECASE
                ):
                    msg = f"Kaggle refused the credentials: {out[:300]}"
                    raise RuntimeError(msg)
                refs = [
                    row["ref"]
                    for row in csv.DictReader(io.StringIO(r.stdout))
                    if row.get("ref", "").count("/") == 1
                ]
                found |= {ref.split("/")[1].lower() for ref in refs}
                if len(refs) < KAGGLE_PAGE:
                    break
                page += 1
            self._mine = found
        return self._mine

    @property
    def account(self) -> str:
        return f"https://www.kaggle.com/{self.owner}"

    def location(self, e: Entry) -> str | None:
        ref = f"{self.owner}/{e.slug}"
        if e.slug.lower() in self.mine() or self.exists(ref):
            return f"https://www.kaggle.com/datasets/{ref}"
        return None

    def exists(self, ref: str) -> bool:
        """Whether a dataset exists, asking Kaggle directly when its list does not name it.

        Kaggle's list of an account's datasets lags behind a new one, so a slug the list does not
        name is asked about directly before it is taken to be absent.
        """
        r = self._run("datasets", "status", ref)
        # A throttled answer says nothing, and taking it as absent re-creates a dataset we hold.
        if self._throttled(r):
            msg = f"Kaggle kept refusing the status of {ref}"
            raise RuntimeError(msg)
        return r.returncode == 0 and (r.stdout + r.stderr).strip().lower().endswith("ready")

    def held(self, e: Entry) -> set[str]:
        ref = f"{self.owner}/{e.slug}"
        if e.slug.lower() not in self.mine() and not self.exists(ref):
            return set()
        with tempfile.TemporaryDirectory() as d:
            r = self._run("datasets", "download", ref, "-f", "publicdata.json", "-p", d, "-o", "-q")
            for z in Path(d).glob("*.zip"):
                shutil.unpack_archive(z, d)
            if r.returncode != 0 or not (Path(d) / "publicdata.json").exists():
                out = (r.stdout + r.stderr).strip()
                msg = f"Kaggle could not read publicdata.json in {ref}: {out[:500]}"
                raise RuntimeError(msg)
            return {json.loads((Path(d) / "publicdata.json").read_text("utf-8"))["version"]}

    def files(self, e: Entry, work: Path, fetch: Callable) -> list[Path]:
        # Creating a dataset counts every tag offered, unknown ones included, against Kaggle's
        # category limit, so uploads carry none and the settings update sets them.
        _write_json(
            work / "dataset-metadata.json", {**kaggle_metadata(e, self.owner), "keywords": []}
        )
        _write_json(work / "publicdata.json", provenance(e))
        paths = [work / "publicdata.json"]
        for fmt in kaggle_formats(e):
            fetch(e.files[fmt], work / f"data.{fmt}")
            paths.append(work / f"data.{fmt}")
        return paths

    def publish(self, e: Entry, work: Path, fetch: Callable, first: bool) -> str:
        """Publishes a version, with every upload following the settings.

        Kaggle scores a dataset's page from its newest version, and attaches file and column
        descriptions only as files are uploaded. A new dataset is therefore created, given its
        settings and uploaded once more, and a later version is given its settings first.
        """
        ref = f"{self.owner}/{e.slug}"
        kaggle_settings_licence(e)
        self.files(e, work, fetch)
        meta = work.parent / f"{work.name}-meta"
        try:
            if first:
                # -t keeps the Parquet file as it is; Kaggle otherwise converts tables to CSV.
                r = self._run("datasets", "create", "-p", str(work), "-u", "-t", "-q")
                self._check(r, f"Kaggle refused {e.slug}")
                if self._mine is not None:
                    self._mine.add(e.slug.lower())
                self.wait_ready(ref)
                self.settings(e, meta, fetch)
                self.upload(e, work, f"Page settings for publicdata.au version {e.version}")
            else:
                self.wait_ready(ref)
                self.settings(e, meta, fetch)
                self.upload(e, work, f"publicdata.au version {e.version}, {e.version_url}")
            self.wait_ready(ref)
            # Sent again so the file and column descriptions apply to the new version's files.
            self.settings(e, meta, fetch)
            self.notebook(e, meta / "notebook")
        finally:
            shutil.rmtree(meta, ignore_errors=True)
        return f"https://www.kaggle.com/datasets/{ref}"

    def refresh(self, e: Entry, work: Path, fetch: Callable) -> str:
        """Gives a version Kaggle already holds the current settings and uploads its files again.

        The upload attaches their descriptions and has the page scored afresh, and the notebook is
        pushed after.
        """
        ref = f"{self.owner}/{e.slug}"
        data = work / "data"
        data.mkdir(parents=True, exist_ok=True)
        self.files(e, data, fetch)
        self.wait_ready(ref)
        self.settings(e, work / "meta", fetch)
        self.upload(e, data, f"Page settings for publicdata.au version {e.version}")
        self.wait_ready(ref)
        self.settings(e, work / "meta", fetch)
        self.notebook(e, work / "notebook")
        rating = self.usability(ref)
        url = f"https://www.kaggle.com/datasets/{ref}"
        return url if rating is None else f"{url} (usability {rating:.2f} before Kaggle rescores)"

    def upload(self, e: Entry, work: Path, message: str) -> None:
        for attempt in range(4):
            r = self._run(
                "datasets", "version", "-p", str(work), "-m", message, "-t", "-q", tries=1
            )
            if not self._throttled(r):
                break
            time.sleep(self.pause * (attempt + 1))
        self._check(r, f"Kaggle refused a version of {e.slug}")

    def settings(self, e: Entry, meta_dir: Path, fetch: Callable) -> None:
        """Tags, update frequency, sources and the cover image.

        Kaggle takes these only through a metadata update.
        """
        ref = f"{self.owner}/{e.slug}"
        meta_dir.mkdir(parents=True, exist_ok=True)
        fetch(f"{e.site}/og/d/{e.slug}.png", meta_dir / "card.png")
        cover_image(meta_dir / "card.png", meta_dir / "dataset-cover-image.png")
        (meta_dir / "card.png").unlink()
        # An update with a tag Kaggle does not have is refused whole, and the refusal names
        # the tags, so they are dropped for the rest of the run and the update sent again.
        # An update over Kaggle's category limit is refused whole too, so the list is cut from
        # the end, where the least specific tags are, until it fits.
        keep = TAG_LIMIT
        for attempt in range(8):
            meta = kaggle_metadata(e, self.owner)
            meta["keywords"] = [t for t in meta["keywords"] if t not in self._bad_tags][:keep]
            meta["licenses"] = [{"name": kaggle_settings_licence(e)}]
            _write_json(meta_dir / "dataset-metadata.json", meta)
            r = self._run("datasets", "metadata", ref, "--update", "-p", str(meta_dir), tries=1)
            out = r.stdout + r.stderr
            bad = re.search(r"keywords are invalid: (.+)", out)
            if bad:
                self._bad_tags |= set(re.findall(r'"([^"]+)"', bad.group(1)))
            elif re.search(r"max category limit", out, re.IGNORECASE) and keep > 1:
                keep = max(1, min(keep, len(meta["keywords"])) - 2)
            elif self._throttled(r):
                time.sleep(self.pause * (attempt + 1))
            else:
                break
        self._check(r, f"Kaggle refused the settings for {e.slug}")

    def has_notebook(self, e: Entry) -> bool:
        """True or False only when Kaggle says so.

        A 403 or 404 means absent, and any other failure, such as a throttled call, is retried and
        then raised, so a failed check never spends Kaggle's limit on saving notebooks.
        """
        ref = kaggle_notebook(e, self.owner)[0]["id"]
        for attempt in range(KAGGLE_TRIES):
            r = self._run("kernels", "status", ref, tries=1)
            out = r.stdout + r.stderr
            if r.returncode == 0:
                return True
            if re.search(r"\b(403|404)\b|Permission 'kernels\.get' was denied", out):
                return False
            if attempt < KAGGLE_TRIES - 1:
                time.sleep(self.pause * (attempt + 1))
        msg = f"Kaggle could not say whether {ref} exists: {out.strip()[:300]}"
        raise RuntimeError(msg)

    def ensure(self, e: Entry, work: Path, fetch: Callable) -> str | None:  # noqa: ARG002 - takes what refresh takes
        """Pushes the starter notebook a held dataset lacks.

        Such a dataset may be one Kaggle's limit on saving notebooks turned away. Returns what was
        done, or None when nothing was needed.
        """
        if self._notebooks_limited or self.has_notebook(e):
            return None
        if not self.push_notebook(e, work / "notebook"):
            return "notebook deferred until Kaggle's limit on saving notebooks resets"
        return "added its notebook"

    def notebook(self, e: Entry, nb_dir: Path) -> None:
        # A notebook reads the newest data each time it runs, so it is pushed once, and Kaggle's
        # limit on saving notebooks is spent only on datasets that have none.
        if not self._notebooks_limited and not self.has_notebook(e):
            self.push_notebook(e, nb_dir)

    def push_notebook(self, e: Entry, nb_dir: Path) -> bool:
        """True once pushed, and False when Kaggle's limit on saving notebooks turned it away.

        The rest of the run then pushes none, as each refused push counts against the limit, and a
        later run adds the notebooks still missing.
        """
        nb_dir.mkdir(parents=True, exist_ok=True)
        kmeta, nb = kaggle_notebook(e, self.owner)
        _write_json(nb_dir / "kernel-metadata.json", kmeta)
        _write_json(nb_dir / "notebook.ipynb", nb)
        for attempt in range(KAGGLE_TRIES):
            r = self._run("kernels", "push", "-p", str(nb_dir), tries=1)
            out = r.stdout + r.stderr
            if re.search(r"\b429\b|Too Many Requests", out):
                self._notebooks_limited = True
                return False
            # Each pushed notebook runs, and Kaggle runs only a few at once per account.
            if re.search(r"Maximum batch CPU session count", out):
                if attempt == KAGGLE_TRIES - 1:
                    self._notebooks_limited = True
                    return False
                time.sleep(self.pause * 3 * (attempt + 1))
                continue
            if not self._throttled(r):
                break
            time.sleep(self.pause * (attempt + 1))
        if r.returncode != 0 and re.search(r"403 .*SaveKernel", r.stdout + r.stderr):
            msg = (
                f"Kaggle refused the notebook for {e.slug} with 403. An account must be phone "
                "verified before it can save notebooks; the dataset and its settings are in place, "
                "and a --refresh run adds the notebook once the account is verified."
            )
            raise RuntimeError(msg)
        self._check(r, f"Kaggle refused the notebook for {e.slug}")
        return True

    def wait_ready(self, ref: str, tries: int = 60, pause: float = 15) -> None:
        # A new version is processed before its settings can change or a notebook can mount it.
        # Kaggle answers 403 for a large new dataset it has not registered yet, so the first
        # few minutes of 403s are waited out.
        for n in range(tries):
            r = self._run("datasets", "status", ref)
            out = (r.stdout + r.stderr).strip().lower()
            if r.returncode == 0 and out.endswith("ready"):
                return
            if re.search(r"\b403\b|\b404\b", out) and n < KAGGLE_UNREGISTERED_WAITS:
                time.sleep(pause)
                continue
            if r.returncode != 0 or "error" in out or "failed" in out:
                msg = f"Kaggle could not process {ref}: {out[:300]}"
                raise RuntimeError(msg)
            time.sleep(pause)
        msg = f"Kaggle was still processing {ref} after {tries * pause:.0f} seconds"
        raise RuntimeError(msg)

    @staticmethod
    def _throttled(r: subprocess.CompletedProcess) -> bool:
        # Kaggle answers a burst of calls with a page the CLI cannot parse as JSON, or a 429,
        # and the CLI prints the 429 and still exits 0.
        out = r.stdout + r.stderr
        return bool(
            re.search(
                r"Expecting value: line 1|\b(429|50[234]) (Client|Server) Error|Too Many Requests",
                out,
            )
        )

    def usability(self, ref: str) -> float | None:
        """Kaggle's own usability rating, from 0 to 1, as it stands now."""
        with tempfile.TemporaryDirectory() as d:
            r = self._run("datasets", "metadata", ref, "-p", d)
            f = Path(d) / "dataset-metadata.json"
            if r.returncode != 0 or not f.exists():
                return None
            meta = json.loads(f.read_text("utf-8"))
            return (meta.get("info") or meta).get("usabilityRating")

    @staticmethod
    def _check(r: subprocess.CompletedProcess, what: str) -> None:
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0 or re.search(r"\berror\b", out, re.IGNORECASE):
            msg = f"{what}: {out[:500]}"
            raise RuntimeError(msg)


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")


def configured(env=os.environ) -> tuple[dict, list[str]]:
    """The hubs whose credentials are present, and a line for each one that is skipped."""
    hubs, skipped = {}, []
    if env.get("HF_TOKEN"):
        hubs["huggingface"] = lambda: HuggingFace(
            env["HF_TOKEN"], env.get("HF_NAMESPACE") or "publicdata-au"
        )
    else:
        skipped.append("huggingface: HF_TOKEN is not set, so nothing was copied there.")
    if env.get("ZENODO_TOKEN"):
        hubs["zenodo"] = lambda: Zenodo(
            env["ZENODO_TOKEN"],
            env.get("ZENODO_URL") or "https://zenodo.org",
            env.get("ZENODO_COMMUNITY") or None,
        )
    else:
        skipped.append("zenodo: ZENODO_TOKEN is not set, so nothing was copied there.")
    if env.get("KAGGLE_API_TOKEN") and env.get("KAGGLE_USERNAME"):
        hubs["kaggle"] = lambda: Kaggle(
            env["KAGGLE_USERNAME"], env["KAGGLE_API_TOKEN"], env.get("KAGGLE_CLI") or "kaggle"
        )
    else:
        skipped.append(
            "kaggle: KAGGLE_USERNAME and KAGGLE_API_TOKEN are not both set, so nothing was copied there."
        )
    return hubs, skipped


def run(
    hubs: dict,
    entries,
    fetch: Callable | None = None,
    work_root: Path | None = None,
    log: Callable = print,
    refresh: bool = False,
    record: dict | None = None,
    on_record: Callable | None = None,
) -> int:
    """Publishes each entry's version to each hub that lacks it.

    One failure is reported and the rest carry on; the return is the number of failures. Where
    each copy is goes into `record`, and `on_record` is called with it after every dataset so a
    caller can save as the run goes.
    """
    fetch = fetch or download
    failures = 0
    entries = list(entries)
    for name, hub in hubs.items():
        for slug, e in entries:
            if isinstance(e, Excluded):
                log(f"{name} {slug}: not copied, {e}")
                continue
            if isinstance(e, Refused):
                log(f"{name} {slug}: REFUSED {e}")
                failures += 1
                continue
            try:
                _one(name, hub, slug, e, fetch, work_root, log, refresh)
            except Exception as err:  # noqa: BLE001
                failures += 1
                log(f"{name} {slug}: FAILED {type(err).__name__}: {err}")
                continue
            if record is not None and hasattr(hub, "location"):
                try:
                    where = hub.location(e)
                except Exception as err:  # noqa: BLE001
                    log(f"{name} {slug}: location not read: {err}")
                    continue
                if where:
                    record.setdefault("datasets", {}).setdefault(slug, {})[name] = where
                    if on_record:
                        on_record(record)
        if record is not None and getattr(hub, "account", None):
            record.setdefault("accounts", {})[name] = hub.account
    return failures


def _one(
    name: str,
    hub,
    slug: str,
    e: Entry,
    fetch: Callable,
    work_root: Path | None,
    log: Callable,
    refresh: bool,
) -> None:
    held = hub.held(e)
    if e.version in held:
        if refresh and hasattr(hub, "refresh"):
            work = Path(tempfile.mkdtemp(prefix=f"{name}-{slug}-", dir=work_root))
            try:
                url = hub.refresh(e, work, fetch)
            finally:
                shutil.rmtree(work, ignore_errors=True)
            log(f"{name} {slug}: refreshed {e.version} {url}")
        elif hasattr(hub, "ensure"):
            work = Path(tempfile.mkdtemp(prefix=f"{name}-{slug}-", dir=work_root))
            try:
                done = hub.ensure(e, work, fetch)
            finally:
                shutil.rmtree(work, ignore_errors=True)
            log(f"{name} {slug}: holds {e.version}" + (f", {done}" if done else ""))
        else:
            log(f"{name} {slug}: holds {e.version}")
        return
    if held and max(held) > e.version:
        log(f"{name} {slug}: holds {max(held)}, newer than the site's {e.version}")
        return
    work = Path(tempfile.mkdtemp(prefix=f"{name}-{slug}-", dir=work_root))
    try:
        if isinstance(hub, Kaggle):
            url = hub.publish(e, work, fetch, first=not held)
        else:
            url = hub.publish(e, work, fetch)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log(f"{name} {slug}: published {e.version} {url}")


def merge_record(old: dict, new: dict) -> dict:
    """The committed record with this run's findings laid over it.

    A dataset or hub this run did not reach keeps what was recorded, so a run limited to some
    datasets loses nothing.
    """
    datasets = {k: dict(v) for k, v in (old.get("datasets") or {}).items()}
    for slug, hubs_ in (new.get("datasets") or {}).items():
        datasets.setdefault(slug, {}).update(hubs_)
    accounts = {**(old.get("accounts") or {}), **(new.get("accounts") or {})}
    return {
        "accounts": dict(sorted(accounts.items())),
        "datasets": {k: dict(sorted(v.items())) for k, v in sorted(datasets.items())},
    }


def render(
    entries, out: Path, namespace: str = "publicdata-au", owner: str = "publicdataau"
) -> None:
    """Writes what each hub would receive, without the data files, for review."""
    for slug, e in entries:
        d = out / slug
        d.mkdir(parents=True, exist_ok=True)
        if isinstance(e, Refused):
            (d / "REFUSED.txt").write_text(f"{e}\n", "utf-8")
            continue
        (d / "huggingface-README.md").write_text(hf_card(e, f"{namespace}/{slug}"), "utf-8")
        _write_json(d / "kaggle-dataset-metadata.json", kaggle_metadata(e, owner))
        kmeta, nb = kaggle_notebook(e, owner)
        _write_json(d / "kaggle-kernel-metadata.json", kmeta)
        _write_json(d / "kaggle-notebook.ipynb", nb)
        _write_json(d / "zenodo-metadata.json", zenodo_metadata(e))
        _write_json(d / "publicdata.json", provenance(e))
