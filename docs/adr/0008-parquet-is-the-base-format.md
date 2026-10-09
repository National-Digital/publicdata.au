# 0008: Parquet is the format everything reads

- Status: **Accepted** (#56, merged into #48's branch and landed with it; query copies read by the
  MCP tools: #52; a layout edit rewrites them: #94)
- Date: 2026-10-06
- Deciders: National Digital
- Relates to: [0007](0007-formats-each-version-carries.md), [0009](0009-queries-read-parquet.md),
  [0010](0010-mcp-answer-layer.md)

## Context

The site's own readers used different files. The figures, the query console and the D1 loader read
each version's SQLite file, the explorer and the hubs read Parquet, and DuckDB attached its own
file. The build cache had to keep both Parquet and SQLite for every version: 1.7 GB of Parquet and
6.7 GB of SQLite across the live versions.

The MCP server is the consumer this design is built for: an AI agent querying through it. It needs
to answer from any version, past or present, and a Worker can only do that by reading part of a
file.

A benchmark on a deployed Worker in October 2026 (hyparquet with zstd, CPU time from the Worker's
own logs, the median of three runs) measured what makes a partial read cheap (#56):

- Sort order mattered most. A filter on type and registration year over the company register took
  15.3 s of CPU and 20.4 s of wall time on the file as published, and 727 ms once sorted by
  registration date. A place and year filter over crash rows fell from 1,522 ms to 349 ms of CPU
  under the profile.
- A sort that suits one filter can hurt another. Sorting the company register by registration date
  made a lookup by company number rise from 299 ms to 2,563 ms.
- Wall time was mostly R2 reads made one after another. With 65,536-row groups a single count over
  the water series took 100 reads, and 14 under the profile.
- INT64 columns decode to BigInt in JavaScript.

## Decision

Parquet is the one format the site reads, and every other read path is built from it. The figures,
the query console, the D1 loader, the explorer, the hubs and the rollups read a version's Parquet,
and the MCP tools read it for every version D1 does not hold. SQLite remains a download format.

Every table's `data.parquet`, a layer's GeoParquet and a database's tables follow one profile, and
say so in the footer key `publicdata.profile` beside the provenance key `publicdata`.

- **Order.** An entry may declare `sort:`. Without it, rows keep the publisher's order, which is
  often already grouped on the main filter. With it, rows are sorted by those fields, then the key,
  then their position in the source, so the order is complete and the publisher's order survives
  within each group. The sort applies to Parquet and what is made from it. CSV, Excel, JSON and the
  publisher's file keep the source order, because people compare our CSV with the source line by
  line.
- **Lookups.** An entry may declare `lookup:` fields, which get bloom filters, so an equality lookup
  stays cheap when the sort serves another filter. No reader in this repository uses them yet. They
  serve external readers such as DuckDB.
- **Types.** An integer field is written as INT32 when the entry declares it under `int32:`, in
  every version, so a field keeps one type across versions and parts.
- **Encoding and sizes.** zstd, dictionary encoding and column statistics on every file, and a page
  index on sorted files only. Row groups of 500,000 rows and pages of at most 10,000 rows or 8 MiB:
  1,000,000-row groups overflowed the reader's stack, and 10,000-row pages used 10 to 30% less CPU
  on point lookups than 20,000-row pages.

A version's own files keep the layout its fetch recorded in its manifest, under
[0002](0002-versions-are-kept.md). So that the versions published before the profile, and those
published under an older layout, can still be read in part, every table version also has a query
copy under the current profile and the entry's current layout, at `_q/<slug>/<version>.parquet` in
the published bucket. No route serves it and nothing links it. Answers and DuckDB SQL name the
public `data.parquet`.

## Alternatives considered

- **Sort every table on its main filter.** The benchmark showed a sort that suits one filter slows
  another, and many publishers' files are already grouped on the filter that matters. Sorting only
  where an entry asks avoids that.
- **A page index on every file.** On an unsorted file it saves no work and multiplies the reads.
- **Rewriting each published `data.parquet` under the profile.** That would change the bytes of
  cited files outside the reasons [0002](0002-versions-are-kept.md) allows. A query copy gives the
  same reach without touching them.

## Consequences

- One read format means one place to get provenance right. The build cache keeps no Parquet and
  reads it back from the published bucket ([0014](0014-build-cache-in-r2.md)).
- Query copies add about the size of the Parquet set, 1.7 GB in October 2026. A copy is rewritten
  in place when an entry's `sort`, `lookup` or `int32` changes, so a reader keys its caches on the
  object's ETag. A new profile writes its copies beside the old ones.
- A re-sort never makes a new version, because `rows_sha256` hashes each row and sorts the hashes.
- Versions stored as parts alone get no query copy ([0006](0006-period-partitions.md)).
- In October 2026 three entries declare `sort:` and none declares `lookup:`.

See `docs/ARCHITECTURE.md` ("Parquet profile", "Hosting").
