# 0009: Large dated tables are split into period parts

- Status: Accepted
- Date: 2026-10-10

## Context

A long daily series, such as station rainfall from 1889 to yesterday, runs to hundreds of millions
of rows. As one file per version it would be rewritten in full whenever a single day changes, it
would be too large for the explorer and for a Worker to query, and every format of it would be
stored again with each version.

Most of such a table is finished. Rows for 1950 do not change in 2026, except when a publisher
reprocesses its history, and an archive has to record that.

## Decision

- A register entry may declare `period: {field, grain}`, with the grain a year, a financial year, a
  quarter or a month. A table or a point layer can take a period. A database or a layer of lines or
  polygons cannot.
- A dated table needs a period once its newest version is over 100 MiB of Parquet or 5 million
  rows, and a feed always has one. The grain is the largest that keeps each part at or under
  100 MiB, which is what the explorer can hold in a browser and a Worker can scan within one
  request's budget. The gate enforces both.
- Each fetch records the period in its own manifest, and a version is split by the period its
  manifest names. Adding a period to an entry with history changes none of its versions.
- A version's parts are published under it at `parts/<period>.parquet` and `.csv.gz`, with rows that
  have no date in an `undated` part.
- The recent periods still open to change are the revision window, two by default. An older part is
  finished and is written once: a later snapshot with the same rows for that period points at the
  earlier version's file. A later change to a finished part is a revision. It is written as a new
  part, flagged in the manifest, and always cuts a snapshot.
- While the parts total 100 MiB of Parquet or less and 5 million rows or fewer, the version also
  carries the whole table. Past that, it is its parts and a DuckDB file that reads them as one
  table, and its manifest says `"whole": false`.

## Alternatives considered

- **One file per version.** Each snapshot of a long series would store the whole series again in
  every format.
- **Storing identical files once by content** ([0011](0011-files-stored-at-their-url-paths.md)).
  Every writer but two embeds the version's provenance in the file, so an unchanged year still
  differs by its header. Reusing a finished part is decided on its rows instead, and needs no
  lookup when a file is read.

## Consequences

- A version stored as parts alone has no query console and no pages by place, since each reads one
  whole file. The explorer stays on the newest whole version, and so do the hubs, since they take
  one table ([0018](0018-copies-on-the-hubs.md)). Its dataset page says so. The figures and sample
  rows read the parts joined outside the published tree.
- Such a version is still queryable. The MCP row tools read it as the list of its parts, and D1
  loads it from its parts while their CSVs total 500 MB or less. It gets no rollup, since a rollup
  is counted from a whole `data.parquet` ([0017](0017-how-queries-are-answered.md)).
- Parts keep the Parquet layout their fetch recorded and get no query copy, so a later change to an
  entry's `sort`, `lookup` or `int32` reaches only versions fetched after it.
- A diff between snapshots reads the parts back and compares whole tables. Skipping parts with the
  same hash is not built.
- Shards split the build by dataset, so one large dataset's parts are built in one shard.
- In October 2026 two entries are split, the eucalypt records and the water storage levels, both
  by year, and no large table is waiting to be split.

See `docs/ARCHITECTURE.md` ("Periods", "URL contract", "Archive").
