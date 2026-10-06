# Architecture

One job: take a dataset a government already publishes under an open licence and serve it
as immutable, versioned, schema-carrying files that never send traffic back to the source.

## The shape

```
register/**/<slug>.yaml source, fetch strategy, cadence, licence + evidence, attribution,
                       column allow-list, geometry, partition key, status
pipeline/publicdata/
  fetch.py             adapters: file (a fixed URL, licence checked against the evidence page's
                       words), kiwis (Water Data Online series as one CSV), aihw (an AIHW data
                       page's newest listed file), zenodo (a concept record's newest version),
                       ckan-stack (many workbooks across packages as one table), ckan-resource (a file, zipped or not), socrata, opendatasoft,
                       arcgis-hub, arcgis-feature (a layer read in pages into one GeoJSON,
                       because the publisher offers no file) and ala (the Atlas of Living
                       Australia's search API: one query over named government providers,
                       sliced by load time and place because the API pages 100 rows to a
                       depth of 5,000)
  register_draft.py    a first register entry from a CKAN dataset URL, for review
  store.py             raw/<slug>/<YYYY-MM-DD>/<sha256>.<ext> + manifest, append-only
  normalise.py         CSV/XLSX/GeoJSON -> tabular model + Table Schema, allow-listed columns;
                       a point geometry becomes two fields, a zip yields its named member
  serialise/           pure functions: json, ndjson, csv, csv.gz, parquet, sqlite, duckdb, xlsx,
                       geojson, gpkg, geo.parquet, pmtiles, schema.sql, csvw, and arrow for
                       versions fetched before the size limits; formats_for picks a version's set,
                       datapackage, dcat, llms.txt
  gate.py              fail-closed checks over dist/
  site.py              dataset pages, request pages, agency pages, from the same model
  figures.py           build-time figures: rows per year and rows per map cell, counted in
                       each version's data.parquet and drawn as inline SVG; a part year is
                       left out and named, a state with no located data is drawn hatched;
                       a register entry's chart block keeps the chart to the rows it names
                       and sets its colour and its measure
  topics.py            the topics a register entry files under; the home page's way in and a
                       page per topic listing what is served and what is coming
  brand.py             the mark, favicons, app icons, web app manifest, 1200x630 social cards
  hubs.py              copies each newest version to Hugging Face, Zenodo and Kaggle
  __main__.py          register validate | draft | labels, fetch, build, gate, and the rest
```

## Rules that decide the code

1. The published site never fetches upstream.
2. Nothing derived. Re-keying, re-typing, declared joins only. A database's views are the
   publisher's own scripts, kept in the register.
3. Suppression survives as a typed flag, never a number or a blank.
4. Ingest is an allow-list; unknown upstream columns are reported and held.
5. Licence is read every run and gates the publish. Old versions stay up under the licence
   they were published under.
6. Versions are immutable and dated by source change; unchanged hash, no version.
7. Serialisers are pure functions of the model.
8. Every payload carries provenance.
9. Requesters are organisations, never people.
10. Every page says the publisher has not endorsed the site.
11. Every page passes WCAG 2.2 AA in both colour schemes; CI runs axe over the fixture build
    (`scripts/a11y.mjs`). A map is a PNG file under `maps/` in an `img` whose alt is worked
    out from the cells, with a vector SVG over it; no SVG embeds a raster, and every figure's
    label is derived from what it draws.

## Licence gate

| As found | Decision |
|---|---|
| CC BY 4.0, CC BY 3.0 AU, CC BY 2.5 AU, CC0 | publish, attribution in the publisher's own words |
| CC BY-SA | publish, outputs carry SA |
| an open licence with a condition of use (the Open G-NAF EULA: CC BY 4.0 plus a restriction on compiling addresses for mail) | publish, with the condition stated on the dataset page, the version pages and in every provenance header (`licence.condition`); the gate refuses a page that leaves it out; never copied to the hubs, since no hub can state it (`register.LICENCE_CONDITIONS`) |
| "Licence specified by agency" | hold as `assessing` until a human records evidence |
| not specified, blank | hold as `blocked` pending written confirmation |
| any ND, NC-ND, restricted | never; listed as blocked with the reason |

