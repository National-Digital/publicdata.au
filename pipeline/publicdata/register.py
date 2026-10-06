"""Load and validate register/*.yaml. The register is the only input a human edits."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from .topics import TOPICS

OPEN_LICENCES = {
    "CC-BY-4.0": ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
    "CC-BY-3.0-AU": ("CC BY 3.0 AU", "https://creativecommons.org/licenses/by/3.0/au/"),
    "CC-BY-2.5-AU": ("CC BY 2.5 AU", "https://creativecommons.org/licenses/by/2.5/au/"),
    "CC0-1.0": ("CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    "CC-BY-SA-4.0": ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
    "CC-BY-SA-3.0-AU": ("CC BY-SA 3.0 AU", "https://creativecommons.org/licenses/by-sa/3.0/au/"),
}
# A grant that is not a Creative Commons licence is admitted from register/licences/, one file per
# grant quoting the publisher's words for each of GRANT_TESTS (see the README there). Grants join
# OPEN_LICENCES at the end of this module, and a grant's condition joins LICENCE_CONDITIONS: it is
# shown on every page and in every provenance header, and the hubs job never copies such a
# dataset, since a hub can only name a licence it has an id for.
GRANTS_DIR = Path(__file__).resolve().parents[2] / "register" / "licences"
GRANT_TESTS = ("reproduce", "adapt", "commercial", "attribution")
GRANT_KINDS = ("notice", "eula", "permission")
PERMISSION_RE = re.compile(r"^[A-Z0-9]+(-[A-Z0-9]+)*-PERMISSION-\d{4}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Ids the gate will never let through, whatever the status says.
CLOSED_LICENCES = {
    "CC-BY-ND-4.0",
    "CC-BY-NC-ND-4.0",
    "CC-BY-NC-4.0",
    "CC-BY-NC-SA-4.0",
    "CC-BY-ND-3.0-AU",
    "CC-BY-NC-3.0-AU",
    "CC-BY-NC-SA-3.0-AU",
    "CC-BY-NC-ND-3.0-AU",
}
# A dataset is one table, or a database: a publisher's release of several related tables that are
# served together as one DuckDB file and one Parquet file per table.
KINDS = ("table", "database")
# A wide table whose columns are dates is turned into one row per cell: the column's header goes
# to the field whose source is HEADER_SOURCE and the cell to the one whose source is CELL_SOURCE.
HEADER_SOURCE = "(column header)"
CELL_SOURCE = "(cell)"
# In a stack of files, the name the publisher gives each file, which often names its period.
FILE_SOURCE = "(file)"
# A GeoJSON source's point coordinates are read as two columns with these source names.
LON_SOURCE = "(longitude)"
LAT_SOURCE = "(latitude)"
UNPIVOT_HEADERS = ("date",)
# A presentation table, with header rows above and row headers to the left, is read cell by cell.
# Field sources name where each value comes from: the sheet, a row header or a header row by
# position, and the cell itself, or one cell per value of the last header row.
SHEET_SOURCE = "(sheet)"
ROW_HEADER_RE = re.compile(r"^\(row header (\d+)\)$")
COLUMN_HEADER_RE = re.compile(r"^\(column header (\d+)\)$")
CELL_OF_RE = re.compile(r"^\(cell: (.+)\)$")
WIDE_KEYS = {
    "sheets",
    "header_row",
    "header_rows",
    "first_column",
    "row_headers",
    "fill_down",
    "column_match",
    "sheet_match",
}
STATUSES = ("live", "building", "backlog", "blocked", "assessing")
TYPES = ("string", "integer", "number", "boolean", "date", "datetime")
JURISDICTIONS = ("Cth", "NSW", "Vic", "Qld", "WA", "SA", "Tas", "ACT", "NT", "Local")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class RegisterError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    name: str
    source: str
    type: str = "string"
    description: str = ""
    true_values: tuple[str, ...] = ("Yes", "YES", "Y", "true", "True")
    false_values: tuple[str, ...] = ("No", "NO", "N", "false", "False")
    date_format: str = "%Y-%m-%d"
    note: str = ""
    null_values: tuple[str, ...] = ()
    label: str = ""
    # In a database, the table and field this one refers to, as "table.field".
    references: str = ""

    @property
    def display(self) -> str:
        """The name a reader sees in the explorer: the register's label, or the field name as words."""
        return self.label or _words(self.name)


ACRONYMS = {"abs", "id", "lga", "dca", "nsw", "qld", "sa2", "sa3", "sa4"}


def draft_label(name: str, names) -> str:
    """A first label for review, from the field name: a prefix most of the dataset's fields share
    is dropped (crash_ in crash_severity), involving_x reads as X involved, and count_x as X."""
    heads = [n.split("_", 1)[0] for n in names if "_" in n]
    words = name.split("_")
    if len(words) > 1 and heads.count(words[0]) >= max(3, len(heads) // 4):
        words = words[1:]
    if words[0] == "count" and len(words) > 1:
        words = words[1:]
    tail = ""
    if words[0] == "involving" and len(words) > 1:
        words, tail = words[1:], " involved"
    return _words("_".join(words)) + tail


def _words(name: str) -> str:
    text = " ".join(w.upper() if w in ACRONYMS else w for w in name.split("_"))
    return text[:1].upper() + text[1:]


@dataclass(frozen=True)
class Publisher:
    name: str
    short: str
    jurisdiction: str
    url: str


@dataclass(frozen=True)
class Licence:
    id: str
    evidence: str
    attribution: str
    portal_id: str = ""
    # For a file with no portal record: words of the evidence page that grant the licence. The
    # fetch reads the page every run and stops when they are gone.
    statement: str = ""
    # The date the evidence page was read and the licence decided for this entry. Each fetch
    # records in the version's manifest where and when it read the licence again.
    reviewed: str = field(default="", repr=False, compare=False)

    @property
    def open(self) -> bool:
        return self.id in OPEN_LICENCES

    @property
    def title(self) -> str:
        return OPEN_LICENCES.get(self.id, (self.id, ""))[0]

    @property
    def url(self) -> str:
        return OPEN_LICENCES.get(self.id, ("", ""))[1]

    @property
    def condition(self) -> str:
        return LICENCE_CONDITIONS.get(self.id, "")


@dataclass(frozen=True)
class TableSpec:
    """One table of a database: its upstream name, its allow-listed fields and its key."""

    name: str
    source: str
    fields: tuple[Field, ...]
    description: str = ""
    key: tuple[str, ...] = ()

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)


