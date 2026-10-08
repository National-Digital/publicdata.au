"""Query and download Australian government open data from publicdata.au.

Every function works for every dataset the site serves, named by its slug, so a dataset added to
the site needs no new release of this package.

    >>> import publicdata_au as pd_au
    >>> pd_au.datasets("road crashes")
    >>> pd_au.rows("au-road-deaths", {"state": "QLD", "year": pd_au.gte(2020)}, limit=5)
    >>> pd_au.aggregate("au-road-deaths", group="state")
    >>> pd_au.read("au-road-deaths")  # a pandas DataFrame, with the [pandas] extra
    >>> con = pd_au.connect("gnaf")  # a DuckDB connection with the version attached over HTTPS
    >>> con.sql("SELECT postcode, count(*) FROM address_view GROUP BY 1").df()
    >>> pd_au.relation("au-road-deaths").filter("year >= 2020").aggregate("state, count(*)").df()
    >>> pd_au.read_geo("abs-lga-2025")  # a geopandas GeoDataFrame, with the [geo] extra
    >>> pd_au.changes("rba-money-market-daily")  # what each release changed
    >>> print(pd_au.cite("rba-cash-rate", format="bibtex"))
    >>> pd_au.fields("act-road-crashes")  # every field with its type, range and values
    >>> by_sa2 = pd_au.aggregate("act-road-crashes", group="sa2_2021_code")
    >>> pd_au.join_boundaries(by_sa2.to_pandas())  # a GeoDataFrame of SA2s, with the [geo] extra

Nothing is kept on disk unless asked: pass `cache=True`, or set PUBLICDATA_CACHE=1, and files
are kept in `cache_dir()` and reused, since a version never changes.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__version__ = "0.5.0"
__all__ = [
    "Client",
    "Connection",
    "Filter",
    "LicenceCondition",
    "PublicDataError",
    "Rows",
    "SiteUnreachable",
    "aggregate",
    "boundaries",
    "boundary_layers",
    "browse",
    "cache_clear",
    "cache_dir",
    "cache_list",
    "catalogue",
    "changes",
    "cite",
    "connect",
    "dataset",
    "datasets",
    "diff",
    "download",
    "eq",
    "fields",
    "file_url",
    "gt",
    "gte",
    "ilike",
    "in_",
    "is_null",
    "join_boundaries",
    "like",
    "lt",
    "lte",
    "neq",
    "not_",
    "latest",
    "provenance",
    "read",
    "read_geo",
    "relation",
    "rows",
    "schema",
    "tables",
    "versions",
]

SITE = "https://publicdata.au"
FORMATS = (
    "parquet",
    "csv",
    "csv.gz",
    "json",
    "ndjson",
    "sqlite",
    "duckdb",
    "xlsx",
    "arrow",
    "geojson",
    "gpkg",
    "geo.parquet",
)
# On every table version. The others are left out of a version when the table is over their size
# limits, and Arrow is only on versions whose manifest has no `caps` field.
ALWAYS = ("parquet", "csv", "csv.gz", "ndjson", "duckdb")
PAGE_MAX = 10_000


class PublicDataError(Exception):
    """An answer from publicdata.au that was not a success."""

    def __init__(self, status: int, message: str, body: Any = None, url: str = ""):
        super().__init__(f"{status}: {message}" + (f" ({url})" if url else ""))
        self.status = status
        self.body = body
        self.url = url


class SiteUnreachable(PublicDataError):
    """The site could not be reached: no connection, a DNS failure or a timeout."""


class LicenceCondition(UserWarning):
    """A dataset's licence sets a condition on its use beyond attribution, such as G-NAF's rule
    on mail compilation. Shown once per dataset per process; silence it with
    `warnings.simplefilter("ignore", publicdata_au.LicenceCondition)`."""


class Filter:
    """One condition on a field, in the query API's `operator.value` form."""

    def __init__(self, expr: str):
        self.expr = expr

    def __str__(self) -> str:
        return self.expr

    def __repr__(self) -> str:
        return f"Filter({self.expr!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Filter) and other.expr == self.expr

    def __hash__(self) -> int:
        return hash(self.expr)


