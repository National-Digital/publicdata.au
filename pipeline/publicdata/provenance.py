"""The provenance header that goes inside every payload."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, NotRequired, TypedDict

from . import OPERATOR, SITE
from .spine import ATTRIBUTION as SPINE_ATTRIBUTION
from .spine import LAYERS

if TYPE_CHECKING:
    from .jsontypes import JSON
    from .register import Dataset
    from .store import Manifest

    class HeaderOperator(TypedDict):
        name: str
        url: str

    class HeaderPublisher(TypedDict):
        name: str
        jurisdiction: str
        url: str

    class HeaderLicence(TypedDict):
        id: str
        title: str
        url: str
        condition: NotRequired[str]

    class HeaderSource(TypedDict):
        url: str | None
        filename: str
        fetched_at: str
        sha256: str
        bytes: int
        encoding: str
        backfilled: bool

    class HeaderPlaces(TypedDict):
        datasets: list[str]
        attribution: str

    class HeaderPartition(TypedDict):
        field: str
        value: JSON

    class _Condition(TypedDict, total=False):
        condition: str

    class _Places(TypedDict, total=False):
        places: HeaderPlaces

    class _Database(TypedDict, total=False):
        kind: str
        tables: list[str]

    class Header(TypedDict):
        """The header every payload carries, as JSON, in the order header() below writes it."""

        site: str
        operator: HeaderOperator
        dataset: str
        title: str
        version: str
        as_at: str | None
        url: str
        publisher: HeaderPublisher
        licence: HeaderLicence
        attribution: str
        cite: str
        cite_request: str
        source: HeaderSource
        places: NotRequired[HeaderPlaces]
        rows: int
        fields: int
        kind: NotRequired[str]
        tables: NotRequired[list[str]]
        not_endorsed: str
        partition: NotRequired[HeaderPartition]


NOT_ENDORSED = "This is an independent republication. The publisher has not endorsed this site."
OPERATOR_URL = "https://nationaldigital.com.au/"
# The operator's own @id, so its node merges with the one its website publishes.
OPERATOR_ORG = {
    "@type": "Organization",
    "@id": "https://nationaldigital.com.au/#organization",
    "name": OPERATOR,
    "url": OPERATOR_URL,
}
CITE_REQUEST = (
    "The licence requires the publisher's attribution. We ask that you also name "
    "publicdata.au and link to the version you used."
)


def long_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%B %Y')}"


def attribution(ds: Dataset, m: Manifest) -> str:
    return ds.licence.attribution.format(sourced=long_date(m.fetched_at))


def landing(ds: Dataset) -> str:
    return ds.landing or ds.licence.evidence or ds.source.url


def cite(ds: Dataset, m: Manifest, version_url: str) -> dict[str, str]:
    """The licence attribution plus the operator's request, in the forms people paste."""
    name = ds.collection_title or ds.title
    lic = ds.licence
    when = long_date(m.fetched_at)
    page = landing(ds)
    year = m.version[:4]
    key = f"publicdata_{ds.slug}_{m.version}".replace("-", "_")
    return {
        "text": f"{attribution(ds, m)} Serialised and versioned by {OPERATOR} at publicdata.au, {version_url}",
        "html": (
            f'Data: <a href="{page}">{name}</a>, {ds.publisher.name}, '
            f'<a href="{lic.url}">{lic.title}</a>, sourced {when}. '
            f'Files: <a href="{version_url}">publicdata.au</a> by <a href="{OPERATOR_URL}">{OPERATOR}</a>.'
        ),
        "markdown": (
            f"Data: [{name}]({page}), {ds.publisher.name}, [{lic.title}]({lic.url}), sourced {when}. "
            f"Files: [publicdata.au]({version_url}) by [{OPERATOR}]({OPERATOR_URL})."
        ),
        # Author-date, as the style guides departments use ask for. No access date: a dated
        # version is the same bytes whenever it is read.
        "harvard": (
            f"{ds.publisher.name} ({year}) {ds.title}, version {m.version} [data set], "
            f"{lic.title}, sourced {when}. Serialised and versioned by {OPERATOR} at publicdata.au. "
            f"Available at: {version_url}"
        ),
        # Double braces keep an organisation name as one author and the title's case.
        "bibtex": "\n".join(
            [
                f"@misc{{{key},",
                f"  author = {{{{{ds.publisher.name}}}}},",
                f"  title = {{{{{ds.title}}}}},",
                f"  year = {{{year}}},",
                f"  howpublished = {{publicdata.au, serialised and versioned by {OPERATOR}}},",
                f"  url = {{{version_url}}},",
                f"  note = {{Version {m.version}, {lic.title}. Source: {page}}}",
                "}",
            ]
        ),
    }


def header(ds: Dataset, m: Manifest, rows: int, url: str) -> Header:
    version_url = url.rsplit("/", 1)[0] + "/"
    condition: _Condition = {"condition": ds.licence.condition} if ds.licence.condition else {}
    places: _Places = (
        {
            "places": {
                "datasets": [LAYERS[k].slug for k in ds.enrich],
                "attribution": SPINE_ATTRIBUTION,
            }
        }
        if ds.enrich
        else {}
    )
    database: _Database = (
        {"kind": "database", "tables": [t.name for t in ds.tables]} if ds.kind == "database" else {}
    )
    return {
        "site": SITE,
        "operator": {"name": OPERATOR, "url": OPERATOR_URL},
        "dataset": ds.slug,
        "title": ds.title,
        "version": m.version,
        "as_at": m.as_at or None,
        "url": url,
        "publisher": {
            "name": ds.publisher.name,
            "jurisdiction": ds.publisher.jurisdiction,
            "url": ds.publisher.url,
        },
        "licence": {
            "id": ds.licence.id,
            "title": ds.licence.title,
            "url": ds.licence.url,
            **condition,
        },
        "attribution": attribution(ds, m),
        "cite": cite(ds, m, version_url)["text"],
        "cite_request": CITE_REQUEST,
        "source": {
            "url": m.source.get("url"),
            "filename": m.filename,
            "fetched_at": m.fetched_at,
            "sha256": m.sha256,
            "bytes": m.bytes,
            "encoding": m.encoding,
            "backfilled": m.backfilled,
        },
        **places,
        "rows": rows,
        "fields": ds.field_count,
        **database,  # type: ignore[typeddict-item]  # partition comes later, from write_partitions
        "not_endorsed": NOT_ENDORSED,
    }