## URL contract

```
/catalog.json                        DCAT-AP JSON-LD over every live dataset
/requests.json                       every register entry with status and blocked reason
/d/<slug>/                           dataset page
/d/<slug>/datapackage.json           Frictionless envelope pointing at latest
/d/<slug>/schema.json                Table Schema
/d/<slug>/schema.xlsx                the same field list as an Excel data dictionary
/d/<slug>/in/<value>/                one page per value of place_field: its rows counted,
                                     drawn and linked; a WebPage about the dataset
/d/<slug>/versions.json              every version: date, as-at, rows, fields, hash
/d/<slug>/changes.json               consecutive diffs: row deltas and schema diffs
/d/<slug>/diff/<a>..<b>.json         diff between two consecutive versions
/d/<slug>/history.tar.zst            every version's parquet + manifest
/d/<slug>/latest/  -> /d/<slug>/v/<YYYY-MM-DD>/      302, max-age 300
/d/<slug>/v/<date>/                  version page: kept and cited, noindex, not in the sitemap
/d/<slug>/v/<date>/data.{ndjson,csv,csv.gz,parquet,duckdb}     on every table version
/d/<slug>/v/<date>/data.{json,sqlite,xlsx}                       within their size limits
/d/<slug>/v/<date>/data.{gpkg,geo.parquet,geojson,pmtiles}        with coordinates or shapes
/d/<slug>/v/<date>/data.arrow        versions fetched before the size limits only
/d/<slug>/v/<date>/by/<field>/<value>.json           where partition_by is declared
/d/<slug>/v/<date>/manifest.json     source URL, fetched-at, SHA-256 of source bytes
/d/<slug>/v/<date>/source.<ext>      the bytes as fetched, served from publicdata-raw
/d/<slug>/openapi.json               OpenAPI for this dataset's query paths
/d/<slug>/explore/                   the explorer; ?view=<id> opens a saved dashboard
/d/<slug>/embed/                     the same dashboard in a frame, with the attribution
/api/v1/views, /api/v1/views/<id>    save and read explorer dashboards
/government/                         what a public servant needs before using the site
/llms.txt
/health.json
```

Immutable versions are cached for a year. Every dataset page carries schema.org Dataset
JSON-LD.

## What every dataset gets

A register entry that passes `register validate` and has a stored version gets all of this from
the build, with nothing written by hand:

- the dataset page, its Markdown twin, schema.org Dataset JSON-LD and a catalogue record;
- one dated, immutable version per source change, each with its files, its manifest, the
  publisher's own file and a diff against the version before. Every table version has Parquet,
  CSV, CSV (gzip), NDJSON and DuckDB. A table with coordinates adds GeoParquet as
  `data.geo.parquet` and a GeoPackage, and a polygon or line layer keeps its shapes in
  `data.parquet`, which is GeoParquet, with a GeoPackage and PMTiles vector tiles. The DuckDB
  file attaches read-only over HTTPS (the R2 function answers range requests), so a query runs
  against a version without a download;
- the formats that grow with the table, each written only while the version is within its
  limit. SQLite is written up to 500 MB of CSV, Excel up to 50 MB of CSV and 1,048,575 rows,
  JSON up to 50 MB of NDJSON, and GeoJSON up to 100 MB of NDJSON. The NDJSON and CSV are written
  first and their byte counts decide the rest, so two builds of one snapshot make the same
  choice. The manifest records those byte counts as `measured_bytes` and each format left out
  with its reason as `formats_left_out`, the version and dataset pages say why the file is not
  there, and the gate refuses a version that publishes a file past its cap. A version fetched
  before `serialise.CAPPED_FROM` keeps the set it was built with, Arrow included, since a
  dated version never changes; Arrow is not written for any later version;
- partition files for each `partition_by` field;
- the query API over D1 for the newest versions, with OpenAPI at `/d/<slug>/openapi.json` and
  the query console on the dataset page, whose field values and first query come from the
  version's data.parquet;
- the explorer at `/d/<slug>/explore/` and its frame at `/d/<slug>/embed/`, whose first
  dashboard comes from the same field hints as the console;