@dataclass(frozen=True)
class View:
    """A query over the tables the publisher ships beside them, kept in the database file."""

    name: str
    sql: str
    description: str = ""
    # A column of the view the page's example query groups by.
    example: str = ""


@dataclass(frozen=True)
class Database:
    # A pattern matched against each member of the publisher's archive; its `table` group names
    # the table the member belongs to, so a release split by state loads as one table.
    member_match: str
    delimiter: str = ","
    encoding: str = "utf-8"


@dataclass(frozen=True)
class Source:
    adapter: str
    url: str
    portal: str = ""
    package: str = ""
    resource: str = ""
    cadence: str = ""
    encoding: str = ""
    # The column separator of a text file, when it is not a comma: "tab" or the character itself.
    delimiter: str = ""
    # True when the file's host turns automated clients away. The fetch reads the portal's record
    # and reports when the file has changed; a person downloads it and runs the fetch with --file.
    manual: bool = False
    # For a stack of workbooks whose header sits on a different row in each: the first cell of
    # the header row.
    header_match: str = ""
    # For a stack whose files each hold several small tables: a pattern for the title above each
    # table, whose `section` group names the table. Every cell is read as its own row. This and the
    # options below shape the fetched bytes, not the build, so they stay out of the repr the build
    # cache keys on.
    section_match: str = field(default="", repr=False)
    # For a stack: header rows below the matched one, a pattern for the rows that name a group
    # of the rows below them, footnote numbers at the end of the first cell, and a pattern whose
    # first group is what the (file) column takes from each file's name.
    header_depth: int = field(default=1, repr=False)
    group_match: str = field(default="", repr=False)
    footnote_marks: bool = field(default=False, repr=False)
    file_match: str = field(default="", repr=False)
    as_at_regex: str = ""
    sheet: str = ""
    header_row: int = 1
    # A publisher that adds a file (or a package) for each release instead of replacing one is
    # followed by name: the newest match is fetched.
    package_match: str = ""
    resource_match: str = ""
    # The data file inside a zip the publisher serves, by name.
    member: str = ""
    # An API that answers a search: the search, and the providers whose rows are taken, by id.
    search: str = ""
    providers: tuple[str, ...] = ()
    # A live feed: a view of what is current, with no change date of its own. It is fetched daily,
    # and each day it changes is one version dated by that day.
    feed: bool = False
    # The element each row is read from, for an XML file.
    record: str = ""
    # The file's format when its URL does not end in one, such as a WFS request: geojson, csv or xml.
    format: str = ""
    # A WFS GetFeature that caps each answer: the features asked for per request, read page by page
    # with startIndex into one GeoJSON file.
    page_size: int = 0


@dataclass(frozen=True)
class Dataset:
    slug: str
    title: str
    status: str
    publisher: Publisher
    licence: Licence
    source: Source
    description: str = ""
    summary: str = ""
    collection: str = ""
    collection_title: str = ""
    key: tuple[str, ...] = ()
    partition_by: tuple[str, ...] = ()
    # The Parquet's row order and its bloom-filtered fields (docs/adr/0008).
    sort: tuple[str, ...] = ()
    lookup: tuple[str, ...] = ()
    unpivot: str = ""
    wide: dict | None = None
    geometry: dict | None = None
    # The place spine layers a point dataset is joined to by location (spine.LAYERS).
    enrich: tuple[str, ...] = ()
    fields: tuple[Field, ...] = ()
    suppression: tuple[str, ...] = ()
    note: str = ""
    blocked_reason: str = ""
    planned: str = ""
    temporal_start: str = ""
    order: int = 100
    search_title: str = ""
    also_known_as: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    faq: tuple[tuple[str, str], ...] = ()
    collection_description: str = ""
    landing: str = ""
    row_label: str = ""
    topics: tuple[str, ...] = ()
    example: dict | None = None
    chart: dict | None = None
    # The rows the page shows as a sample. It shapes no version's bytes, so it stays out of the repr.
    sample: dict | None = field(default=None, repr=False)
    # Upstream columns knowingly left out, each with the reason, such as a third party's series.
    # They are recorded in the manifest and are not held as unknown.
    omit: dict[str, str] = field(default_factory=dict)
    # Why the publisher's own file is not republished beside each version, as when it carries a
    # column that omit leaves out for its terms. Empty republishes it.
    source_withheld: str = ""
    # The search phrase the collection page targets, given on the entry that carries the
    # collection description, so a collection and its largest table never target one phrase.
    collection_search_title: str = ""
    # A partition field whose values are places. Each value gets a page of its own rows.
    place_field: str = ""
    # False keeps a dataset out of the query API; its files are served as usual. Neither field
    # shapes a version's bytes, so both stay out of the repr the build cache keys on.
    query: bool = field(default=True, repr=False)
    path: str = field(default="", repr=False, compare=False)
    extra: dict = field(default_factory=dict)
    kind: str = "table"
    database: Database | None = None
    tables: tuple[TableSpec, ...] = ()
    views: tuple[View, ...] = ()

    @property
    def publishable(self) -> bool:
        return self.status in ("live", "building") and self.licence.open

    @property
    def field_count(self) -> int:
        """Every published field: the table's, or in a database every table's."""
        return sum(len(t.fields) for t in self.tables) if self.tables else len(self.fields)

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def table(self, name: str) -> TableSpec:
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(name)


