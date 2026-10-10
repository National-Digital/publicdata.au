# 0017: The query API and the MCP tools share one query path, and every version can be queried

- Status: Accepted
- Date: 2026-10-10

## Context

People and agents ask about any version, since how a figure changed between releases is what the
archive is for. Many of the questions are counts or totals by place, period or category. A reader
who gets one answer from the query API and another from an MCP tool, or a different answer once a
version ages out of a store, cannot trust either.

D1 answers from an index in one round trip, but it bills every row and index entry written. The
deploy loads at most the two newest versions of each dataset into D1, none over 500 MB of CSV and
none whose entry sets `query: false`, and plans at most 10 million rows written a deploy.

A Worker can instead read part of a version's Parquet in R2
([0008](0008-parquet-is-the-base-format.md)). On files written under the profile, the October 2026
benchmark measured 204 ms to 1.1 s of wall time for the filtered queries and counts it ran, mostly
in R2 reads. A count over a field the file is not sorted by can cost more than any one call should.

A small table of precomputed counts answers most such questions in one read. In October 2026
rollups were built over the newest version of all 361 live tables, stored as gzipped JSON. With a
1 MB cap and four measures, the 126 tables with at least 5,000 rows had rollups of 31.4 MB in all,
5.3% of their Parquet, with a median of 152 KB. They held every field named by 84.5% of the
register's example and chart questions, 98.2% of counts by one field and 78% of counts by one field
filtered on another. In workerd a cold rollup answered in 6 to 25 ms and a warm one in about 1 ms.

## Decision

- **One path.** Every version a reader can download can be queried, and the query API (`/api/v1`)
  and the MCP tools answer it through the same function, so a query gets the same answer from
  either. The MCP tools wrap that answer as structured content. Without a version, a query answers
  from the newest live version.
- **Rollups first, for exact answers.** A rollup holds one version's counts and totals grouped
  several ways ("cubes"), and the path reads it before any other engine. It answers only what it
  gets exactly right: counts, sums of integers, minima and maxima. A sum or average of a decimal
  field falls through. A version gets a rollup when its table has at least 5,000 rows, for the
  versions D1 holds and for every version of an entry with `query: false`. Cubes are chosen by the
  weight of questions each answers per byte, with the register's own questions (`rollup`,
  `example` and `chart`) weighing most, until the gzipped rollup reaches 1 MB.
- A rollup is counted from its version's published `data.parquet` and answers only while it is tied
  to that file's bytes. It follows SQLite's rules for filters, nulls, LIKE and ordering, so its
  answer is the one D1 gives. Rollups are stored under `_rollup/` in the published bucket, outside
  the published tree, and no route serves them. Each carries the version's provenance.
- **D1 for the newest versions.** D1 answers the versions it holds in one round trip. It is there
  for speed, and what can be queried does not depend on it.
- **Parquet for every other version.** Older versions, versions too large for D1 and datasets with
  `query: false` are read from Parquet in R2: a version's query copy, or its `data.parquet` when
  that file follows the profile, and a version stored as parts alone as the list of its parts.
  Before reading any data the engine prices the query from the footer and the page index against
  a fixed budget per call. A query over the budget is refused with the DuckDB SQL that returns the
  same rows in the same order from the version's public Parquet.
- **The engine is invisible.** Rows without an explicit order come in the query copy's order (the
  declared sort, then the key, then the source position) from every engine, and ties under an
  explicit order are settled the same way. When a page of rows is over the budget only because
  counting every match reads too much, the count comes from the rollup.
- Every answer names the version, the licence and the attribution, and the query URL it cites
  answers.

## Alternatives considered

- **D1 alone for the query API, Parquet for the MCP tools only.** The two would answer different
  sets of versions, an MCP answer about an older version would cite an API URL that did not answer,
  and a version would change engine and row order when it aged out of D1.
- **Load every version into D1.** Each load writes every row and index entry, which D1 bills.
  Parquet answers need no load step.
- **Parquet for every version, D1 for none.** D1 answers filters a file's sort cannot serve within
  the budget, and answers the newest versions in one round trip. With one path, dropping it later
  is a change inside that path.
- **One grouping over every chosen field.** It reached only 58% of the register's questions,
  because each field multiplies the number of groups.
- **A 2 MB rollup cap.** Doubling the cap gained about three points on filtered counts and doubled
  the parse time.
- **Counts alone.** They answer the same fields in a third of the bytes, but no sums or averages,
  and of 295 example questions in the register 134 ask for a sum and 42 for an average.

## Consequences

- On profile files the filtered crash, water and company-number queries took 349, 213 and 299 ms of
  CPU, under the target of 500 ms. A filter on type and year over the company register still took
  13.6 s from Parquet, because its company-number order cannot serve it. Its counts come from a
  rollup, and D1 answers its newest versions.
- An unauthenticated caller can make the API read any version's Parquet. The per-call budget and the
  per-address rate limit bound what one call and one address can cost.
- The build never imports the rollup module, so rollups shape no version's files.
  `publicdata rollup` runs on main after the D1 load, and only where D1 is enabled.
- A version stored as parts alone gets no rollup, since it has no whole `data.parquet`.
- The build writes `fields.json` for every dataset whose newest version has a `data.parquet` or is
  stored as parts, so `list_fields` answers for every dataset the path serves.
- D1 stays until its cost presses on the budget in [0012](0012-storage-budget.md) or a measurement
  shows Parquet answering the newest versions within about twice D1's time on the same queries.
  Either reopens this record.

See `docs/ARCHITECTURE.md` ("Query API", "Rollups").