- a figure band on the dataset page and each version page: rows per year, split by the same
  field the explorer colours by, and a map of the rows when they have coordinates. Each is a
  count of the rows in that version, worked out in the build from data.parquet. A year that
  ends after the version's as-at date (or its fetched date) is not drawn and the caption says
  so, so a chart never falls away at a part year. The explorer shows the same figures, dimmed,
  while it loads. The home page overlays the maps of the located crash datasets named in
  `HERO_MAP` and hatches each state none of them covers, so a state with no published data
  never reads as a state with no crashes.

## Parquet profile

Parquet files follow one profile (`serialise/profile.py`, ADR 0008), and say so in their footer:
the key `publicdata.profile` holds the profile version, now `1`, beside the `publicdata`
provenance key. A reader checks that key before it relies on the order, the sizes or the page
index.

A published file never changes (ADR 0002), so the profile reaches a version in one of two ways.

- A version's own files keep the layout its fetch recorded. `fetch` writes the register entry's
  layout (`profile.layout`: the profile version, `sort`, `key`, `lookup` and `int32`) into the
  manifest as `parquet`, and the version's data.parquet, data.duckdb, a layer's GeoParquet and a
  database's tables follow that record for good. A later edit to the entry, or a later profile,
  never changes them, so a profile writer is kept for every version that records it. A version
  whose manifest has no `parquet`, fetched before the profile, keeps the writer it was published
  with: 65,536-row groups, 64-bit integers, the publisher's order and no profile key. Its rebuilt
  data.parquet is byte for byte the file in R2.
- Every table version has a query copy under the current profile and register entry
  (`profile.query_key`, `_q/<slug>/<version>.parquet`), which the query engine reads. For a version
  whose layout is the current one the copy is its data.parquet, the same bytes; otherwise it is
  written again. See Hosting.

The profile itself:

- Order. Without `sort:` the rows keep the publisher's order. With it, the rows are sorted by the
  `sort:` fields, then the `key`, then each row's position in the source, so the order is
  complete and two builds agree byte for byte. Nulls sort last. DuckDB works out the order once
  per version, since it spills to disk, and every writer that sorts takes it. A sorted file
  records `sorting_columns` (the sort fields, then the key) in every row group.
- Scope. The sort applies to data.parquet and to what is made from it: data.duckdb, the history
  archive and a layer's GeoParquet. JSON, NDJSON, CSV, Excel, SQLite, Arrow, the partition files
  and the publisher's file keep the source order. The figures, the query console and the D1 load
  read data.parquet, so they see its order.
- Lookups. Each `lookup:` field gets a bloom filter in every row group, sized from its distinct
  values with a 1% false-positive rate, so an equality lookup can skip row groups when the sort
  serves another filter.
- Types. Each `int32:` field is written as INT32 in every version, whatever its values, so a field
  has one type across versions and parts. A version holding a value outside 32 bits stops the
  build. No other column is narrowed. The build reads INT32 back as 64 bits (`profile.widen`), so
  a diff or a format written from the Parquet sees the types normalise made.
- Encoding. zstd, dictionary encoding and statistics on every column. The page index (column and
  offset indexes) is written on sorted files only, since on an unsorted file it saves no work
  and multiplies the reads.
- Sizes. 500,000 rows to a row group, set by the October 2026 follow-up benchmark. A page ends at
  10,000 rows or 8 MB, whichever comes first, so long text and geometry stay in bounded pages.
  The tables of a `kind: database` release follow the profile without a sort.

`rows_sha256` hashes each row and sorts the hashes, so a new `sort:` never cuts a new version.
data.geo.parquet, the GIS download for a table with coordinates, is written by DuckDB's spatial
extension and is outside the profile.