def _v(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def eq(value) -> Filter:
    return Filter(f"eq.{_v(value)}")


def neq(value) -> Filter:
    return Filter(f"neq.{_v(value)}")


def gt(value) -> Filter:
    return Filter(f"gt.{_v(value)}")


def gte(value) -> Filter:
    return Filter(f"gte.{_v(value)}")


def lt(value) -> Filter:
    return Filter(f"lt.{_v(value)}")


def lte(value) -> Filter:
    return Filter(f"lte.{_v(value)}")


def like(pattern: str) -> Filter:
    """`*` stands for any run of characters. Case is ignored in ASCII letters only."""
    return Filter(f"like.{pattern}")


def ilike(pattern: str) -> Filter:
    """The same as `like`."""
    return Filter(f"ilike.{pattern}")


def in_(*values) -> Filter:
    if len(values) == 1 and isinstance(values[0], (list, tuple, set, frozenset)):
        values = tuple(values[0])
    if any("," in _v(v) for v in values):
        raise ValueError("a value in in_() cannot contain a comma")
    return Filter(f"in.({','.join(_v(v) for v in values)})")


def is_null() -> Filter:
    """Blank in the source, or suppressed by the publisher."""
    return Filter("is.null")


def not_(f: Filter) -> Filter:
    return Filter(f"not.{f.expr}")


def _filter(value) -> str:
    if isinstance(value, Filter):
        return value.expr
    if value is None:
        return "is.null"
    if isinstance(value, (list, tuple, set, frozenset)):
        return in_(tuple(value)).expr
    return eq(value).expr


class Rows(list):
    """A list of row dicts that also carries where they came from.

    `version`, `attribution`, `cite` and `licence` come from the answer itself, so they always
    name the version the rows were read from."""

    def __init__(self, rows=(), meta: Mapping | None = None, page: Mapping | None = None):
        super().__init__(rows)
        self.meta = dict(meta or {})
        self.page = dict(page or {})

    @property
    def version(self) -> str | None:
        return self.meta.get("version")

    @property
    def attribution(self) -> str | None:
        return self.meta.get("attribution")

    @property
    def cite(self) -> str | None:
        return self.meta.get("cite")

    @property
    def licence(self) -> dict | None:
        return self.meta.get("licence")

    @property
    def version_page(self) -> str | None:
        return self.page.get("version_page")

    def to_pandas(self):
        """The rows as a DataFrame. Dates are already `datetime.date`; `df.attrs["fields"]` maps
        each column to its field's description."""
        import pandas as pd

        df = pd.DataFrame(list(self))
        df.attrs["publicdata"] = self.meta
        df.attrs["fields"] = dict(self.page.get("fields") or {})
        return df


class Results(list):
    """A page of catalogue records: `total` matches in all, `next_offset` for the next page."""

    def __init__(self, rows=(), total: int | None = None, next_offset: int | None = None):
        super().__init__(rows)
        self.total = total
        self.next_offset = next_offset


class Connection:
    """A DuckDB connection from `connect()`: every method of the connection, plus `publicdata`,
    which names the dataset, the version, the file's URL and its licence."""

    def __init__(self, con, publicdata: dict):
        self._con = con
        self.publicdata = publicdata

    def __getattr__(self, name: str):
        return getattr(self._con, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._con.close()

    def __repr__(self) -> str:
        return f"Connection({self.publicdata['dataset']!r}, {self.publicdata['version']!r})"


class Client:
    """Talks to one publicdata.au site. The module-level functions use a shared default."""

    def __init__(
        self,
        site: str | None = None,
        *,
        timeout: float = 60,
        retries: int = 3,
        user_agent: str | None = None,
        cache: bool | None = None,
        cache_dir: str | os.PathLike | None = None,
    ):
        self.site = (site or os.environ.get("PUBLICDATA_SITE") or SITE).rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent or f"publicdata-au-python/{__version__}"
        if cache is None:
            cache = os.environ.get("PUBLICDATA_CACHE", "").lower() in ("1", "true", "yes")
        self.cache = cache
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._conditions: dict[str, str] = {}
        self._shown: set[str] = set()
        self._relations: dict[tuple, Connection] = {}
        self._memo: dict[str, Any] = {}

    def close(self) -> None:
        """Closes the DuckDB connections `relation()` opened."""
        for con in self._relations.values():
            con.close()
        self._relations.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _open(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        attempt = 0
        while True:
            try:
                return urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as err:
                # The API allows a burst per address, then answers 429 with how long to wait.
                if err.code == 429 and attempt < self.retries:
                    attempt += 1
                    wait = err.headers.get("Retry-After") or "10"
                    time.sleep(min(float(wait) if wait.isdigit() else 10.0, 60.0))
                    continue
                # A passing server error is tried again after a pause.
                if err.code in (500, 502, 503, 504) and attempt < self.retries:
                    attempt += 1
                    err.close()
                    time.sleep(2.0**attempt)
                    continue
                raw = err.read()
                err.close()
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = raw.decode("utf-8", "replace")
                msg = body.get("error") if isinstance(body, dict) else str(body)[:200]
                raise PublicDataError(err.code, msg or err.reason, body, url) from None
            except (urllib.error.URLError, TimeoutError) as err:
                reason = getattr(err, "reason", err)
                raise SiteUnreachable(0, f"could not reach the site: {reason}", None, url) from err

    def _json(self, path_or_url: str, params: Mapping | None = None):
        url = path_or_url if "://" in path_or_url else self.site + path_or_url
        if params:
            q = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None},
                safe="*(),.:",
                quote_via=urllib.parse.quote,
            )
            if q:
                url += ("&" if "?" in url else "?") + q
        with self._open(url) as r:
            return json.loads(r.read())

    def datasets(
        self,
        q: str | None = None,
        *,
        publisher: str | None = None,
        topic: str | None = None,
        jurisdiction: str | None = None,
    ) -> list[dict]:
        """Datasets the site serves, each with slug, title, publisher, licence and page URL.
        `q` searches titles, summaries, publishers, keywords and field names. `publisher` is
        part of a publisher's name, `topic` a topic such as "roads" or "crime", and
        `jurisdiction` a code such as "Qld" or a name such as "Queensland", all ignoring case.
        Every condition given must match."""
        out = self._json("/api/v1/datasets", {"q": q} if q else None)["results"]
        if publisher is None and topic is None and jurisdiction is None:
            return out
        keep = self._catalogue_match(publisher, topic, jurisdiction)
        return [d for d in out if d["slug"] in keep]

    def _catalogue_match(self, publisher, topic, jurisdiction) -> set[str]:
        for what, v in (("publisher", publisher), ("topic", topic), ("jurisdiction", jurisdiction)):
            if v is not None and (not isinstance(v, str) or not v):
                raise ValueError(f"{what} must be one piece of text")
        entries = self._json("/catalog.json")["dataset"]
        keep = entries
        if publisher is not None:
            p = publisher.lower()
            keep = [e for e in keep if p in (e.get("publisher") or {}).get("name", "").lower()]
        if topic is not None:
            known = sorted({t.lower() for e in entries for t in e.get("publicdata:topics") or ()})
            if not known:
                raise ValueError("the site's catalogue does not list topics yet")
            if topic.lower() not in known:
                raise ValueError(
                    f"no datasets are filed under topic {topic!r}; the topics are {', '.join(known)}"
                )
            keep = [
                e
                for e in keep
                if topic.lower() in [t.lower() for t in e.get("publicdata:topics") or ()]
            ]
        if jurisdiction is not None:
            j = jurisdiction.lower()
            if j in ("commonwealth", "australian government", "federal"):
                j = "cth"
            keep = [
                e
                for e in keep
                if j
                in (
                    (e.get("publicdata:jurisdiction") or "").lower(),
                    (e.get("spatial") or "").lower(),
                )
            ]
        return {e["identifier"] for e in keep}

    def dataset(self, slug: str) -> dict:
        """The dataset's Frictionless data package: title, licence, attribution, fields and
        every file of the newest version."""
        return self._json(f"/d/{_slug(slug)}/datapackage.json")

    def versions(self, slug: str) -> list[dict]:
        """Every version kept, newest first, each with its date, rows, fields and source hash."""
        return self._json(f"/d/{_slug(slug)}/versions.json")["versions"]

    def latest(self, slug: str) -> str:
        """The date of the newest version."""
        return self._json(f"/d/{_slug(slug)}/versions.json")["latest"]

    def schema(self, slug: str, version: str | None = None) -> dict:
        """A version's schema.json: the fields of a table, or every table of a database with
        its fields, keys and references, and the views."""
        at = f"v/{_date(version)}" if version else "latest"
        return self._json(f"/d/{_slug(slug)}/{at}/schema.json")

    def tables(self, slug: str, version: str | None = None) -> list[dict]:
        """The tables of a database, each with its name, description, rows, fields and keys.
        A dataset that is one table has one entry, `records`."""
        s = self.schema(slug, version)
        if s.get("kind") == "database":
            return s["tables"]
        return [
            {
                "name": "records",
                "fields": s.get("fields", []),
                "primaryKey": s.get("primaryKey", []),
            }
        ]

    def connect(
        self,
        slug: str,
        version: str | None = None,
        *,
        name: str | None = None,
        cache: bool | None = None,
    ):
        """A DuckDB connection with the version's DuckDB file attached read-only over HTTPS and
        made the current database, so every table and view is queried by name. Only the blocks
        a query touches are read. Needs the duckdb package.

        For a database such as G-NAF the file holds every table, the keys between them and the
        publisher's views; for a single table it holds `records`. `con.publicdata` names the
        version, the URL and the file's provenance. Pass a date from `versions()` to pin a
        version. With `cache=True` the whole file is downloaded into `cache_dir()` once and
        attached from there, which makes repeated scans of a large database much faster."""
        import duckdb

        version = _date(version) if version else self.latest(slug)
        url = self.file_url(slug, "duckdb", version)
        path = self._fetch(slug, "duckdb", version, cache=True)[0] if self._caching(cache) else None
        name = _ident(name or _slug(slug).replace("-", "_"))
        con = duckdb.connect()
        src = str(path) if path else url
        if src.startswith("http"):
            con.execute("INSTALL httpfs; LOAD httpfs")
        con.execute(f"ATTACH '{src.replace(chr(39), chr(39) * 2)}' AS \"{name}\" (READ_ONLY)")
        con.execute(f'USE "{name}"')
        try:
            header = _header(con.execute("SELECT key, value FROM publicdata").fetchall())
        except duckdb.Error:
            header = {}
        self._notice(slug, header.get("licence"))
        return Connection(
            con,
            {k: v for k, v in header.items() if k not in ("dataset", "version", "url", "name")}
            | {
                "dataset": slug,
                "version": version,
                "url": url,
                "path": str(path) if path else None,
                "name": name,
                "licence": header.get("licence"),
            },
        )

    def relation(
        self,
        slug: str,
        table: str | None = None,
        version: str | None = None,
        *,
        cache: bool | None = None,
    ):
        """One table or view as a lazy DuckDB relation over the attached file: `.filter()`,
        `.aggregate()`, `.project()` and `.order()` build SQL that runs only when `.df()`,
        `.arrow()` or `.fetchall()` asks, reading only the blocks it needs. `table` defaults to
        `records`, the one table of most datasets; a database such as G-NAF needs a table or
        view name from `tables()`. Calls for the same dataset and version share a connection."""
        version = _date(version) if version else self.latest(slug)
        key = (_slug(slug), version, self._caching(cache))
        con = self._relations.get(key)
        if con is None:
            con = self.connect(slug, version, cache=cache)
            self._relations[key] = con
        names = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_catalog = current_database()"
            ).fetchall()
        }
        if table is not None and _is_date(table):
            raise ValueError(f"{table!r} is a version; pass it as version={table!r}")
        if table is None:
            if "records" not in names:
                raise ValueError(
                    f"{slug!r} has several tables; name one of {', '.join(sorted(names))}"
                )
            table = "records"
        if _table(table) not in names:
            raise ValueError(
                f"{slug!r} has no table or view {table!r}; its tables are {', '.join(sorted(names))}"
            )
        return con.table(table)

    def _query(self, slug, kind, where, version, params) -> dict:
        base = f"/api/v1/datasets/{_slug(slug)}/"
        if version:
            base += f"versions/{_date(version)}/"
        params = dict(params)
        for field, value in (where or {}).items():
            params[field] = _filter(value)
        return self._json(base + kind, params)

    def rows(
        self,
        slug: str,
        where: Mapping[str, Any] | None = None,
        *,
        select: str | list[str] | None = None,
        order: str | list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        version: str | None = None,
        all: bool = False,
    ) -> Rows:
        """Rows of a dataset from the query API.

        `where` maps a field to a value, which must match exactly, or to a filter such as
        `gte(2020)`, `in_("QLD", "NSW")` or `is_null()`. A list means any of its values and None
        means blank. Every condition must match.

        Without `version` the answer comes from the newest version and changes when the
        publisher releases again; with a date from `versions()` it never changes. `all=True`
        follows every page. For a whole table, `read()` or `download()` is faster and has no
        rate limit."""
        if all and limit is None:
            limit = PAGE_MAX
        params = {
            "select": _list(select),
            "order": _list(order),
            "limit": limit,
            "offset": offset,
        }
        body = self._query(slug, "rows", where, version, params)
        out = Rows(body.get("rows", ()), body.get("publicdata"), _page(body))
        self._notice(slug, out.licence)
        nxt = body.get("next")
        while all and nxt:
            body = self._json(nxt)
            out.extend(body.get("rows", ()))
            nxt = body.get("next")
        out.page["next"] = nxt
        return self._typed(slug, out, version)

    def aggregate(
        self,
        slug: str,
        group: str | list[str] | None = None,
        metric: str | list[str] = "count",
        where: Mapping[str, Any] | None = None,
        *,
        version: str | None = None,
    ) -> Rows:
        """Counts, sums, averages, minimums and maximums by group, from the query API.

        `metric` is `count`, `sum.<field>`, `avg.<field>`, `min.<field>` or `max.<field>`, or a
        list of them. `where` works as it does for `rows()`."""
        params = {"group": _list(group), "metric": _list(metric)}
        body = self._query(slug, "aggregate", where, version, params)
        out = Rows(body.get("rows", ()), body.get("publicdata"), _page(body))
        self._notice(slug, out.licence)
        return self._typed(slug, out, version)

    def fields(self, slug: str, version: str | None = None) -> list[dict]:
        """Each field's name, type ("string", "integer", "number", "boolean", "date" or
        "datetime") and description, with `min` and `max` for a number or date and `values`
        when it holds few. For a database such as G-NAF each entry also names its `table`.
        Without `version` the fields are the newest version's; a pinned version is described by
        its schema, without ranges or values."""
        if not version:
            try:
                return self._json(f"/d/{_slug(slug)}/fields.json")["fields"]
            except PublicDataError as err:
                if err.status != 404:
                    raise
        s = self.schema(slug, version)
        if s.get("kind") == "database":
            return [{"table": t["name"], **f} for t in s["tables"] for f in t["fields"]]
        return s.get("fields", [])

    def _field_types(self, slug: str, version: str | None = None) -> dict[str, dict]:
        # Typing is a convenience: if the fields cannot be read, the answer goes back untyped.
        key = f"fields:{slug}:{version or 'latest'}"
        if key not in self._memo:
            try:
                fields = self.fields(slug, version)
            except PublicDataError:
                return {}
            self._memo[key] = {f["name"]: f for f in fields if "table" not in f}
        return self._memo[key]

    def _typed(self, slug: str, out: Rows, version: str | None = None) -> Rows:
        """Values as the fields of the version they came from say: the API sends dates as text
        and booleans as 0 or 1. A value that does not parse is left as it came."""
        fields = self._field_types(slug, version)
        out.page["fields"] = {
            n: f.get("description") for n, f in fields.items() if f.get("description")
        }
        conv = {n: _CONVERT[f["type"]] for n, f in fields.items() if f.get("type") in _CONVERT}
        for row in out:
            for name in conv.keys() & row.keys():
                v = row[name]
                if v is not None:
                    try:
                        row[name] = conv[name](v)
                    except (TypeError, ValueError):
                        pass
        return out

    def file_url(
        self,
        slug: str,
        format: str = "parquet",
        version: str | None = None,
        table: str | None = None,
    ) -> str:
        """The URL of a version's file: data.<format>, or with `table` one table of a database
        as tables/<table>.parquet."""
        if format not in FORMATS:
            raise ValueError(f"format must be one of {', '.join(FORMATS)}")
        at = f"v/{_date(version)}" if version else "latest"
        if table:
            if format != "parquet":
                raise ValueError("a table of a database is served as parquet")
            return f"{self.site}/d/{_slug(slug)}/{at}/tables/{_table(table)}.parquet"
        return f"{self.site}/d/{_slug(slug)}/{at}/data.{format}"

    def _save(self, slug, format, version, path, table=None) -> tuple[Path, str]:
        try:
            resp = self._open(self.file_url(slug, format, version, table))
        except PublicDataError as err:
            if err.status == 404 and table is None and format not in ALWAYS:
                raise PublicDataError(404, _absent_why(slug, format), err.body, err.url) from None
            raise
        with resp as r:
            final = r.geturl()
            got = final.split("/v/", 1)[1].split("/", 1)[0] if "/v/" in final else "latest"
            dest = (
                Path(path) if path else Path(f"{slug}-{got}{'-' + table if table else ''}.{format}")
            )
            with open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        return dest, got

    def download(
        self,
        slug: str,
        format: str = "parquet",
        version: str | None = None,
        path: str | os.PathLike | None = None,
        *,
        table: str | None = None,
        cache: bool | None = None,
    ) -> Path:
        """Saves one version's file, the newest by default, and returns where. Without `path`
        the file is named `<slug>-<version>.<format>` in the working directory, or with the
        cache on it is the kept file in `cache_dir()`. `table` saves one table of a database,
        as Parquet. Files have no rate limit."""
        if self._caching(cache):
            kept = self._fetch(slug, format, version, table, cache=True)[0]
            if path is None:
                out = kept
            else:
                out = Path(path)
                shutil.copyfile(kept, out)
        else:
            out = self._save(slug, format, version, path, table)[0]
        self._notice(slug)
        return out

    def read(
        self,
        slug: str,
        version: str | None = None,
        *,
        table: str | None = None,
        cache: bool | None = None,
        columns: list[str] | None = None,
    ):
        """The whole table as a pandas DataFrame, read from the version's Parquet file, or with
        `table` one table of a database. `columns` reads only those fields. `df.attrs["publicdata"]`
        is the provenance header the file itself carries: version, licence, attribution,
        citation and source. Without pyarrow the table is read from the gzipped CSV instead,
        typed by the version's fields."""
        if columns is not None and (isinstance(columns, str) or not columns):
            raise ValueError("columns must be a list of field names, as fields() lists them")
        pq = _parquet_module()
        if pq is None:
            if table:
                raise ImportError(
                    "a table of a database is served only as Parquet, which needs pyarrow: "
                    "pip install 'publicdata-au[pandas]'"
                ) from None
            return self._read_csv(slug, version, cache, columns)
        with tempfile.TemporaryDirectory() as d:
            p, _ = self._fetch(slug, "parquet", version, table, cache, Path(d))
            tbl = pq.read_table(p, columns=list(columns) if columns else None)
        header = (tbl.schema.metadata or {}).get(b"publicdata")
        df = tbl.to_pandas()
        df.attrs["publicdata"] = json.loads(header) if header else {}
        self._notice(slug, df.attrs["publicdata"].get("licence"))
        return df

    def _read_csv(self, slug, version, cache, columns):
        import pandas as pd

        fields = self._field_types(slug, version)
        if columns:
            unknown = [c for c in columns if fields and c not in fields]
            if unknown:
                raise ValueError(f"unknown fields: {', '.join(unknown)}")
        with tempfile.TemporaryDirectory() as d:
            p, got = self._fetch(slug, "csv.gz", version, None, cache, Path(d))
            # Every column as text, then typed as the Parquet path types it.
            df = pd.read_csv(
                p,
                compression="gzip",
                usecols=list(columns) if columns else None,
                dtype=str,
                keep_default_na=False,
                na_values=[""],
            )
        if columns:
            df = df[list(columns)]
        for name in df.columns:
            kind = "suppressed" if name == "suppressed" else fields.get(name, {}).get("type")
            df[name] = _csv_column(df[name], kind)
        df.attrs["publicdata"] = self._file_header(slug, version or got)
        self._notice(slug, df.attrs["publicdata"].get("licence"))
        return df

    def _file_header(self, slug: str, version: str | None) -> dict:
        """The provenance header every file of a version carries, from the first line of its
        NDJSON, read with a range request so the rest is not downloaded."""
        url = self.file_url(slug, "ndjson", version if version != "latest" else None)
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Range": "bytes=0-65535"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                line = r.read(65536).split(b"\n", 1)[0]
            return json.loads(line).get("publicdata", {})
        except (urllib.error.URLError, TimeoutError, ValueError, AttributeError):
            return {}

    def read_geo(self, slug: str, version: str | None = None, *, cache: bool | None = None):
        """A dataset's map layer as a geopandas GeoDataFrame, read from the version's
        GeoPackage. Datasets with a location or a shape have one; the coordinates are in the
        reference system the publisher used, usually GDA2020 (EPSG:7844). Needs the [geo]
        extra. `gdf.attrs["publicdata"]` is the provenance the file carries."""
        import geopandas as gpd

        with tempfile.TemporaryDirectory() as d:
            try:
                p, _ = self._fetch(slug, "gpkg", version, None, cache, Path(d))
            except PublicDataError as err:
                if err.status == 404:
                    raise PublicDataError(
                        404,
                        f"{slug!r} has no map layer: only datasets with a location or a shape have one",
                        err.body,
                        err.url,
                    ) from None
                raise
            gdf = gpd.read_file(p, layer="records")
            try:
                import sqlite3
                from contextlib import closing

                with closing(sqlite3.connect(p)) as db:
                    header = _header(db.execute("SELECT key, value FROM publicdata").fetchall())
            except sqlite3.Error:
                header = {}
        gdf.attrs["publicdata"] = header
        self._notice(slug, header.get("licence"))
        return gdf

    def changes(self, slug: str, since: str | None = None, until: str | None = None) -> list[dict]:
        """Every comparison of a version with the one before it, oldest first: the two
        versions, their rows, rows added, removed, changed and unchanged, the schema changes
        and the URL of the full comparison. `since` and `until` bound the versions compared.
        A database is compared by each table's row count."""
        out = self._json(f"/d/{_slug(slug)}/changes.json")["changes"]
        if since:
            out = [c for c in out if c["from"] >= _date(since)]
        if until:
            out = [c for c in out if c["to"] <= _date(until)]
        return out

    def diff(self, slug: str, version: str | None = None) -> dict:
        """The full comparison of `version`, the newest by default, with the version before
        it: the counts, the keys of the rows added, removed and changed (up to 50,000 of each,
        `truncated` says when there were more) and up to ten changed rows field by field."""
        version = _date(version) if version else self.latest(slug)
        for c in self.changes(slug):
            if c["to"] == version:
                return self._json(c["url"])
        raise ValueError(
            f"no comparison ends at {version} for {slug!r}: it is the first version kept, "
            "or not a version; changes() lists them"
        )

    def cite(self, slug: str, version: str | None = None, *, format: str = "text") -> str:
        """How to cite one version, the newest by default: `format="text"` for the citation
        the site gives, with the attribution the licence requires, or `"bibtex"` for a
        reference manager."""
        if format not in ("text", "bibtex"):
            raise ValueError('format must be "text" or "bibtex"')
        dp = self._json(f"/d/{_slug(slug)}/datapackage.json")
        version = _date(version) if version else dp["version"]
        url = f"{self.site}/d/{slug}/v/{version}/"
        pub = next(
            (c["title"] for c in dp.get("contributors", []) if c.get("role") == "publisher"),
            "publicdata.au",
        )
        licence = ", ".join(
            lic.get("title") or lic.get("name", "") for lic in dp.get("licenses", [])
        )
        if version == dp["version"] and dp.get("publicdata:attribution"):
            note = dp["publicdata:attribution"].rstrip(".")
        else:
            m = self._json(f"/d/{slug}/v/{version}/manifest.json")
            note = f"Licensed under {licence}, read from the publisher on {m['fetched_at'][:10]}"
        how = f"Version {version}, serialised and versioned by National Digital at publicdata.au"
        if format == "text":
            return f"{pub} ({version[:4]}). {dp['title']}. {how}. {note}. {url}"
        fields = {
            "title": dp["title"],
            "author": "{" + pub + "}",
            "year": version[:4],
            "howpublished": how,
            "url": url,
            "note": note,
        }
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items())
        return f"@misc{{{slug}-{version},\n{body}\n}}"

    def provenance(self, slug: str, version: str | None = None) -> dict:
        """The record kept with a version: the publisher's file it was read from, with its
        address, name, size and SHA-256, when it was fetched, the licence as the publisher stated
        it and when that was read, and the rows and fields it holds."""
        at = f"v/{_date(version)}" if version else "latest"
        return self._json(f"/d/{_slug(slug)}/{at}/manifest.json")

    def browse(self, slug: str, version: str | None = None) -> str:
        """Opens the dataset's page, or one version's, in the browser and returns its URL."""
        import webbrowser

        url = f"{self.site}/d/{_slug(slug)}/" + (f"v/{_date(version)}/" if version else "")
        webbrowser.open(url)
        return url

    def catalogue(
        self,
        q: str | None = None,
        *,
        jurisdiction: str | None = None,
        status: str | list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Results:
        """Every dataset on the government portals, well over a hundred thousand, of which the
        site serves a small part. Each record has its `id`, `title`, `summary`, `publisher`,
        `jurisdiction`, portal `url`, `licence`, `formats`, `modified`, `status` ("served",
        "votable", "chosen" or "closed"), the site's `page` when served and the `reason` when
        closed. `jurisdiction` is a code such as "qld" or a name such as "Queensland"."""
        jur = None
        if jurisdiction is not None:
            jur = _JUR_CODES.get(str(jurisdiction).lower())
            if not jur:
                raise ValueError(
                    f"jurisdiction is one of {', '.join(sorted(set(_JUR_CODES.values())))}"
                )
        params = {"q": q, "jur": jur, "state": _list(status), "limit": limit, "offset": offset}
        body = self._json("/api/v1/catalogue", params)
        rows = [
            {
                ("jurisdiction" if k == "jur" else "status" if k == "state" else k): v
                for k, v in r.items()
            }
            for r in body.get("rows", [])
        ]
        return Results(rows, body.get("total"), body.get("next_offset"))

    def boundary_layers(self) -> list[dict]:
        """The ABS boundary layers rows join to: council areas, SA2s, suburbs, postal areas and
        state and federal electorates, each with its `key`, `slug`, `title`, the `code` and
        `name` fields that identify an area, and the `version` served."""
        if "places" not in self._memo:
            self._memo["places"] = self._json("/places.json")["layers"]
        return self._memo["places"]

    def _layer(self, layer: str) -> dict:
        want = str(layer).lower()
        for lay in self.boundary_layers():
            if want in (lay["key"].lower(), lay["slug"].lower(), lay["code"].lower()):
                return lay
        keys = ", ".join(lay["key"] for lay in self.boundary_layers())
        raise ValueError(f"no boundary layer {layer!r}; the layers are {keys}")

    def boundaries(self, layer: str, *, cache: bool | None = None):
        """A boundary layer, such as "lga", "sa2", "suburb", "postcode", "state_electorate" or
        "federal_electorate", as a GeoDataFrame in GDA2020 (EPSG:7844). Needs the [geo] extra."""
        return self.read_geo(self._layer(layer)["slug"], cache=cache)

    def join_boundaries(
        self, df, layer: str | None = None, by: str | None = None, *, cache: bool | None = None
    ):
        """`df` with the boundary of the area each row names, by its ABS code, as a GeoDataFrame
        in the order of `df`. The layer is found from a column such as `lga_2025_code` when
        `layer` is None; `by` names the column of codes when it is called something else.
        Numeric codes are compared as text with leading zeros restored, so postcode 800 matches
        "0800". A row whose code matches no area gets no geometry. Aggregate first: 500 council
        areas draw faster than 80,000 rows each carrying its area's shape."""
        import geopandas as gpd
        import pandas as pd

        if isinstance(df, gpd.GeoDataFrame):
            raise ValueError("df already has a geometry; drop it first")
        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(list(df))
        if layer is None:
            hits = [lay for lay in self.boundary_layers() if lay["code"] in df.columns]
            if not hits:
                codes = ", ".join(lay["code"] for lay in self.boundary_layers())
                raise ValueError(
                    f"df has no column named for a boundary code ({codes}); "
                    'pass layer= and by=, as in layer="lga", by="council_code"'
                )
            if len(hits) > 1:
                raise ValueError(
                    f"df has codes for several layers ({', '.join(h['key'] for h in hits)}); choose one with layer="
                )
            lay = hits[0]
        else:
            lay = self._layer(layer)
        col = by or lay["code"]
        if col not in df.columns:
            raise ValueError(f"df has no column {col!r}")
        b = self.boundaries(lay["key"], cache=cache)
        ref = b[lay["code"]].astype(str)
        codes = _norm_codes(df[col], ref)
        index = dict(zip(ref, range(len(ref)), strict=True))
        pos = [index.get(c) if c is not None else None for c in codes]
        out = df.copy()
        out[col] = codes
        if lay["name"] not in out.columns:
            out[lay["name"]] = [b[lay["name"]].iloc[i] if i is not None else None for i in pos]
        geometry = gpd.GeoSeries(
            [b.geometry.iloc[i] if i is not None else None for i in pos], crs=b.crs, index=out.index
        )
        gdf = gpd.GeoDataFrame(out, geometry=geometry, crs=b.crs)
        gdf.attrs["publicdata"] = dict(getattr(df, "attrs", {}).get("publicdata") or {})
        gdf.attrs["boundaries"] = b.attrs.get("publicdata", {})
        missed = sum(1 for c, i in zip(codes, pos, strict=True) if c is not None and i is None)
        if missed:
            warnings.warn(f"{missed} row(s) matched no {lay['noun']} boundary", stacklevel=2)
        return gdf

    # The cache: a version never changes, so a file kept once is never stale.

    def cache_dir(self) -> Path:
        """Where kept files go: PUBLICDATA_CACHE_DIR, else the platform's user cache folder."""
        if self._cache_dir:
            return self._cache_dir
        env = os.environ.get("PUBLICDATA_CACHE_DIR")
        if env:
            return Path(env)
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        elif os.uname().sysname == "Darwin":
            base = Path.home() / "Library" / "Caches"
        else:
            base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        return base / "publicdata-au"

    def cache_list(self, slug: str | None = None, version: str | None = None) -> list[dict]:
        """Every kept file: its dataset, version, file, bytes and when it was saved."""
        root = self._cache_root(slug, version)
        if not root.is_dir():
            return []
        base = self.cache_dir()
        out = []
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.endswith(".part"):
                parts = p.relative_to(base).parts
                st = p.stat()
                out.append(
                    {
                        "dataset": parts[0],
                        "version": parts[1],
                        "file": "/".join(parts[2:]),
                        "bytes": st.st_size,
                        "saved": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
                    }
                )
        return out

    def cache_clear(self, slug: str | None = None, version: str | None = None) -> int:
        """Deletes kept files, every one or one dataset's or one version's, and returns the
        bytes freed."""
        root = self._cache_root(slug, version)
        if not root.is_dir():
            return 0
        freed = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        shutil.rmtree(root)
        return freed

    def _cache_root(self, slug, version) -> Path:
        if version and not slug:
            raise ValueError("a version needs its dataset's slug")
        root = self.cache_dir()
        if slug:
            root = root / _slug(slug)
        if version:
            root = root / _date(version)
        return root

    def _caching(self, cache: bool | None) -> bool:
        return self.cache if cache is None else bool(cache)

    def _fetch(self, slug, format, version=None, table=None, cache=None, tmp: Path | None = None):
        """One version's file: the kept copy when caching, else a download into `tmp`."""
        if format not in FORMATS:
            raise ValueError(f"format must be one of {', '.join(FORMATS)}")
        if not self._caching(cache):
            name = f"tables-{table}.parquet" if table else f"data.{format}"
            return self._save(slug, format, version, tmp / name, table)
        version = _date(version) if version else self.latest(slug)
        name = f"tables/{_table(table)}.parquet" if table else f"data.{format}"
        dest = self.cache_dir() / _slug(slug) / version / name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            try:
                self._save(slug, format, version, part, table)
                os.replace(part, dest)
            finally:
                part.unlink(missing_ok=True)
        return dest, version

    # The licence notice: shown once per dataset, as the dataset's page shows it.

    def _notice(self, slug: str, licence: Mapping | None = None) -> None:
        if slug in self._shown:
            return
        if isinstance(licence, Mapping):
            cond = licence.get("condition") or ""
        elif slug in self._conditions:
            cond = self._conditions[slug]
        else:
            try:
                dp = self._json(f"/d/{_slug(slug)}/datapackage.json")
            except PublicDataError:
                return
            cond = " ".join(
                lic["publicdata:condition"]
                for lic in dp.get("licenses", [])
                if lic.get("publicdata:condition")
            )
            self._conditions[slug] = cond
        if cond:
            self._shown.add(slug)
            warnings.warn(
                f"The licence of {slug!r} sets a condition on its use: {cond}",
                LicenceCondition,
                stacklevel=3,
            )


def _parquet_module():
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return None
    return pq


def _csv_column(col, kind):
    """One column of the gzipped CSV, typed as pyarrow types the Parquet file's column: numbers
    as int64 or float64 (float64 when a whole number is missing), "nan" as NaN, dates as
    datetime.date, timestamps as datetime64 or datetime when out of its range, booleans as bool
    or objects when some are missing, and the suppressed names as arrays."""
    import numpy as np
    import pandas as pd

    present = col.notna()

    def each(f):
        return pd.Series(
            [f(v) if ok else None for v, ok in zip(col, present, strict=True)],
            index=col.index,
            dtype=object,
        )

    if kind == "integer" and present.all():
        return pd.Series([int(v) for v in col], index=col.index, dtype="int64")
    if kind in ("integer", "number"):
        # float() reads "nan" and "inf" as the CSV writer writes them, and round-trips every double.
        conv = float if kind == "number" else int
        return pd.Series(
            [conv(v) if ok else float("nan") for v, ok in zip(col, present, strict=True)],
            index=col.index,
            dtype="float64",
        )
    if kind == "date":
        return each(dt.date.fromisoformat)
    if kind == "datetime":
        # The Parquet file stores milliseconds, which pyarrow 14 and later keep in pandas.
        try:
            stamps = [v if ok else "NaT" for v, ok in zip(col, present, strict=True)]
            return pd.Series(np.array(stamps, dtype="datetime64[ms]"), index=col.index)
        except ValueError:
            return each(dt.datetime.fromisoformat)
    if kind == "boolean":
        out = each(lambda v: v.lower() == "true")
        return out.astype(bool) if present.all() else out
    if kind == "suppressed":
        # The CSV joins the suppressed field names with ";"; the Parquet holds them as a list.
        return pd.Series(
            [
                np.array(v.split(";") if ok and v else [], dtype=object)
                for v, ok in zip(col, present, strict=True)
            ],
            index=col.index,
            dtype=object,
        )
    return col if col.dtype != object else col.where(present, None)


def _absent_why(slug: str, format: str) -> str:
    """Why a version may have no data.<format>, for a 404 on one."""
    head = f"{slug!r} has no data.{format} in this version."
    always = f"{', '.join(ALWAYS)} are on every table version."
    if format in ("gpkg", "geo.parquet"):
        return f"{head} Only datasets with a location or a shape have it. {always}"
    if format == "arrow":
        return (
            f"{head} Arrow is only on versions fetched before the format change, whose "
            f"manifest has no caps field. {always}"
        )
    geo = " GeoJSON is only for datasets with a location or a shape." if format == "geojson" else ""
    return (
        f"{head} Excel, JSON, GeoJSON and SQLite are left out of a version whose table is over "
        f"their size limits, and the version's manifest names the reason under "
        f"formats_left_out.{geo} {always}"
    )


def _datetime(v: str) -> dt.datetime:
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00").replace(" ", "T", 1))


def _bool(v):
    if isinstance(v, bool):
        return v
    if v in (0, 1):
        return bool(v)
    raise ValueError(v)


_CONVERT = {"date": dt.date.fromisoformat, "datetime": _datetime, "boolean": _bool}

_JUR_CODES = {
    **{c: c for c in ("cth", "nsw", "vic", "qld", "wa", "sa", "tas", "act", "nt")},
    "commonwealth": "cth",
    "australian government": "cth",
    "federal": "cth",
    "new south wales": "nsw",
    "victoria": "vic",
    "queensland": "qld",
    "western australia": "wa",
    "south australia": "sa",
    "tasmania": "tas",
    "australian capital territory": "act",
    "northern territory": "nt",
}


def _norm_codes(col, ref) -> list:
    """Codes as text, with the leading zeros a number lost restored when every code has one
    width."""
    import pandas as pd

    widths = {len(r) for r in ref if isinstance(r, str)}
    width = next(iter(widths)) if len(widths) == 1 else 0
    out = []
    for v in col:
        if v is None or v is pd.NA or (isinstance(v, float) and v != v):
            out.append(None)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            whole = float(v).is_integer()
            out.append(str(int(v)).zfill(width) if whole else repr(v))
        else:
            out.append(str(v).strip())
    return out


def _header(kv) -> dict:
    """A file's publicdata table: one row per key, with any value that is not text held as JSON."""
    out = {}
    for k, v in kv:
        if isinstance(v, str) and v[:1] in ("{", "["):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k] = v
    return out


