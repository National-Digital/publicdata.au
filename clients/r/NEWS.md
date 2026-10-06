# publicdataau 0.5.0

* `pd_read()` reads the version's gzipped CSV, typed from its fields, when
  'arrow' is not installed or was built without zstd, instead of stopping. A
  table of a database is served only as Parquet and still needs 'arrow'.
* `pd_download()` documents the formats every version carries. Excel, JSON,
  GeoJSON and SQLite are left out of a version whose table is over their size
  limits, and Arrow files are only on versions made before October 2026.

# publicdataau 0.4.1

* `pd_read()` stops with a clear error when 'arrow' was built without zstd,
  which the site's Parquet files use, and its example is skipped there.

# publicdataau 0.4.0

* Answers are tibbles. `pd_rows()` and `pd_aggregate()` type each column as its
  field says, so dates come back as `Date` and booleans as logical, and every
  answer labels its columns with the fields' descriptions.
* `pd_fields()` lists a dataset's fields with their types, descriptions,
  ranges and values.
* `pd_join_boundaries()` attaches the ABS boundary of the area each row names
  by its code, and `pd_boundaries()` and `pd_boundary_layers()` read the
  layers.
* `pd_catalogue()` searches every dataset on the government portals, served
  or not.
* `pd_provenance()` gives a version's source file, checksum and fetch time.
* `pd_url()` gives a file's address and `pd_latest()` the newest version.
* `pd_browse()` opens a dataset's page.
* `pd_read()` takes `columns` to read only some fields.
* Errors have the class `publicdataau_error`, and `publicdataau_unreachable`
  when the site cannot be reached.
* Large downloads show a progress bar in interactive sessions.
* A vignette, "Getting started with publicdataau", and a `CITATION` file.

# publicdataau 0.3.0

* `pd_tbl()` returns a table or view of a version's DuckDB file as a lazy 'dplyr'
  table, so dplyr verbs run as SQL against the file over HTTPS.
* `pd_sf()` reads a dataset's map layer as an 'sf' object.
* `pd_read()`, `pd_download()`, `pd_connect()`, `pd_tbl()` and `pd_sf()` take
  `cache` to keep a version's file in `pd_cache_dir()` and reuse it; nothing
  is kept unless asked. `pd_cache_list()` and `pd_cache_clear()` manage it.
* `pd_changes()` lists what each release changed and `pd_diff()` reads one
  comparison in full.
* `pd_cite()` gives a version's citation as a `bibentry`, for text or BibTeX.
* `pd_datasets()` takes `publisher`, `topic` and `jurisdiction`.
* A licence condition beyond attribution, such as G-NAF's, is shown once per
  session; `options(publicdataau.quiet = TRUE)` turns it off.
* `pd_connect()` carries the file's attribution and citation, so
  `pd_attribution()` works on a connection, and keeps 'DuckDB''s extensions
  out of the home directory on 'duckdb' releases before 1.5.5 too.
* `pd_download()` takes the `"geo.parquet"` format.

# publicdataau 0.2.0

* `pd_connect()` attaches a version's DuckDB file read-only over HTTPS and returns a 'DBI'
  connection, so a query runs against the version without a download.
* `pd_tables()` and `pd_schema()` list the tables of a database such as G-NAF with their
  fields, keys and references.
* `pd_read()` and `pd_download()` take `table` to read one table of a database as Parquet.
* `pd_download()` takes the new `"duckdb"` format.
* `pd_available()` says whether publicdata.au can be reached, and every
  function now stops with a message naming the site when it cannot.

# publicdataau 0.1.0

* First release: `pd_datasets()`, `pd_versions()`, `pd_dataset()`, `pd_rows()`, `pd_aggregate()`, `pd_download()`, `pd_read()` and the filter helpers.