def _bool(v, ctx: str) -> bool:
    if not isinstance(v, bool):
        raise RegisterError(f"{ctx}: must be true or false")
    return v


def _req(d: dict, key: str, ctx: str) -> object:
    if key not in d or d[key] in (None, ""):
        raise RegisterError(f"{ctx}: missing '{key}'")
    return d[key]


def parse(raw: dict, ctx: str) -> Dataset:
    slug = str(_req(raw, "slug", ctx))
    if not SLUG_RE.match(slug):
        raise RegisterError(f"{ctx}: bad slug '{slug}'")
    status = str(_req(raw, "status", ctx))
    if status not in STATUSES:
        raise RegisterError(f"{ctx}: status '{status}' not one of {STATUSES}")
    pub = raw.get("publisher") or {}
    publisher = Publisher(
        name=str(_req(pub, "name", f"{ctx}.publisher")),
        short=str(pub.get("short") or pub["name"]),
        jurisdiction=str(_req(pub, "jurisdiction", f"{ctx}.publisher")),
        url=str(pub.get("url", "")),
    )
    if publisher.jurisdiction not in JURISDICTIONS:
        raise RegisterError(f"{ctx}: jurisdiction '{publisher.jurisdiction}' unknown")
    lic = raw.get("licence") or {}
    licence = Licence(
        id=str(_req(lic, "id", f"{ctx}.licence")),
        evidence=str(lic.get("evidence", "")),
        attribution=str(lic.get("attribution", "")),
        portal_id=str(lic.get("portal_id", "")),
        statement=" ".join(str(lic.get("statement", "")).split()),
        reviewed=str(lic.get("reviewed", "")),
    )
    if licence.reviewed and not DATE_RE.match(licence.reviewed):
        raise RegisterError(f"{ctx}: licence.reviewed is a date, YYYY-MM-DD")
    src = raw.get("source") or {}
    source = Source(
        adapter=str(src.get("adapter", "none")),
        url=str(_req(src, "url", f"{ctx}.source")),
        portal=str(src.get("portal", "")),
        package=str(src.get("package", "")),
        resource=str(src.get("resource", "")),
        cadence=str(src.get("cadence", "")),
        encoding=str(src.get("encoding", "")),
        delimiter=str(src.get("delimiter", "")),
        manual=_bool(src.get("manual", False), f"{ctx}.source.manual"),
        header_match=str(src.get("header_match", "")),
        section_match=str(src.get("section_match", "")),
        header_depth=int(src.get("header_depth", 1)),
        group_match=str(src.get("group_match", "")),
        footnote_marks=_bool(src.get("footnote_marks", False), f"{ctx}.source.footnote_marks"),
        file_match=str(src.get("file_match", "")),
        as_at_regex=str(src.get("as_at_regex", "")),
        sheet=str(src.get("sheet", "")),
        header_row=int(src.get("header_row", 1)),
        package_match=str(src.get("package_match", "")),
        resource_match=str(src.get("resource_match", "")),
        member=str(src.get("member", "")),
        search=str(src.get("search", "")),
        providers=tuple(str(x) for x in (src.get("providers") or [])),
        feed=_bool(src.get("feed", False), f"{ctx}.source.feed"),
        record=str(src.get("record", "")),
        format=str(src.get("format", "")).lstrip(".").lower(),
        page_size=int(src.get("page_size", 0)),
    )
    if source.manual and source.adapter not in ("ckan-resource", "ckan-stack"):
        raise RegisterError(
            f"{ctx}: source.manual needs a ckan-resource or ckan-stack, whose portal records date it"
        )
    if "browser" in src:
        raise RegisterError(f"{ctx}: source.browser is now source.manual")
    if source.delimiter and source.delimiter != "tab" and len(source.delimiter) != 1:
        raise RegisterError(f"{ctx}: source.delimiter is 'tab' or one character")
    for k in ("package_match", "resource_match", "section_match", "group_match", "file_match"):
        try:
            re.compile(getattr(source, k))
        except re.error as e:
            raise RegisterError(f"{ctx}: source.{k} is not a valid pattern: {e}") from e
    if "chart_where" in raw:
        raise RegisterError(f"{ctx}: chart_where is now chart.where")
    kind = str(raw.get("kind", "table"))
    if kind not in KINDS:
        raise RegisterError(f"{ctx}: kind '{kind}' not one of {KINDS}")
    fields = _fields(raw.get("fields") or [], ctx)
    seen = {f.name for f in fields}
    geometry = _geometry(raw.get("geometry"), seen, ctx)
    enrich = tuple(str(x) for x in raw.get("enrich") or ())
    if enrich:
        from .spine import LAYERS, SOURCE_PREFIX

        if not geometry or geometry["kind"] != "point":
            raise RegisterError(
                f"{ctx}: enrich joins points to the place spine; declare point geometry"
            )
        for k in enrich:
            if k not in LAYERS:
                raise RegisterError(
                    f"{ctx}: enrich '{k}' is not a spine layer, one of {sorted(LAYERS)}"
                )
        if len(set(enrich)) < len(enrich):
            raise RegisterError(f"{ctx}: enrich names a layer twice")
        for k in enrich:
            layer = LAYERS[k]
            for name, label in (layer.code, layer.name):
                if name in seen:
                    raise RegisterError(
                        f"{ctx}: field '{name}' is the spine's; rename the publisher's"
                    )
                fields.append(
                    Field(
                        name=name,
                        source=f"{SOURCE_PREFIX}{k})",
                        label=label,
                        description=f"{'Code' if name == layer.code[0] else 'Name'} of the {layer.title} the point falls in. Joined by location from {layer.slug}, not published by the publisher.",
                    )
                )
                seen.add(name)
    key = tuple(raw.get("key") or ())
    partition_by = tuple(raw.get("partition_by") or ())
    for k in (*key, *partition_by):
        if k not in seen:
            raise RegisterError(f"{ctx}: key/partition field '{k}' is not a declared field")
    unpivot = str((raw.get("unpivot") or {}).get("headers", "")) if raw.get("unpivot") else ""
    if unpivot and raw.get("wide"):
        raise RegisterError(f"{ctx}: a table is read as wide or unpivoted, not both")
    if unpivot:
        if unpivot not in UNPIVOT_HEADERS:
            raise RegisterError(f"{ctx}: unpivot.headers '{unpivot}' not one of {UNPIVOT_HEADERS}")
        for marker in (HEADER_SOURCE, CELL_SOURCE):
            if sum(1 for f in fields if f.source == marker) != 1:
                raise RegisterError(
                    f"{ctx}: unpivot needs exactly one field with source '{marker}'"
                )
    wide = _wide(raw.get("wide"), fields, ctx) if raw.get("wide") else None
    ds = Dataset(
        slug=slug,
        title=str(_req(raw, "title", ctx)),
        status=status,
        publisher=publisher,
        licence=licence,
        source=source,
        description=str(raw.get("description", "")).strip(),
        summary=str(raw.get("summary", "")).strip(),
        collection=str(raw.get("collection", "")),
        collection_title=str(raw.get("collection_title", "")),
        key=key,
        partition_by=partition_by,
        **_profile(raw, fields, ctx),
        unpivot=unpivot,
        wide=wide,
        geometry=geometry,
        enrich=enrich,
        fields=tuple(fields),
        suppression=tuple(raw.get("suppression") or ()),
        note=str(raw.get("note", "")).strip(),
        blocked_reason=str(raw.get("blocked_reason", "")).strip(),
        planned=str(raw.get("planned", "")).strip(),
        temporal_start=str(raw.get("temporal_start", "")),
        order=int(raw.get("order", 100)),
        search_title=str(raw.get("search_title", "")).strip(),
        also_known_as=tuple(str(x) for x in raw.get("also_known_as") or ()),
        keywords=tuple(str(x) for x in raw.get("keywords") or ()),
        faq=tuple(
            (str(_req(q, "q", f"{ctx}.faq[{i}]")), str(_req(q, "a", f"{ctx}.faq[{i}]")))
            for i, q in enumerate(raw.get("faq") or ())
        ),
        collection_description=str(raw.get("collection_description", "")).strip(),
        landing=str(raw.get("landing", "")),
        row_label=str(raw.get("row_label", "")).strip(),
        topics=tuple(str(x) for x in raw.get("topics") or ()),
        example=_example(raw.get("example"), fields, ctx),
        chart=_chart(raw.get("chart"), fields, ctx),
        sample=_sample(raw.get("sample"), fields, ctx),
        collection_search_title=str(raw.get("collection_search_title", "")).strip(),
        place_field=str(raw.get("place_field", "")).strip(),
        query=_bool(raw.get("query", True), f"{ctx}.query") and kind == "table",
        omit=_omit(raw.get("omit"), fields, ctx),
        source_withheld=str(raw.get("source_withheld", "")).strip(),
        kind=kind,
        **(_database(raw, ctx) if kind == "database" else {}),
    )
    if kind == "database":
        for k in (
            "fields",
            "key",
            "partition_by",
            "sort",
            "lookup",
            "geometry",
            "wide",
            "unpivot",
            "omit",
        ):
            if raw.get(k):
                raise RegisterError(f"{ctx}: a database has no top-level {k}; it goes on a table")
        # A database's cells are typed by DuckDB with no suppressed flag, so a release that
        # suppresses cells is not a database until that is built.
        if raw.get("suppression"):
            raise RegisterError(f"{ctx}: a database does not support suppression")
        # The archive is linked whole, so there is no source file to leave out.
        if raw.get("source_withheld"):
            raise RegisterError(f"{ctx}: a database does not support source_withheld")
    labels = [f.display for f in ds.fields]
    if len(set(labels)) < len(labels):
        raise RegisterError(f"{ctx}: a spine column's label is also a publisher field's label")
    if ds.place_field and ds.place_field not in partition_by:
        raise RegisterError(f"{ctx}: place_field '{ds.place_field}' is not a partition_by field")
    if ds.collection_search_title and not ds.collection_description:
        raise RegisterError(
            f"{ctx}: collection_search_title goes on the entry with collection_description"
        )
    if ds.source.adapter == "ala" and not (ds.source.search and ds.source.providers):
        raise RegisterError(f"{ctx}: an ala source needs a search and providers")
    if ds.status in ("live", "building"):
        if not ds.licence.open:
            raise RegisterError(
                f"{ctx}: status '{ds.status}' needs an open licence, got {ds.licence.id}"
            )
        # A draft is built for review before a person has read the licence; a live entry has been.
        for k in ("evidence", "attribution", *(("reviewed",) if ds.status == "live" else ())):
            if not getattr(ds.licence, k):
                raise RegisterError(f"{ctx}: status '{ds.status}' needs licence.{k}")
        if ds.status == "live" and not (ds.tables if kind == "database" else ds.fields):
            raise RegisterError(f"{ctx}: a live dataset needs a field allow-list")
        if ds.status == "live":
            # The search-facing text is part of publishing, so a live entry cannot skip it.
            for k, n in (("search_title", 1), ("also_known_as", 2), ("keywords", 3), ("faq", 1)):
                v = getattr(ds, k)
                if (len(v) if isinstance(v, tuple) else int(bool(v))) < n:
                    raise RegisterError(
                        f"{ctx}: a live dataset needs {k}"
                        + (f" with at least {n} entries" if n > 1 else "")
                    )

        if ds.status == "live" and ds.source.adapter == "none":
            raise RegisterError(f"{ctx}: a live dataset needs a fetch adapter")
        # A topic is how the home page and the topic pages find a served entry.
        if ds.status == "live" and not ds.topics:
            raise RegisterError(f"{ctx}: a live dataset needs at least one topic")
        if (
            ds.source.adapter in ("file", "file-stack", "kiwis", "aihw")
            and not ds.licence.statement
        ):
            raise RegisterError(
                f"{ctx}: a {ds.source.adapter} source needs licence.statement, the evidence page's own words"
            )
        if ds.source.adapter == "aihw" and not ds.source.resource_match:
            raise RegisterError(
                f"{ctx}: an aihw source needs resource_match, the file's listed title"
            )
        if ds.source.adapter == "zenodo" and not (ds.source.package and ds.source.resource_match):
            raise RegisterError(
                f"{ctx}: a zenodo source needs package (the concept record id) and resource_match"
            )
        if ds.source.adapter == "kiwis" and not (
            ds.source.search and ds.source.package and ds.source.resource in ("stations", "values")
        ):
            raise RegisterError(
                f"{ctx}: a kiwis source needs search (the parameter), package (the series name) "
                "and resource, stations or values"
            )
        if ds.source.adapter == "ckan-stack" and not (
            ds.source.portal
            and ds.source.package
            and ds.source.package_match
            and ds.source.header_match
        ):
            raise RegisterError(
                f"{ctx}: a ckan-stack source needs portal, package (the search text), package_match "
                "and header_match"
            )
        if ds.source.adapter == "file-stack" and not (
            ds.source.resource_match and ds.source.header_match
        ):
            raise RegisterError(
                f"{ctx}: a file-stack source needs resource_match (the pattern each file's link "
                "matches) and header_match"
            )
        if ds.source.adapter == "ckan-resource":
            # With package_match, package is the search text; a blank search would see only the
            # portal's newest page of packages.
            if not ds.source.package:
                raise RegisterError(
                    f"{ctx}: source needs package, the search text for package_match"
                )
            if not (ds.source.resource or ds.source.resource_match):
                raise RegisterError(f"{ctx}: source needs resource or resource_match")
    for topic in ds.topics:
        if topic not in TOPICS:
            raise RegisterError(f"{ctx}: unknown topic '{topic}', the topics are {sorted(TOPICS)}")
    if ds.licence.id in CLOSED_LICENCES and ds.status != "blocked":
        raise RegisterError(f"{ctx}: licence {ds.licence.id} must be status 'blocked'")
    if ds.status == "blocked" and not ds.blocked_reason:
        raise RegisterError(f"{ctx}: blocked needs blocked_reason")
    return ds


