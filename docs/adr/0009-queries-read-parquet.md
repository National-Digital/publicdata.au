# 0009: The MCP row tools reach every version through Parquet; D1 keeps the newest

- Status: **Accepted, one part open** (#52; versions stored as parts: #108; the query API's own
  path to older versions is open)
- Date: 2026-10-06
- Deciders: National Digital
- Relates to: [0006](0006-period-partitions.md), [0008](0008-parquet-is-the-base-format.md),
  [0010](0010-mcp-answer-layer.md)

## Context

The query API and the MCP tools `query_rows` and `count_rows` answered from D1 alone. The deploy
loads at most the two newest versions of each dataset into D1, none over 500 MB of CSV and none
whose entry sets `query: false`, and D1 bills every row and index entry written. Every other
version could only be downloaded. An agent asking how a figure changed between releases needs
those versions, which is what the archive exists for.

On files written under the profile, the October 2026 benchmark
([0008](0008-parquet-is-the-base-format.md)) measured 204 ms to 1.1 s of wall time for the filtered
queries and counts it ran, mostly in R2 reads. D1 answers from an index in one round trip.

## Decision

- D1 keeps the newest versions and stays their query path, for the query API and the MCP tools.
- The MCP row tools answer every other version from its Parquet in R2: older versions, versions
  too large for D1 and datasets with `query: false`.
- The Parquet engine reads a version's query copy, or its published `data.parquet` when that file
  follows the profile. A version stored as parts alone is read as the list of its parts, and a
  filter on the period field rules out parts by their labels.
- Before reading any data, the engine prices a query from the footer and the page index against a
  fixed budget per call. A query over the budget is answered with the DuckDB SQL that returns the
  same rows in the same order from the version's public Parquet, so an agent with code execution
  can carry on.
- Every answer names the version, the licence and the attribution.

**Open.** The query API (`/api/v1`) answers only the versions D1 holds. Whether it gains the Parquet
path, and whether D1 later stops holding dataset tables, are left to a later record, made with both
paths measured under the same mix of queries.

## Alternatives considered

- **Load every version into D1.** Each load writes every row and index entry, which D1 bills, and a
  deploy plans at most 10 million rows written. Parquet answers need no load step.
- **Parquet for every version, D1 for none.** The newest versions are the ones most queries ask
  about, and D1 answers them in one round trip. Keeping D1 for them keeps that speed while the
  Parquet path proves itself.

## Consequences

- The October 2026 benchmark on profile files met the target of under 500 ms of CPU for the filtered
  crash, water and company-number queries: 349, 213 and 299 ms, with 13, 6 and 8 merged reads. A
  filter on type and year over the company register still took 13.6 s, because its company-number
  order cannot serve it. Its counts now come from a rollup ([0010](0010-mcp-answer-layer.md)).
- Rows from Parquet come in the file's own order (the declared sort, then the key, then the source
  position), and rows from D1 in the publisher's. The DuckDB SQL rebuilds the file's order.
- A Parquet answer cites the `/api/v1` query that would give the same result, and that URL does not
  answer while D1 lacks the version. The answer also links the version's `data.parquet` and
  manifest. This stays until the open part is decided.
- The build writes `fields.json` for every dataset whose newest version has a `data.parquet` or is
  stored as parts, so `list_fields` answers for every dataset the row tools serve.

See `docs/ARCHITECTURE.md` ("Query API").