A register entry with `kind: database` is a publisher's release of several related tables, such
as G-NAF: an archive of delimited files, which `database.member_match` groups into tables by a
named pattern, each table with its own field allow-list, key and `references` (table.field), and
the publisher's views as SQL. The build reads the archive a table at a time through DuckDB, so a
release of gigabytes builds within a few gigabytes of memory, and writes one `data.duckdb` with
every table, view, key and reference (and `publicdata`, `fields`, `tables` and `relations`
tables), one Parquet file per table under `tables/`, `schema.json` with every table's Table
Schema and keys, and `schema.sql` with the CREATE TABLE statements, references and views. A
database has no JSON, CSV, Excel or SQLite files, no partitions, no query API and no explorer; its
page lists the tables and shows how to attach the file from R, Python and DuckDB, and two versions
are compared table by table by row count. The DuckDB file's bytes are not reproducible, since its
storage picks a compression for each block by sampling, so CI compares DuckDB files by content
(`python -m publicdata.dbcheck`) and everything else byte for byte.

What shapes the defaults is the register: field types, `key` and `partition_by`. A table keyed by
several fields with a count field is charted as the sum of that count; any other table counts its
rows. A text field with 2 to 30 values becomes the master bar chart; a date field, or an integer
field named for a year, becomes the trend. A dataset whose Parquet is over 100 MB gets no explorer,
because the explorer holds the whole file in the browser.

## Explorer

`/d/<slug>/explore/` loads DuckDB-WASM and Perspective, downloads one dated version's
data.parquet, loads it into an in-memory table and draws a Perspective workspace over it:
panels, drag layout, master and detail cross-filtering, chart types, and export of any panel as
CSV, JSON, Arrow or PNG. Every chart is a DuckDB query in the browser, so the explorer puts no
load on the query API and has no rate limit. Integer fields that fit 32 bits are written as INT32
under the Parquet profile; the explorer still casts them to INTEGER, which leaves a profile file
unchanged and keeps an older file's BIGINT columns showing as integers in Perspective.

The whole dashboard is the URL fragment: the workspace JSON, deflated and base64url-encoded, plus
the version when it is not the newest. "Save a short link" posts it to `/api/v1/views`, which
checks the dataset and version exist and the settings are ones the viewer knows, and stores it in
the private votes bucket under `dash/<id>.json`, where the id is a hash of the content. A saved
dashboard never changes and is cached for good. `/d/<slug>/embed/?view=<id>` shows it in a frame
with the attribution and a link back; it is the only page that may be framed.

The libraries come from `package-lock.json`. The build copies the files the browser loads under
`/static/vendor/<hash>/`, where the hash covers every file, so a library upgrade is a new path and
never meets a cached copy of the old one. DuckDB's WASM binary is gzipped to fit the Pages file
limit and inflated in the browser. Parquet support is a DuckDB extension, which the build fetches
at a pinned version and SHA-256 and serves from the same tree. The explorer pages, the frame and
the vendor tree carry their own Content-Security-Policy with `'wasm-unsafe-eval'` and `blob:`
workers; every other page keeps the site policy. The gate fails an explorer page whose vendor
files or Parquet are missing.

## Archive

This site is the version history the portals do not keep. The archive role has its own rules.

- Every version is kept indefinitely. A version is never deleted or rewritten, including when
  the publisher withdraws or replaces the source file. The only exception is a legal takedown,
  which is recorded in `changes.json` as a tombstone that keeps the manifest and hash.
- History is backfilled. Where a portal still lists earlier releases as separate resources,
  each becomes a version dated by the release's own as-at date, with `backfilled: true` in
  its manifest.
- Raw bytes are kept for every version in append-only object storage with versioning on, and
  a `history` branch in git holds every manifest and diff report, so the archive can be
  rebuilt from either.
- Any two versions can be compared: `/d/<slug>/diff/<a>..<b>.json` lists added, removed and
  changed rows by the declared key, and field-level schema differences. `changes.json` is
  the same for consecutive pairs.
- `versions.json` per dataset lists every version with date, as-at, row count, field count,
  source hash and encoding. `/d/<slug>/history.tar.zst` bundles every version's data.parquet
  and manifest for offline use.
- Publishers can cite a version URL knowing it will resolve to the same bytes in ten years.

## Hosting