def _is_date(v) -> bool:
    try:
        _date(v)
    except ValueError:
        return False
    return True


def _slug(slug: str) -> str:
    if not slug or not all(c.isalnum() or c == "-" for c in slug):
        raise ValueError(f"not a dataset slug: {slug!r}")
    return slug


def _ident(name: str) -> str:
    if (
        not name
        or not (name[0].isalpha() or name[0] == "_")
        or not all(c.isalnum() or c == "_" for c in name)
    ):
        raise ValueError(f"not a database name: {name!r}")
    return name


def _table(table: str) -> str:
    if not table or not all(c.isalnum() or c == "_" for c in table):
        raise ValueError(f"not a table name: {table!r}")
    return table


def _date(version: str) -> str:
    v = str(version)
    if len(v) != 10 or v[4] != "-" or v[7] != "-" or not (v[:4] + v[5:7] + v[8:]).isdigit():
        raise ValueError(f"a version is a date such as 2026-08-07, not {version!r}")
    return v


def _list(value) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else ",".join(value)


def _page(body: Mapping) -> dict:
    return {
        k: body.get(k) for k in ("dataset_page", "version_page", "this_version", "manifest", "next")
    }


_default = Client()


def datasets(q: str | None = None, **kw) -> list[dict]:
    return _default.datasets(q, **kw)