def _profile(raw: dict, fields: list[Field], ctx: str) -> dict:
    """`sort` and `lookup`: declared fields, each named once. A boolean has two values, which a
    bloom filter cannot tell apart."""
    by = {f.name: f for f in fields}
    out = {}
    for name in ("sort", "lookup"):
        val = raw.get(name) or []
        if not isinstance(val, list):
            raise RegisterError(f"{ctx}: {name} is a list of fields")
        names = tuple(str(v) for v in val)
        if len(set(names)) < len(names):
            raise RegisterError(f"{ctx}: {name} names a field twice")
        for n in names:
            if n not in by:
                raise RegisterError(f"{ctx}: {name} field '{n}' is not a declared field")
            if name == "lookup" and by[n].type == "boolean":
                raise RegisterError(f"{ctx}: lookup field '{n}' is a boolean")
        out[name] = names
    return out


def _fields(raw: list, ctx: str) -> list[Field]:
    fields = []
    seen = set()
    for i, f in enumerate(raw):
        fctx = f"{ctx}.fields[{i}]"
        name = str(_req(f, "name", fctx))
        if not FIELD_RE.match(name):
            raise RegisterError(f"{fctx}: bad field name '{name}'")
        if name in seen:
            raise RegisterError(f"{fctx}: duplicate field '{name}'")
        seen.add(name)
        ftype = str(f.get("type", "string"))
        if ftype not in TYPES:
            raise RegisterError(f"{fctx}: type '{ftype}' not one of {TYPES}")
        fields.append(
            Field(
                name=name,
                source=str(_req(f, "source", fctx)),
                type=ftype,
                description=str(f.get("description", "")),
                true_values=tuple(f.get("true_values", Field.true_values)),
                false_values=tuple(f.get("false_values", Field.false_values)),
                date_format=str(f.get("date_format", "%Y-%m-%d")),
                note=str(f.get("note", "")),
                null_values=tuple(str(v) for v in f.get("null_values") or ()),
                label=str(f.get("label", "")).strip(),
                references=str(f.get("references", "")).strip(),
            )
        )
    labels = [f.display for f in fields]
    for f in fields:
        if len(f.label) > 60:
            raise RegisterError(f"{ctx}: label for '{f.name}' is over 60 characters")
        # The explorer names its columns by label, so a label must not be another field's name.
        if labels.count(f.display) > 1 or f.label in seen:
            raise RegisterError(f"{ctx}: label '{f.display}' is not unique")
    return fields