Pages on Cloudflare Pages (project `publicdata-au`), data in R2. After the build, `publicdata
split --versioned` moves every file of a dated version, its page included, and anything over the
Pages per-file limit, into a tree that is pushed to the R2 bucket `publicdata-dist` at the same
key as its URL path. Dated keys are written once; a mutable key such as `history.tar.zst` is
rewritten when its SHA-256 changes. `_routes.json` runs the function only on `latest/`, dated version trees, diffs and the history
archive, so a dataset page and its JSON are served as Pages files; split refuses a large file no
route reaches. The Pages Function under `functions/d/` serves a static file
when Pages has it, redirects `latest/` from `latest.json`, and otherwise streams the object from
R2 with byte ranges, a sized HEAD and immutable caching. A dataset missing from `latest.json` (the register withheld it)
answers 410 for every file R2 still holds, `latest/` included, and so does each path in
`withheld.json`, the publisher's files of an entry with `source_withheld`, which the build stops
writing but R2 kept. Query copies (`_q/<slug>/<version>.parquet`) go to `publicdata-dist` alone:
split moves every one into the R2 tree whatever its size, no route or page reaches them, and
nothing links them. They are stored as plain bytes, never gzipped, so a reader can take byte
ranges. A query copy is written once like a dated file. A missing one is uploaded, which is how
the versions published before the profile get theirs on the first deploy after it; a later
profile writes beside them (`<version>.p<N>.parquet`), and an edit to `sort`, `lookup` or
`int32` reaches the copies of versions that have none yet. The gate wants every table version's
query copy in the tree or among the files a cached build left out (`dist-push --expect`). The edge caches nothing over 512 MB
and, until it learns a file is too large, answers a byte range with the whole file, so a dated
file over 500 MB is redirected to its URL with `?edge=bypass`; a zone Cache Rule placed after
"Dated version trees" bypasses the cache for that query, and without it large files fall back to
the edge's behaviour. A version's page comes from R2 as well,
with the site's security headers from `/static/page-headers.json` and a five-minute cache,
because it says whether it is the newest. Pull-request previews skip
`--versioned`, because they never write to R2.

A version built once is not built again while its source, its register entry and the code that
shapes its rows are unchanged. The deploy keeps a build cache in R2, under `_build/` in
`publicdata-raw` (`publicdata cache pull|push`), that holds for each version only its small
files: the manifest, the schema and the SQL. Its other files were pushed to `publicdata-dist` by
the deploy that built them, so a cached build lists them in `absent.json` instead of writing them,
the gate counts them as present, and `dist-push --expect` stops the deploy if R2 lacks any of
them. The Parquet a diff, the history archive or a page reads is read back from
`publicdata-dist` when needed (`build --published`). The build never reads a data.sqlite: its
figures, query console and D1 load query the Parquet through DuckDB (`records.connect`), as a
view shaped like the SQLite file's records table, with SQLite's tie order, NaN read as null and
parameters compared as SQLite's column affinity would (`Records.param`). Its rowid is the row's
place in the Parquet, and floats are summed in that order as SQLite sums them, so the pages come
out as they did from SQLite for an entry in the publisher's order; for a sorted entry the order is
the sorted one, and a float total can differ from data.sqlite's in its last digit. The cache key splits in two: everything the build imports except
the format writers (`cache.environment_key`) names the entry, and each writer under
`serialise/writers/` has a key of its own (`cache.writer_key`), recorded in the entry per format.
A writer added or changed does not invalidate an entry: the build reads the version's rows back
from the cached Parquet, the way the diff does, writes only the files whose writer the entry has
not seen, and records them. A sorted version's Parquet no longer holds the source order, so its
cache entry keeps that order beside the record (`order.parquet`), with the SHA-256 and size of the
Parquet it belongs to. The build puts the rows back in it before it writes a format that keeps the
publisher's order, and only when the Parquet it read back is that very file and carries the
profile key and its sorting columns; otherwise the version is built again from its source. The
query copies are outside the cache: a cached version's copy is listed as already published. A changed Parquet writer, a change to the Parquet profile, or a change to the JSON and
GeoJSON writers that also make the partition files, rebuilds the version from its source. CAPS_PLACEHOLDER The
determinism job proves this by building the fixtures with a subset of formats into a cache and
then with every format, and comparing the result with a plain build (`build --formats`). The cache is saved only after the R2 push succeeds, each entry's record after its files,
and the last push of a deploy to main notes the entries its build pruned in `_build/.unused.json`.
An entry is deleted, its record first, only once it has stayed unused for a day, so a preview
that listed it before main stopped using it still finds it whole. Source bytes are
pulled only for versions the cache does not hold. A deploy dispatched with `replace` builds
without it.

