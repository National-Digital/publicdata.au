# 0007: The formats each version carries, and their size caps

- Status: **Accepted** (#54, merged into #48's branch and landed with it)
- Date: 2026-10-06
- Deciders: National Digital
- Relates to: [0008](0008-parquet-is-the-base-format.md), [0011](0011-text-stored-compressed.md)

## Context

Each version carried every format: JSON, NDJSON, CSV, CSV (gzip), Parquet, SQLite, DuckDB, Excel
and Arrow, plus GeoJSON and GeoPackage for a table with coordinates. A measurement of the live
version files on 6 October 2026 found 71.6 GB, of which NDJSON was 16.3 GB, JSON 10.8, GeoJSON 9.2,
the publisher's files 7.4, SQLite 6.7, CSV 6.3, DuckDB 4.7, GeoPackage 4.2, Arrow 1.8, Parquet 1.7,
Excel 1.2 and CSV (gzip) 1.1. The version files were about 9.7 times the publisher's files among
them.

The formats are why people use the site, so the set stays broad. Some formats stop being usable
above a size, though: Excel stops at 1,048,576 rows, and a JSON array or GeoJSON file of gigabytes
cannot be opened by the tools that want those formats.

## Decision

Every table version carries Parquet, CSV, CSV (gzip), NDJSON and DuckDB, with its manifest, its
schema, its checksums and the publisher's file. A table with coordinates adds GeoParquet and a
GeoPackage, and a layer of lines or polygons adds a GeoPackage and PMTiles. A version split into
parts alone carries what [0006](0006-period-partitions.md) gives it.

Four formats are written only within a cap:

| Format | Cap |
|---|---|
| SQLite | 500 MB of CSV |
| GeoJSON | 100 MB, measured on the written file |
| JSON array | 50 MB of NDJSON |
| Excel | 50 MB of CSV and 1,048,575 rows plus the header |

Each cap is measured on a file the build has already written, so two builds make the same choice.
A format left out is named on the version and dataset pages with the reason. Arrow is no longer
written, because Parquet holds the same table and every Arrow reader reads Parquet.

A version's set of formats is fixed when it is first built. The caps apply to versions whose fetch
stamped `caps` in the manifest, and a version without the stamp keeps the set it was built with,
Arrow and the old row limits included.

## Alternatives considered

- **Every format at every size**, as before. Above its cap a format cannot be used for what people
  choose it for, so storing it costs money and helps no one.

## Consequences

- Versions published before the caps keep every file they have
  ([0002](0002-versions-are-kept.md)).
- The manifest records the byte counts the caps were measured on and each format left out, and the
  gate refuses a version whose files disagree with them.
- The Python and R clients read Parquet and fall back to CSV (gzip), which every version has.
- A table's page shows which formats exist for each version, so a missing format is never a broken
  link.
- Extracts by place or period, made from Parquet on request, were proposed with the caps. They are
  not built, and every format a version carries is a stored file.
- No page states the size of a DuckDB file, because its bytes differ from one write to the next
  ([0018](0018-deterministic-builds.md)).

See `docs/ARCHITECTURE.md` ("What every dataset gets", "URL contract").
