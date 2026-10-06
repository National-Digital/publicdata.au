# publicdata-au

Query and download Australian government open data from [publicdata.au](https://publicdata.au/).
publicdata.au republishes datasets that governments already publish under open licences, keeps
every version at a URL that never changes, and serves each one as Parquet, CSV, Excel, JSON and
other formats with a query API.

This package works for every dataset the site serves, named by its slug, so a dataset added to
the site needs no new release.

```
pip install publicdata-au            # queries and downloads, no dependencies
pip install "publicdata-au[pandas]"  # adds read() into a pandas DataFrame
pip install "publicdata-au[duckdb]"  # adds connect() and relation(), DuckDB on a version
pip install "publicdata-au[geo]"     # adds read_geo() into a geopandas GeoDataFrame
```

## Find a dataset

```python
import publicdata_au as pd_au

pd_au.datasets("road crashes")  # slug, title, publisher, licence and page of each match
pd_au.datasets(topic="roads", jurisdiction="Qld")
pd_au.datasets(publisher="Bureau of Meteorology")
pd_au.versions("au-road-deaths")  # every version kept, newest first
pd_au.fields("au-road-deaths")  # each field's type, description, range and values

pd_au.catalogue("water quality", jurisdiction="Queensland")  # every portal dataset, served or not
```

## Query rows and totals

```python
from publicdata_au import gte, in_

deaths = pd_au.rows(
    "au-road-deaths",
    {"state": in_("QLD", "NSW"), "year": gte(2020)},
    select=["state", "year", "road_user"],
    order="year.desc",
    all=True,
)
deaths.version  # the version the rows came from
deaths.attribution  # the attribution the publisher's licence requires
deaths.to_pandas()

pd_au.aggregate("au-road-deaths", group="state", metric="count", where={"year": 2025})
```

Values come back typed as their fields say: dates as `datetime.date`, timestamps as
`datetime.datetime` and booleans as `bool`. A plain value must match exactly, a list matches any
of its values and None matches a blank or suppressed cell. The filters are `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `like`, `ilike`, `in_`,
`is_null` and `not_`.

Without `version=` an answer comes from the newest version and changes when the publisher
releases again. Pass a date from `versions()` for an answer that never changes.

## Whole tables

```python
df = pd_au.read("au-road-deaths")  # needs the [pandas] extra
df.attrs["publicdata"]  # the version, licence and attribution the file itself carries
pd_au.download("au-road-deaths", "csv")  # or parquet, csv.gz, json, ndjson, sqlite, duckdb, ...
```

`read()` reads the Parquet file with pyarrow. Without pyarrow it reads the gzipped CSV and types
each column from the version's fields as the Parquet path does: whole numbers as int64 (float64
when one is missing), dates as `datetime.date`, timestamps as `datetime64[ms]`, booleans as bool
(objects when one is missing) and `suppressed` as arrays of field names. An `int32` field reads
as int64, and a shape layer's `geometry` column is in the Parquet file only, so read a layer with
pyarrow. Parquet, CSV, CSV (gzip), NDJSON and DuckDB are on every version. Excel, JSON, GeoJSON and SQLite are left out of a version whose table is over their size
limits, and the version's page says why. Arrow files are only on versions fetched before the format
change, whose manifest has no `caps` field.

A version never changes once published, so a downloaded file can be kept and reused. Nothing is
kept unless you ask, with `cache=True` on a call or `PUBLICDATA_CACHE=1` for every call:

```python
df = pd_au.read("au-road-deaths", cache=True)  # downloaded once, read from disk after
pd_au.cache_list()  # what is kept, in cache_dir()
pd_au.cache_clear("au-road-deaths")
```

## Query a version in place

Every version has a DuckDB file, and `connect()` attaches it read-only over HTTPS. Only the
blocks a query touches are read, so a count over millions of rows runs without a download.

```python
con = pd_au.connect("au-road-deaths")  # needs the [duckdb] extra
con.sql("SELECT state, count(*) FROM records GROUP BY 1").df()
con.publicdata  # the version, its URL, licence, attribution and citation

r = pd_au.relation("au-road-deaths")  # one table as a lazy DuckDB relation
r.filter("year >= 2020").aggregate("state, count(*) AS n").df()
```

A database such as G-NAF is one DuckDB file holding every table, the keys between them and the
publisher's views, with one Parquet file per table beside it.

```python
pd_au.tables("gnaf")  # every table with its fields, keys and references
con = pd_au.connect("gnaf")
con.sql("SELECT postcode, count(*) AS n FROM address_view GROUP BY 1 ORDER BY 2 DESC LIMIT 10").df()
pd_au.read("gnaf", table="locality")  # one table as a pandas DataFrame
pd_au.download("gnaf", table="state")  # one table as Parquet
```

For heavy work on a large database, `connect("gnaf", cache=True)` downloads the file once and
queries it from disk. G-NAF's DuckDB file is about 3 GB.

## Maps

Rows of a point dataset carry the codes of the ABS areas their point falls in: council area, SA2,
suburb, postal area and state and federal electorate. Total by one and `join_boundaries()`
attaches the boundaries, in GDA2020 (EPSG:7844). Any DataFrame with a column of codes works, with
`layer=` and `by=`.

```python
by_sa2 = pd_au.aggregate("act-road-crashes", group="sa2_2021_code")
gdf = pd_au.join_boundaries(by_sa2.to_pandas())  # needs the [geo] extra
gdf.plot(column="count")
pd_au.boundary_layers()  # the layers and their code fields
```

Datasets with a location or a shape have their own GeoPackage, which `read_geo()` reads in the
reference system the publisher used.

## What changed

Each release is compared with the one before it on the dataset's key.

```python
pd_au.changes("rba-money-market-daily")  # one entry per release: added, removed, changed
pd_au.diff("rba-money-market-daily")  # the newest release in full, with the keys
pd_au.provenance("au-road-deaths")  # the source file, its checksum and fetch time
```

`file_url(slug, format, version)` gives a file's fixed address for another tool. A `Client` closes
the connections `relation()` opened when used as a context manager or with `close()`. Failing to
reach the site raises `SiteUnreachable`, a `PublicDataError`.

Files have no rate limit. The query API allows 60 requests in 10 seconds from one address, and
this package waits and retries when it answers 429 or a passing server error.

## Licence and attribution

The data is under each publisher's own licence, which requires the attribution string that every
answer carries. Please also name publicdata.au and link to the version you used; `cite(slug)` gives
the citation and `cite(slug, format="bibtex")` the BibTeX entry. Where a licence sets a condition
beyond attribution, such as G-NAF's rule on mail compilation, the package warns once per dataset
with a `LicenceCondition` warning. publicdata.au is
an independent republication, and the publishers have not endorsed it.

The package itself is under the MIT licence.