def _database(raw: dict, ctx: str) -> dict:
    """The tables, views and archive layout of a database entry."""
    db = raw.get("database") or {}
    match = str(_req(db, "member_match", f"{ctx}.database"))
    try:
        if "table" not in re.compile(match).groupindex:
            raise RegisterError(f"{ctx}: database.member_match needs a (?P<table>...) group")
    except re.error as e:
        raise RegisterError(f"{ctx}: database.member_match is not a valid pattern: {e}") from e
    delimiter = str(db.get("delimiter", ","))
    if delimiter == "tab":
        delimiter = "\t"
    if len(delimiter) != 1:
        raise RegisterError(f"{ctx}: database.delimiter is 'tab' or one character")
    tables = []
    names: set[str] = set()
    sources: set[str] = set()
    for i, t in enumerate(raw.get("tables") or []):
        tctx = f"{ctx}.tables[{i}]"
        name = str(_req(t, "name", tctx))
        if not FIELD_RE.match(name):
            raise RegisterError(f"{tctx}: bad table name '{name}'")
        if name in names:
            raise RegisterError(f"{tctx}: duplicate table '{name}'")
        names.add(name)
        source = str(_req(t, "source", tctx))
        if source in sources:
            raise RegisterError(f"{tctx}: two tables read the upstream table '{source}'")
        sources.add(source)
        fields = _fields(t.get("fields") or [], tctx)
        if not fields:
            raise RegisterError(f"{tctx}: a table needs a field allow-list")
        key = tuple(str(k) for k in t.get("key") or ())
        declared = {f.name for f in fields}
        for k in key:
            if k not in declared:
                raise RegisterError(f"{tctx}: key field '{k}' is not a declared field")
        tables.append(
            TableSpec(
                name=name,
                source=source,
                fields=tuple(fields),
                description=str(t.get("description", "")).strip(),
                key=key,
            )
        )
    by_name = {t.name: t for t in tables}
    for t in tables:
        for f in t.fields:
            if not f.references:
                continue
            target, _, column = f.references.partition(".")
            if target not in by_name or column not in {x.name for x in by_name[target].fields}:
                raise RegisterError(
                    f"{ctx}.tables: {t.name}.{f.name} references '{f.references}', which is not a table.field here"
                )
    views = []
    for i, v in enumerate(raw.get("views") or []):
        vctx = f"{ctx}.views[{i}]"
        name = str(_req(v, "name", vctx))
        if not FIELD_RE.match(name) or name in names:
            raise RegisterError(f"{vctx}: bad or duplicate view name '{name}'")
        names.add(name)
        views.append(
            View(
                name=name,
                sql=str(_req(v, "sql", vctx)).strip(),
                description=str(v.get("description", "")).strip(),
                example=str(v.get("example", "")).strip(),
            )
        )
    return {
        "database": Database(
            member_match=match, delimiter=delimiter, encoding=str(db.get("encoding", "utf-8"))
        ),
        "tables": tuple(tables),
        "views": tuple(views),
    }


