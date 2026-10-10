# 0011: Each file is stored at its URL path, and text is stored compressed

- Status: Accepted
- Date: 2026-10-10

## Context

The URL contract (`/d/<slug>/v/<date>/data.<ext>`) is cited and must keep working
([0015](0015-contract-only-grows.md)). How a file is stored behind its URL is free to change, so
long as the bytes a client receives do not ([0002](0002-versions-are-kept.md)).

Of 71.6 GB of live version files in October 2026, NDJSON, JSON, GeoJSON and CSV held 42.6 GB, and
those formats compressed 8 to 19 times at gzip level 6.

## Decision

- Storage keys equal URL paths, one object per file, and file names in URLs stay generic. A
  download saves under a name worked out from the requested path, so it does not depend on how the
  file is stored.
- Dated text files of 1 KB or more are stored gzipped, with deterministic bytes. The function sends
  them with `Content-Encoding: gzip` to a client that accepts gzip and decodes them for one that
  does not, so every client receives the same bytes.
- Formats that readers fetch by byte range stay as written: Parquet, DuckDB, SQLite, Excel,
  GeoPackage, PMTiles, `data.csv.gz` and the publisher's file. So do pages and the query copies
  under `_q/`.
- A version's `data.csv.gz` is served from its gzipped CSV, so no second copy is stored.

## Alternatives considered

- **Storage keys that differ from URL paths**, with manifests mapping each path to an object. Every
  request would need a second read, and the bucket would no longer be browsable by path.
- **Storing each distinct file once**, with version paths resolving to it. A scan of
  `publicdata-dist` on 8 October 2026 (`publicdata r2 shared-report`) found 114 dated files that
  repeated another file's bytes, 914,622 bytes in all, against 90.32 GB of dated files: about
  0.001%, and an upper bound, since a file shared by two datasets was counted more than once. Every
  writer except CSV and `schema.json` embeds the version's provenance header, so a file whose rows
  did not change still differs in its bytes. Period parts reuse an unchanged part by pointing the
  new manifest at the earlier file ([0009](0009-period-partitions.md)), which covers the large case
  with no lookup on each request.

## Consequences

- A gzipped text file cannot be fetched by byte range. A range asked of one is answered with the
  whole file and `Accept-Ranges: none`, and readers that seek should use Parquet or `data.csv.gz`.
- A deployment whose function cannot decode gzipped objects serves them wrongly, so a Pages
  rollback to one, or a preview from a branch without that code, is unsafe.
- The shared-file report is to be run again once period parts have run for a quarter. Storing files
  once is worth a record of its own only if the report then shows a saving of several gigabytes,
  enough to pay for the extra R2 read each request for a shared file would need. Such a design
  stores each object once under its hash and refers a version path to it only when the hash is
  already held, writes objects only when absent and checked by SHA-256, follows the reference
  everywhere the function and the pipeline read R2, and deletes an object only when no version
  refers to it.

See `docs/ARCHITECTURE.md` ("Hosting", "URL contract").