def dataset(slug: str) -> dict:
    return _default.dataset(slug)


def versions(slug: str) -> list[dict]:
    return _default.versions(slug)


def rows(slug: str, where: Mapping[str, Any] | None = None, **kw) -> Rows:
    return _default.rows(slug, where, **kw)


def aggregate(slug: str, group=None, metric="count", where=None, **kw) -> Rows:
    return _default.aggregate(slug, group, metric, where, **kw)


def download(
    slug: str,
    format: str = "parquet",
    version: str | None = None,
    path=None,
    **kw,
) -> Path:
    return _default.download(slug, format, version, path, **kw)


def read(slug: str, version: str | None = None, **kw):
    return _default.read(slug, version, **kw)


def read_geo(slug: str, version: str | None = None, **kw):
    return _default.read_geo(slug, version, **kw)


def relation(slug: str, table: str | None = None, version: str | None = None, **kw):
    return _default.relation(slug, table, version, **kw)


def latest(slug: str) -> str:
    return _default.latest(slug)


def changes(slug: str, since: str | None = None, until: str | None = None) -> list[dict]:
    return _default.changes(slug, since, until)


def diff(slug: str, version: str | None = None) -> dict:
    return _default.diff(slug, version)


def cite(slug: str, version: str | None = None, **kw) -> str:
    return _default.cite(slug, version, **kw)