def _wide(raw: dict, fields: list[Field], ctx: str) -> dict:
    unknown = set(raw) - WIDE_KEYS
    if unknown:
        raise RegisterError(f"{ctx}: wide has unknown keys {sorted(unknown)}")
    w = {
        "sheets": [str(x) for x in raw.get("sheets") or ()],
        "header_row": int(raw.get("header_row", 1)),
        "header_rows": int(raw.get("header_rows", 1)),
        "first_column": int(raw.get("first_column", 1)),
        "row_headers": int(raw.get("row_headers", 1)),
        "fill_down": [int(x) for x in raw.get("fill_down") or ()],
        # Only columns whose last header matches are cells; the rest are held, like a column
        # of notes or a derived percentage beside the years.
        "column_match": str(raw.get("column_match", "")),
        # A publisher that names the sheet for the period it covers renames it each release.
        "sheet_match": str(raw.get("sheet_match", "")),
    }
    for k in ("column_match", "sheet_match"):
        try:
            re.compile(w[k])
        except re.error as e:
            raise RegisterError(f"{ctx}: wide.{k} is not a valid pattern: {e}") from e
    if w["sheets"] and w["sheet_match"]:
        raise RegisterError(f"{ctx}: wide names sheets or a sheet_match, not both")
    if min(w["header_row"], w["header_rows"], w["first_column"]) < 1 or w["row_headers"] < 0:
        raise RegisterError(f"{ctx}: wide rows and columns count from 1")
    cells = [f for f in fields if f.source == CELL_SOURCE]
    measures = [f for f in fields if CELL_OF_RE.match(f.source)]
    if bool(cells) == bool(measures) or len(cells) > 1:
        raise RegisterError(
            f"{ctx}: wide needs one field with source '{CELL_SOURCE}' or fields with '(cell: <header>)'"
        )
    # With one cell per value of the last header row, that row names fields instead of a value.
    levels = w["header_rows"] - 1 if measures else w["header_rows"]
    for f in fields:
        rm, cm = ROW_HEADER_RE.match(f.source), COLUMN_HEADER_RE.match(f.source)
        ok = (
            f.source in (SHEET_SOURCE, CELL_SOURCE)
            or CELL_OF_RE.match(f.source)
            or (rm and 1 <= int(rm.group(1)) <= w["row_headers"])
            or (cm and 1 <= int(cm.group(1)) <= levels)
        )
        if not ok:
            raise RegisterError(
                f"{ctx}: field '{f.name}' source '{f.source}' is not in the wide table"
            )
    sources = [f.source for f in fields]
    if len(set(sources)) < len(sources):
        raise RegisterError(f"{ctx}: two fields of a wide table read the same source")
    if any(not 1 <= p <= w["row_headers"] for p in w["fill_down"]):
        raise RegisterError(f"{ctx}: wide.fill_down names a row header that is not there")
    return w


GEOMETRY_KINDS = ("point", "polygon", "line")


