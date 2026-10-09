# 0010: Counts are answered from per-version rollups first

- Status: **Accepted** (#55; every version of a `query: false` entry and the register's `rollup`
  field: #120)
- Date: 2026-10-06
- Deciders: National Digital
- Relates to: [0008](0008-parquet-is-the-base-format.md), [0009](0009-queries-read-parquet.md)

## Context

Most of what an agent asks is a count or a total by place, period or category. Reading a version's
rows to answer one costs many reads even when the file is well sorted, and a count over a field the
file is not sorted by can cost more than the Parquet engine's budget allows
([0009](0009-queries-read-parquet.md)). A small table of precomputed counts answers it in one read.

In October 2026 rollups were built over the newest version of all 361 live tables, stored as gzipped
JSON. With a 1 MB cap and four measures, the 126 tables with at least 5,000 rows had rollups of
31.4 MB in all, 5.3% of their Parquet, with a median of 152 KB. They held every field named by
84.5% of the register's example and chart questions, 98.2% of counts by one field and 78% of counts
by one field filtered on another. In workerd a cold rollup answered in 6 to 25 ms and a warm one in
about 1 ms.

## Decision

- A rollup holds one version's counts and totals grouped several ways ("cubes"). `count_rows` reads
  it before any other engine, and falls through to Parquet or D1 when no cube holds the query.
- A version gets a rollup when its table has at least 5,000 rows, for the versions D1 holds and for
  every version of an entry with `query: false`, which D1 never holds.
- Cubes are chosen by the weight of questions each answers per byte, with the register's own
  questions (`rollup`, `example` and `chart`) weighing most, until the gzipped rollup reaches 1 MB.
  Each cube totals up to four measures as sum, non-null count, minimum and maximum, so counts, sums,
  averages, minima and maxima all come from it.
- A rollup is counted from its version's published `data.parquet` and answers only while it is tied
  to that file's bytes. It follows SQLite's rules for filters, nulls, LIKE and ordering, so its
  answer is the one D1 gives.
- Rollups are stored in the published bucket under `_rollup/`, outside the published tree. No route
  serves them, and each carries the version's provenance.
- When a page of rows is over the Parquet budget only because counting every match reads too much,
  `query_rows` takes the count from the rollup and reads only until the page is full.

## Alternatives considered

- **One grouping over every chosen field.** It reached only 58% of the register's questions,
  because each field multiplies the number of groups.
- **A 2 MB cap.** Doubling the cap gained about three points on filtered counts and doubled the
  parse time.
- **Counts alone.** They answer the same fields in a third of the bytes, but no sums or averages,
  and of 295 example questions in the register 134 ask for a sum and 42 for an average.

## Consequences

- The build never imports the rollup module, so rollups shape no version's files and adding them
  rebuilt nothing. `publicdata rollup` runs on main after the D1 load, and only where D1 is enabled.
- A sum of a decimal field can differ from D1 in the last digit or two, because the rollup adds
  per-group sums. Counts, integer sums, minima and maxima are exact.
- A version stored as parts alone gets no rollup, since it has no whole `data.parquet`.
- A rollup answer for a version D1 does not hold cites an `/aggregate` URL that does not answer, as
  in [0009](0009-queries-read-parquet.md), and links the version's file and manifest.
- An answer shape that leads with the count and a short sample, and a log of query shapes to choose
  sort keys and cubes from, were proposed with this record. Neither is built. A query log would
  change what [0015](0015-mcp-server-and-agent-surface.md) says the server keeps, so it needs a
  record of its own.

See `docs/ARCHITECTURE.md` ("Rollups", "Query API").