def fields(slug: str, version: str | None = None) -> list[dict]:
    return _default.fields(slug, version)


def file_url(slug: str, format: str = "parquet", version: str | None = None, table=None) -> str:
    return _default.file_url(slug, format, version, table)


def provenance(slug: str, version: str | None = None) -> dict:
    return _default.provenance(slug, version)


def browse(slug: str, version: str | None = None) -> str:
    return _default.browse(slug, version)


def catalogue(q: str | None = None, **kw) -> Results:
    return _default.catalogue(q, **kw)


def boundary_layers() -> list[dict]:
    return _default.boundary_layers()


def boundaries(layer: str, **kw):
    return _default.boundaries(layer, **kw)


def join_boundaries(df, layer: str | None = None, by: str | None = None, **kw):
    return _default.join_boundaries(df, layer, by, **kw)


def cache_dir() -> Path:
    return _default.cache_dir()


def cache_list(slug: str | None = None, version: str | None = None) -> list[dict]:
    return _default.cache_list(slug, version)


def cache_clear(slug: str | None = None, version: str | None = None) -> int:
    return _default.cache_clear(slug, version)


def schema(slug: str, version: str | None = None) -> dict:
    return _default.schema(slug, version)


def tables(slug: str, version: str | None = None) -> list[dict]:
    return _default.tables(slug, version)


def connect(slug: str, version: str | None = None, **kw):
    return _default.connect(slug, version, **kw)


for _f in (
    datasets,
    dataset,
    versions,
    latest,
    rows,
    aggregate,
    download,
    read,
    read_geo,
    relation,
    schema,
    tables,
    connect,
    changes,
    diff,
    cite,
    cache_dir,
    cache_list,
    cache_clear,
    fields,
    file_url,
    provenance,
    browse,
    catalogue,
    boundary_layers,
    boundaries,
    join_boundaries,
):
    _f.__doc__ = getattr(Client, _f.__name__).__doc__