def _geometry(raw, seen: set[str], ctx: str) -> dict | None:
    """Points name their longitude and latitude fields; polygons and lines are read whole from a
    shapefile, GeoPackage or GeoJSON and carry their geometry beside the fields. `crs` is the
    publisher's datum."""
    if not raw:
        return None
    g = dict(raw)
    g["kind"] = str(g.get("kind", "point"))
    if g["kind"] not in GEOMETRY_KINDS:
        raise RegisterError(f"{ctx}: geometry.kind '{g['kind']}' not one of {GEOMETRY_KINDS}")
    if not re.match(r"^EPSG:\d+$", str(g.get("crs", ""))):
        raise RegisterError(f"{ctx}: geometry.crs is the publisher's datum, such as EPSG:7844")
    if g["kind"] == "point":
        for k in ("lon", "lat"):
            if g.get(k) not in seen:
                raise RegisterError(f"{ctx}: geometry.{k} is not a declared field")
    elif g.get("lon") or g.get("lat"):
        raise RegisterError(f"{ctx}: a {g['kind']} layer carries its geometry, not lon and lat")
    return g


def _omit(raw, fields: list[Field], ctx: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(v, str) and v.strip() for v in raw.values()):
        raise RegisterError(f"{ctx}: omit maps each left-out upstream column to its reason")
    declared = {f.source for f in fields}
    for col in raw:
        if col in declared:
            raise RegisterError(f"{ctx}: omit names '{col}', which a field reads")
    return {str(k): str(v).strip() for k, v in raw.items()}