One runner's disk cannot hold a build of every version at once, so the deploy builds in shards.
A plan job lists the versions the cache cannot serve, those with no entry and those a writer
would grow (`publicdata shards`), and packs their datasets by source bytes into at most four
shard jobs; a dataset larger than a share builds alone, and a few small changes build in one job.
Each shard pulls only the cache entries its datasets key to (`cache pull --only`), builds its datasets without pages (`build --no-site`, `gate --versions-only`), pushes their dated files to R2 (`dist-push --dated-only`) and
hands over the cache entries it wrote, with the version files a preview serves. The deploy job
then builds the whole site from the cache those entries filled, links the preview files in with
`build --built`, and pushes the pages. Every deploy is therefore limited by its largest single
dataset, not by the sum of them. A replace dispatch plans every dataset, and purges the versions it rewrote from the edge cache (`publicdata purge`), which otherwise serves a dated file as immutable for a year; it needs the `CLOUDFLARE_PURGE_TOKEN` secret, with Zone Read and Cache Purge on the zone. `publicdata.com.au` and `publicdata.net.au` redirect here.

## Query API

Everything the site answers dynamically is under `/api/v1/`. `/api/v1/datasets/<slug>/rows` and
`/aggregate` answer from D1 for the newest loaded version, the same under
`/api/v1/datasets/<slug>/versions/<date>/` for a dated version, and `/api/v1/datasets/<slug>/versions`
lists what is loaded; `/api/v1/datasets?q=` searches the datasets served here, votes are `/api/v1/votes`, the catalogue search `/api/v1/catalogue` and requests `/api/v1/requests`. The deploy loads the latest
version of each live dataset from that version's own data.parquet, typed as its data.sqlite and
in the Parquet's row order (`publicdata d1 sql`), one table
per version with indexes on the key and partition fields, and records it in `_versions` with its
field list, licence and attribution, and in `_orders` with the order its rows were taken in
(`profile.signature`); a loaded version whose Parquet is in another order is loaded again, so its
rowid agrees with the Parquet and the console. At most two versions per dataset are loaded; every version
stays available as files. A version whose data.sqlite is over 500 MB, or a dataset whose entry sets
`query: false`, is not loaded, and its page, OpenAPI and MCP resources leave the query API out;
`d1.queryable` is the one rule both the build and the loader read. Every other version is for the
Parquet engine, which reads the version's query copy in `publicdata-dist`, never its data.parquet. Up to four versions load at
once, each one's parts in order. Filters follow PostgREST (`field=gte.2020`, `in.(a,b)`, `is.null`,
`like.*x*`, `not.` to negate), every name is checked against the field list and every value is
bound. Paging asks for one row more than the limit and returns a `next` URL on the version's own
path. JSON responses carry the version, licence and attribution; CSV and NDJSON carry them in
headers. Dated answers are cached at the edge for good, the newest for five minutes. Above 60
requests per 10 seconds from one address the zone answers 429 with `Retry-After`,
`RateLimit-Policy` and a JSON body; every API answer carries the same policy header.

It stays off until the D1 database exists, is bound as `DB` in wrangler.toml, the repository
variable `D1_ENABLED` is true, and `QUERY_API` in site.py is flipped so OpenAPI lists it. Until
then the endpoints answer 503 and point to the files. The query builder is tested against
node:sqlite in CI.

## Catalogue

`publicdata catalogue fetch` reads the dataset list of every government open-data portal:
data.gov.au, the NSW, Victorian, Queensland, SA, WA and NT portals and the Department of
Infrastructure's catalogue over CKAN, the ACT portal over the Socrata catalogue API, and the ABS
dataflows over SDMX. Tasmania has no portal; its agencies publish on data.gov.au. Each record is
re-keyed onto one shape (id, portal, title, organisation, licence as the portal states it and as
an SPDX-style id, formats, created and modified dates, landing URL, a 280-character summary) and
nothing is scored. A record data.gov.au copied from a portal read here directly is dropped so the
original is the only copy; records it harvested from elsewhere, such as the ocean and geoscience
catalogues, are kept and name their origin.

