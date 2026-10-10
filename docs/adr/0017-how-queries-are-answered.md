# 0017: The MCP tools count from rollups first, and read D1 for the newest versions and Parquet for the rest

- Status: Accepted
- Date: 2026-10-10

## Context

Many of the questions an agent asks are counts or totals by place, period or category, and it may
ask about any version, since how a figure changed between releases is what the archive is for.

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

- **Rollups first.** A rollup holds one version's counts and totals grouped several ways ("cubes").
  `count_rows` reads it before any other engine and falls through when no cube holds the query.
  A version gets a rollup when its table has at least 5,000 rows, for the versions D1 holds and for
  every version of an entry with `query: false`. Cubes are chosen by the weight of questions each
  answers per byte, with the register's own questions (`rollup`, `example` and `chart`) weighing
  most, until the gzipped rollup reaches 1 MB. Each cube totals up to four measures as sum,
  non-null count, minimum and maximum, so counts, sums, averages, minima and maxima all come from
  it.
- A rollup is counted from its version's published `data.parquet` and answers only while it is tied
  to that file's bytes. It follows SQLite's rules for filters, nulls, LIKE and ordering, so its
  answer is the one D1 gives. Rollups are stored under `_rollup/` in the published bucket, outside
  the published tree, and no route serves them. Each carries the version's provenance.
- **D1 for the newest versions.** D1 holds the newest versions and answers them, for the query API
  and the MCP tools. The query API reads D1 alone.
- **Parquet for every other version.** The MCP row tools answer older versions, versions too large
  for D1 and datasets with `query: false` from Parquet in R2. The engine reads a version's query
  copy, or its `data.parquet` when that file follows the profile, and a version stored as parts
  alone as the list of its parts. Before reading any data it prices the query from the footer and
  the page index against a fixed budget per call. A query over the budget is answered with the
  DuckDB SQL that returns the same rows in the same order from the version's public Parquet.
- When a page of rows is over the budget only because counting every match reads too much,
  `query_rows` takes the count from the rollup and reads only until the page is full.
- Every answer names the version, the licence and the attribution.

## Alternatives considered

- **Load every version into D1.** Each load writes every row and index entry, which D1 bills.
  Parquet answers need no load step.
- **Parquet for every version, D1 for none.** The newest versions are what the query API serves,
  and D1 answers them in one round trip.
- **One grouping over every chosen field.** It reached only 58% of the register's questions,
  because each field multiplies the number of groups.
- **A 2 MB rollup cap.** Doubling the cap gained about three points on filtered counts and doubled
  the parse time.
- **Counts alone.** They answer the same fields in a third of the bytes, but no sums or averages,
  and of 295 example questions in the register 134 ask for a sum and 42 for an average.

## Consequences

- On profile files the filtered crash, water and company-number queries took 349, 213 and 299 ms of
  CPU, under the target of 500 ms. A filter on type and year over the company register still took
  13.6 s, because its company-number order cannot serve it, so its counts come from a rollup.
- Rows from Parquet come in the file's own order (the declared sort, then the key, then the source
  position), and rows from D1 in the publisher's. The DuckDB SQL rebuilds the file's order.
- A sum of a decimal field from a rollup can differ from D1 in the last digit or two, because the
  rollup adds per-group sums. Counts, integer sums, minima and maxima are exact.
- An answer from Parquet or a rollup for a version D1 does not hold cites the `/api/v1` URL that
  would give the same result, and that URL does not answer. The answer also links the version's
  `data.parquet` and manifest.
- The build never imports the rollup module, so rollups shape no version's files.
  `publicdata rollup` runs on main after the D1 load, and only where D1 is enabled.
- A version stored as parts alone gets no rollup, since it has no whole `data.parquet`.
- The build writes `fields.json` for every dataset whose newest version has a `data.parquet` or is
  stored as parts, so `list_fields` answers for every dataset the row tools serve.
- **Open: the query API.** `/api/v1` answers only the versions D1 holds. Whether it gains the
  Parquet path, and whether D1 later stops holding dataset tables, are left to a later record, made
  with both paths measured under the same mix of queries.

See `docs/ARCHITECTURE.md` ("Query API", "Rollups").