# The query API's operators, and the SQL each one is for the chart.
WHERE_OPS = {"eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
AGGREGATES = ("sum", "avg", "min", "max")
# A where value that stands for the field's newest value in the version being built.
NEWEST = "newest"


def _where(raw, by: dict[str, Field], ctx: str) -> tuple[dict, ...]:
    """field: value for an exact match, or field: {op: value, ...} for the query API's other
    operators, where neq may take a list."""
    if not isinstance(raw, dict):
        raise RegisterError(f"{ctx}: where maps each field to a value")
    out = []
    for name, cond in raw.items():
        if name not in by:
            raise RegisterError(f"{ctx}: where names '{name}', not a declared field")
        for op, value in cond.items() if isinstance(cond, dict) else [("eq", cond)]:
            if op not in WHERE_OPS:
                raise RegisterError(
                    f"{ctx}: where.{name} operator '{op}' not one of {list(WHERE_OPS)}"
                )
            # A list under neq leaves out each value, such as a total and its percentage row.
            values = value if op == "neq" and isinstance(value, list) and value else [value]
            for value in values:
                if value is None or isinstance(value, (dict, list)):
                    raise RegisterError(f"{ctx}: where.{name}.{op} needs a single value")
                if isinstance(value, bool):
                    value = "true" if value else "false"
                out.append({"field": str(name), "op": op, "value": str(value)})
    return tuple(out)


def _metric(raw, by: dict[str, Field], ctx: str) -> str:
    metric = str(raw)
    if metric == "count":
        return metric
    fn, _, name = metric.partition(".")
    if fn not in AGGREGATES or name not in by:
        raise RegisterError(f"{ctx}: metric is count or one of {AGGREGATES}.<field>")
    if by[name].type not in ("integer", "number"):
        raise RegisterError(f"{ctx}: metric {metric} needs a numeric field")
    return metric


def _example(raw, fields: list[Field], ctx: str) -> dict | None:
    """The dataset page's first query, chosen for what a reader comes to the table to ask, which
    the query tile answers and the console starts from. Without it the build picks one from the
    field statistics."""
    if raw is None:
        return None
    ctx = f"{ctx}: example"
    if not isinstance(raw, dict) or not set(raw) <= {"where", "group", "metric", "label"}:
        raise RegisterError(f"{ctx} takes where, group, metric and label")
    by = {f.name: f for f in fields}
    # The tile shows the groups, so an example without one would answer with nothing to read.
    if raw.get("group") not in by:
        raise RegisterError(f"{ctx}.group must name a declared field")
    return {
        "filters": _where(raw.get("where") or {}, by, ctx),
        "group": (str(raw["group"]),),
        "metric": _metric(raw.get("metric", "count"), by, ctx),
        "label": str(raw.get("label", "")).strip(),
    }


def _chart(raw, fields: list[Field], ctx: str) -> dict | None:
    """What the yearly chart and its sparkline draw, where the example's measure or the build's
    choice of colour reads wrong: the rows kept, such as leaving out the crashes a publisher
    stopped recording, the field colour splits by (none for one series), the measure and its
    words. The rows themselves are never filtered."""
    if raw is None:
        return None
    ctx = f"{ctx}: chart"
    # A table of one period, or one whose only date is when a row was edited, has no year to draw.
    if raw == "none":
        return {"off": True, "where": (), "split": None, "metric": "", "label": "", "year": ""}
    if not isinstance(raw, dict) or not set(raw) <= {"where", "split", "metric", "label", "year"}:
        raise RegisterError(f"{ctx} takes where, split, metric, label and year, or is none")
    by = {f.name: f for f in fields}
    year = str(raw.get("year", ""))
    if year and (year not in by or by[year].type not in ("integer", "date", "datetime")):
        raise RegisterError(f"{ctx}.year must name an integer or date field")
    where = _where(raw.get("where") or {}, by, ctx)
    if any(w["value"] == NEWEST for w in where):
        raise RegisterError(f"{ctx}.where cannot use {NEWEST}; the chart draws every year")
    split = raw.get("split")
    if split is not None and split != "none" and split not in by:
        raise RegisterError(f"{ctx}.split must name a declared field, or none")
    return {
        "where": tuple({**w, "op": WHERE_OPS[w["op"]]} for w in where),
        "split": None if split is None else ("" if split == "none" else str(split)),
        "metric": _metric(raw["metric"], by, ctx) if raw.get("metric") else "",
        "label": str(raw.get("label", "")).strip(),
        "year": year,
    }


def _sample(raw, fields: list[Field], ctx: str) -> dict | None:
    """The rows the dataset page shows, where the build's pick of the newest rows reads dull:
    the rows kept, the order as the query API writes it, the field whose values take turns
    (none for no turns) and the words that say how the rows were picked."""
    if raw is None:
        return None
    ctx = f"{ctx}: sample"
    if not isinstance(raw, dict) or not set(raw) <= {"where", "order", "spread", "label"}:
        raise RegisterError(f"{ctx} takes where, order, spread and label")
    by = {f.name: f for f in fields}
    order = []
    for term in raw.get("order") or ():
        name, _, way = str(term).partition(".")
        if name not in by or way not in ("", "asc", "desc"):
            raise RegisterError(f"{ctx}.order term '{term}' is not field, field.asc or field.desc")
        order.append((name, way == "desc"))
    spread = raw.get("spread")
    if spread is not None and spread != "none" and spread not in by:
        raise RegisterError(f"{ctx}.spread must name a declared field, or none")
    where = _where(raw.get("where") or {}, by, ctx)
    if (raw.get("where") or raw.get("order")) and not str(raw.get("label", "")).strip():
        raise RegisterError(f"{ctx} needs a label that says which rows its where and order pick")
    return {
        "where": tuple({**w, "op": WHERE_OPS[w["op"]]} for w in where),
        "order": tuple(order),
        "spread": None if spread is None else ("" if spread == "none" else str(spread)),
        "label": str(raw.get("label", "")).strip(),
    }


@dataclass(frozen=True)
class Grant:
    id: str
    title: str
    url: str
    kind: str
    read: str
    hub: str
    grants: dict[str, str]
    condition: str = ""
    letter: str = ""


def parse_grant(raw: dict, ctx: str, root: Path) -> Grant:
    gid = str(_req(raw, "id", ctx))
    if gid in CC_LICENCES or gid in CLOSED_LICENCES:
        raise RegisterError(f"{ctx}: {gid} is a Creative Commons id, not a grant")
    kind = str(_req(raw, "kind", ctx))
    if kind not in GRANT_KINDS:
        raise RegisterError(f"{ctx}: kind '{kind}' not one of {GRANT_KINDS}")
    read = str(_req(raw, "read", ctx))
    if not DATE_RE.match(read):
        raise RegisterError(f"{ctx}: read is the date the words were read, YYYY-MM-DD")
    words = raw.get("grants") or {}
    for test in GRANT_TESTS:
        if not str(words.get(test) or "").strip():
            raise RegisterError(
                f"{ctx}: a grant must quote the publisher on '{test}'; one that cannot is not admitted"
            )
    letter = str(raw.get("letter", ""))
    if kind == "permission":
        if not PERMISSION_RE.match(gid):
            raise RegisterError(f"{ctx}: a permission's id is <AGENCY>-PERMISSION-<year>")
        if not letter or not (root / letter).is_file():
            raise RegisterError(f"{ctx}: a permission names the stored reply in letter")
    return Grant(
        id=gid,
        title=str(_req(raw, "title", ctx)),
        url=str(_req(raw, "url", ctx)),
        kind=kind,
        read=read,
        hub=str(raw.get("hub") or "other"),
        grants={t: " ".join(str(words[t]).split()) for t in GRANT_TESTS},
        condition=" ".join(str(raw.get("condition") or "").split()),
        letter=letter,
    )


def load_grants(root: Path = GRANTS_DIR) -> dict[str, Grant]:
    out = {}
    for p in sorted(root.glob("*.yaml")) if root.is_dir() else ():
        g = parse_grant(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, p.name, root)
        if g.id != p.stem:
            raise RegisterError(f"{p.name}: id '{g.id}' does not match the filename")
        out[g.id] = g
    return out


def load(register_dir: Path) -> list[Dataset]:
    out = []
    slugs: dict[str, str] = {}
    # Entries may sit in folders, such as one per jurisdiction; publishers/ and licences/ are not
    # entries.
    paths = sorted(
        p
        for p in register_dir.rglob("*.yaml")
        if p.relative_to(register_dir).parts[0] not in ("publishers", "licences")
    )
    for p in paths:
        name = p.relative_to(register_dir).as_posix()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        ds = parse(raw, name)
        if ds.slug != p.stem:
            raise RegisterError(f"{name}: slug '{ds.slug}' does not match filename")
        if ds.slug in slugs:
            raise RegisterError(f"{name}: duplicate slug, also in {slugs[ds.slug]}")
        slugs[ds.slug] = name
        out.append(replace(ds, path=str(p)))
    for coll in sorted({d.collection for d in out if d.collection and d.status == "live"}):
        if not any(d.collection_description for d in out if d.collection == coll):
            raise RegisterError(
                f"collection '{coll}' needs collection_description on one of its entries"
            )
    # One search phrase, one page: a title tag shared by two pages splits the ranking.
    phrases: dict[str, str] = {}
    for d in out:
        for phrase in (d.search_title, d.collection_search_title):
            key = phrase.strip().lower()
            if not key:
                continue
            if key in phrases and phrases[key] != d.slug:
                raise RegisterError(
                    f"{slugs[d.slug]}: search title '{phrase}' is also used by {phrases[key]}"
                )
            phrases[key] = d.slug
    out.sort(key=lambda d: (d.order, d.collection or d.slug, d.slug))
    return out


CC_LICENCES = dict(OPEN_LICENCES)
# Read at import on purpose: every command that touches the register sees the same licences, and a
# grant that fails its tests stops them all rather than only `register validate`.
GRANTS = load_grants()
OPEN_LICENCES.update({g.id: (g.title, g.url) for g in GRANTS.values()})
LICENCE_CONDITIONS = {g.id: g.condition for g in GRANTS.values() if g.condition}