Councils that run a portal of their own are read directly: Opendatasoft portals through the
explore API's catalogue and ArcGIS Hub sites through the OGC Records search of the site's own
catalogue. The list in `catalogue.py` holds only portals that add datasets no other portal lists
in full. Each council portal is one publisher, named in that list and placed under its state as a
council. A portal names the organisations on other portals that copy it, and a record there is
dropped when the council portal lists the same title or the record's source URL is on the council
portal. Anything else such an organisation holds is kept, because data.gov.au often harvests a
council's map services as well.

Record ids are the portal code plus the portal's own stable id (CKAN package UUID, Socrata
four-by-four, ABS dataflow id, Opendatasoft dataset uid, ArcGIS item id), so they survive renames and serve as vote keys. The snapshot is
one gzipped NDJSON file sorted by id and stored like a source at `store/catalogue/<date>/`; the
weekly harvest, a workflow of its own so a slow portal never holds up a dataset, writes a new version only when the bytes change, and builds pull only the newest. A portal that cannot be read keeps
its records from the latest snapshot and the snapshot's stats name it and the date the records
come from; the government and browse pages say so. The first harvest has nothing to carry, so
such a portal is marked as not read.

## Directory

Every government and every publisher has a page: `/browse/`, `/<jurisdiction>/` for the nine
governments, and `/<jurisdiction>/<publisher>/`, so each tier of a dataset's breadcrumb resolves.
A publisher page lists the datasets its organisation lists on the portals, newest first, with the
licence and formats the portal states, links to the portal, and a vote button on each record
that has an open licence and a file or API we can read. A catalogue record never gets a page of
its own; it gets a dataset page when it is serialised. Publisher pages with fewer than three
records and nothing served carry `noindex` and stay out of the sitemap.

Each catalogue organisation is one publisher, named as its portal names it, in its portal's
jurisdiction. `register/publishers/<jurisdiction>.yaml` overrides that where the portal cannot
say: a council or state body publishing through data.gov.au, one agency listed on several
portals, a federal agency listed on a state portal, and bodies that are not government at all.
`publicdata catalogue publishers` proposes entries for organisations nothing claims yet, using
name prefixes, place names and the ABS council-area list; proposals are reviewed and committed by
hand, never applied at build time. `/catalogue/publishers.json` and each publisher's
`catalogue.json` carry the same lists as data. `/catalogue/votable/<portal>-<c>.json` shards the
votable record ids so the vote endpoint reads one small file per check.

## Raw store

`store/<slug>/<version>/manifest.json` is committed. The bytes beside it (`source.<ext>`) are
not; they live in the R2 bucket `publicdata-raw` under the same path and are pulled by hash
before every build (`publicdata store pull`). No built tree holds them: the `/d/` function serves
a version's `source.<ext>` from `publicdata-raw`, only once that version's manifest is published,
and `dist-push` stops the deploy if a published version's file is missing there. A committed manifest with no matching object is a
build failure, never a silent skip. A download that is empty, or an HTML page where a data file should be, is
refused and reported; it never becomes a version. One dataset that cannot be fetched does not
stop the others, and the fetch workflow ends red when anything failed. The scheduled fetch workflow pushes new bytes and opens one
pull request per government with its new manifests, so a source that fails its checks holds back
only its own government's; merging a PR is what publishes its versions. A changed file dated on a
day an earlier version already holds takes the fetch date, or the next free day, and its manifest
notes say so. A source whose file host turns automated clients away is marked `manual`: the fetch
reads its portal record, lists a changed file in the **Manual downloads due** issue, and takes the
bytes from a file a person downloaded (`fetch --file`).

## Votes and requests

