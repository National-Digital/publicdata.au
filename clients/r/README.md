# publicdataau

Query and download Australian government open data from [publicdata.au](https://publicdata.au/)
in R. publicdata.au republishes datasets that governments already publish under open licences,
keeps every version at a URL that never changes, and serves each one as Parquet, CSV, Excel, JSON
and other formats with a query API.

Every function takes a dataset's slug, so a dataset added to the site works with no new release.

```r
install.packages("publicdataau")
```

The development version installs from GitHub:

```r
remotes::install_github("National-Digital/publicdata.au/clients/r")
```

Bugs and requests go to the [issue tracker](https://github.com/National-Digital/publicdata.au/issues).

## Find a dataset

```r
library(publicdataau)

pd_datasets("road crashes")      # slug, title, publisher, licence and page of each match
pd_datasets(topic = "roads", jurisdiction = "Qld")
pd_datasets(publisher = "Bureau of Meteorology")
pd_versions("au-road-deaths")    # every version kept, newest first
pd_fields("au-road-deaths")      # each field's type, description, range and values

pd_catalogue("water quality", jurisdiction = "Queensland")  # every portal dataset, served or not
```

## Query rows and totals

```r
deaths <- pd_rows(
  "au-road-deaths",
  state = c("QLD", "NSW"),
  year = pd_gte(2020),
  .select = c("state", "year", "road_user"),
  .order = "year.desc",
  .all = TRUE
)
pd_attribution(deaths)           # the attribution the publisher's licence requires
attr(deaths, "publicdata")       # the version, licence and citation the answer came with

pd_aggregate("au-road-deaths", group = "state", metric = "count", year = 2025)
```

Answers are tibbles, with each column typed as its field says (dates as `Date`, booleans as
logical) and labelled with the field's description. A plain value must match exactly, several
values match any of them and `NA` matches a blank or suppressed cell. The filters are `pd_eq()`, `pd_neq()`, `pd_gt()`, `pd_gte()`, `pd_lt()`,
`pd_lte()`, `pd_like()`, `pd_ilike()`, `pd_in()`, `pd_is_null()` and `pd_not()`.

Without `.version` an answer comes from the newest version and changes when the publisher
releases again. Pass a date from `pd_versions()` for an answer from that version alone.

## Whole tables

```r
crashes <- pd_read("au-road-deaths")                   # Parquet with arrow, else the CSV
path <- pd_download("au-road-deaths", "csv", path = "au-road-deaths.csv")
```

`pd_read()` reads the Parquet file with 'arrow' when it is installed with zstd. Otherwise it reads
the gzipped CSV and types the columns from the version's fields as 'arrow' would, with whole
numbers past 32 bits as integer64 when 'bit64' is installed and doubles when it is not. A shape
layer's `geometry` column is in the Parquet file only, so read a layer with 'arrow'. Parquet, CSV,
CSV (gzip), NDJSON and DuckDB are on every version. Excel, JSON, GeoJSON and SQLite are left out of a version whose
table is over their size limits, and the version's page says why. Arrow files are only on versions
fetched before the format change, whose manifest has no `caps` field.

A version keeps its content once published, so a downloaded file can be kept and reused. A
correction to a version is recorded in its notes, and `pd_cache_clear()` removes the old copy. Nothing is
kept unless you ask:

```r
options(publicdataau.cache = TRUE)                    # or cache = TRUE on one call
crashes <- pd_read("au-road-deaths")                  # downloaded once, read from disk after
pd_cache_list()                                       # what is kept, in pd_cache_dir()
pd_cache_clear("au-road-deaths")
```

Files have no rate limit. The query API allows 60 requests in 10 seconds from one address, and
this package waits and retries when it answers 429 or a passing server error.

## Query a version in place

Every version has a DuckDB file. `pd_tbl()` attaches it read-only over HTTPS and hands back a
table for dplyr: verbs become SQL that DuckDB runs against the file, reading only the blocks they
touch, and `collect()` brings the answer into R.

```r
library(dplyr)                                        # needs the dbplyr and duckdb packages
pd_tbl("au-road-deaths") |>
  filter(year >= 2020) |>
  count(state, road_user) |>
  collect()
```

`pd_connect()` gives the same file as a `DBI` connection for SQL.

```r
con <- pd_connect("au-road-deaths")                   # needs the DBI and duckdb packages
DBI::dbGetQuery(con, "SELECT state, count(*) AS n FROM records GROUP BY 1")
attr(con, "publicdata")                               # the version, its URL and its licence
DBI::dbDisconnect(con)
```

A database such as G-NAF is one DuckDB file holding every table, the keys between them and the
publisher's views, with one Parquet file per table beside it.

```r
pd_tables("gnaf")                                     # every table with its rows, fields and key
con <- pd_connect("gnaf")
DBI::dbGetQuery(con, "SELECT postcode, count(*) AS n FROM address_view
                      GROUP BY 1 ORDER BY 2 DESC LIMIT 10")
pd_tbl("gnaf", "locality") |> filter(primary_postcode == "4000") |> collect()
localities <- pd_read("gnaf", table = "locality")     # one table as a data frame
```

For heavy work on a large database, `cache = TRUE` downloads the file once and queries it from
disk. G-NAF's DuckDB file is about 3 GB.

## Maps

Rows of a point dataset carry the codes of the ABS areas their point falls in: council area,
SA2, suburb, postal area and state and federal electorate. Total by one and
`pd_join_boundaries()` attaches the boundaries, in GDA2020 (EPSG:7844). Any data frame with a
column of codes works, with `layer` and `by`.

```r
by_sa2 <- pd_aggregate("act-road-crashes", group = "sa2_2021_code")
map <- pd_join_boundaries(by_sa2)                     # needs the sf package
plot(map["count"])

my_councils <- data.frame(code = c("31000", "33430"), spend = c(12, 9))
pd_join_boundaries(my_councils, layer = "lga", by = "code")
pd_boundary_layers()                                  # the layers and their code fields
```

Datasets with a location or a shape have their own GeoPackage, which `pd_sf()` reads as an `sf`
object in the reference system the publisher used.

## What changed

Each release is compared with the one before it on the dataset's key.

```r
pd_changes("rba-money-market-daily")                  # one row per release: added, removed, changed
pd_diff("rba-money-market-daily")                     # the newest release in full, with the keys
pd_provenance("au-road-deaths")                       # the source file, its checksum and fetch time
```

## Reproducible work

Pass a date from `pd_versions()` for an answer from that version alone. `pd_url()` gives a file's fixed
address for another tool, or for a [targets](https://docs.ropensci.org/targets/) pipeline that
reruns when the publisher releases again:

```r
tar_target(deaths_url, pd_url("au-road-deaths", version = pd_latest("au-road-deaths")), format = "url")
```

Every error the package raises has the class `publicdataau_error`, and one from failing to reach
the site also has `publicdataau_unreachable`, so a pipeline can catch either.

## Licence and attribution

The data is under each publisher's own licence, which requires the attribution string that every
answer carries. Please also name publicdata.au and link to the version you used; `pd_cite()` gives
the citation, and `utils::toBibtex(pd_cite(slug))` the BibTeX entry. Where a licence sets a
condition beyond attribution, such as G-NAF's rule on mail compilation, the package says so once
per session. publicdata.au is
an independent republication, and the publishers have not endorsed it.

The package itself is under the MIT licence. `citation("publicdataau")` cites it.