The backlog is the whole catalogue. The build writes one search row per listed record
(`directory.search_rows`) to a SQLite file, and the deploy loads it into D1 as the `_catalogue`
table with an FTS5 index over title, summary and publisher, once per harvest.
`/api/v1/catalogue` searches it, and the backlog page and the home page's most-wanted list read
it. A record the register has already planned votes under the register slug, so its votes and
the entry's are one count. Votes are one object per browser per day per dataset in the private
R2 bucket `publicdata-votes`, keyed by a salted hash of address and user agent, and a count is a
prefix listing. `/api/v1/votes` reads one rollup object, `_counts.json` in the same bucket, which is recounted from the vote objects once it is a minute old. `/catalogue/aliases.json` maps each record id a register entry has claimed to the entry's slug, so votes cast under the id before the entry existed count for it, and a new vote under the id is stored under the slug. The salt is created on first use and never leaves the bucket. "Have a link?"
resolves a pasted portal URL against the catalogue (`locate()` in directory.py and
functions/_catalogue.js, held together by tests) and casts a vote for the record it names. A URL
it does not hold is not stored; the answer points to National Digital's contact form. Both endpoints keep the query API's fair-use limit per address in the edge cache
(`functions/_limit.js`), as the MCP server does, because the zone's rule covers
`/api/v1/datasets/*` only.

## Site-wide files

A file that lists every dataset grows with the register, so the large ones are kept to a fixed
size or split. The site's `openapi.json` has one rows path with the slug as an enum, and each
dataset's own `/d/<slug>/openapi.json` names its fields as filters. `sitemap.xml` is a sitemap
index over `/sitemaps/<jurisdiction>.xml` and `/sitemaps/site.xml`. `search_datasets` asks
`/api/v1/datasets`, which searches the `_served` table in D1: one row per live dataset with a
full-text index over title, summary, publisher, keywords and field names. The build writes it
into the catalogue index file with a version that is a hash of its rows, so it loads again when
the register changes rather than once per harvest. Without it the endpoint reads `catalog.json`.

## Hubs

`publicdata hubs` copies each served dataset's newest version to Hugging Face, Zenodo and Kaggle,
and the Hubs workflow runs it after each production deploy and weekly. It reads the live
`catalog.json`, so a dataset or version the site serves reaches the hubs with no code change. Each
hub is asked which versions it holds first (Hugging Face by its `v<date>` tags, Zenodo by the
record versions whose `isVersionOf` is the dataset page, Kaggle by the `publicdata.json` in the
dataset), and only a newer version is uploaded. Every copy carries `publicdata.json` (version,
licence, attribution, the publisher's file hash) and text that links the version URL, so the link
back survives a re-upload. A licence with no hub mapping in `hubs.LICENCES` is refused, a licence with a
condition of use is refused since no hub can state it, a database is refused since the hubs take
one table, and an Australian port goes up as "other" with the licence named in the text where a
hub has no id for it. Zenodo gets a DOI per version under one concept DOI, and relates each record to the version
URL, the dataset page and the publisher's page. `--render <dir>` writes what each hub would
receive, without the data, for review.

Kaggle copies carry everything Kaggle's usability rating counts: tags, update frequency, sources,
file and column descriptions, a cover image cut from the social card and a public starter
notebook. Kaggle scores a page from its newest version, so settings are sent before each upload
and again after it. `--refresh` re-applies cards, settings and notebooks to versions a hub already
holds.

Each run records where every copy is (`--record store/hubs.json`): the Hugging Face repository,
the Kaggle dataset and the Zenodo concept DOI, and each hub account. On main a changed record goes
up as a `data(hubs)` pull request that `data-pr.yml` approves and that merges itself once its
checks are green, like the fetch's. The build
reads the committed record: a dataset page lists its copies in an "Also published on" card, its
Dataset JSON-LD adds them to `sameAs` and gives the DOI as an `identifier`, and the home page's
operator names the hub accounts in `sameAs`.

## Analytics

Cloudflare Web Analytics, loaded first-party from Cloudflare's beacon, with no cookies and no
other tracking. The Content-Security-Policy in `_headers` names it and nothing else.

## Phases

0. Bootstrap: register schema, raw store, `ckan-resource` adapter, normaliser, JSON and CSV,
   gate, deploy. Done when one Commonwealth CSV rebuilds deterministically with one version
   per source release.
1. Seed set of six to eight datasets exercising every adapter and format.
2. Request bank: public page, issue-form intake, agency proposal pages.
3. Approach the publishers behind the seed set.
4. Query and agents, on demand.
