# 0011: URLs stay fixed while text is stored compressed; storing identical files once is deferred

- Status: **Accepted** (#51; storing identical files once measured and deferred: #121, #130)
- Date: 2026-10-06
- Deciders: National Digital
- Relates to: [0006](0006-period-partitions.md), [0007](0007-formats-each-version-carries.md)

## Context

Version files were stored in R2 at the key that matches their URL, one object per file, with text
formats uncompressed. Of 71.6 GB of live version files, NDJSON, JSON, GeoJSON and CSV held 42.6 GB,
and those formats compressed 8 to 19 times at gzip level 6 (#51).

The URL contract (`/d/<slug>/v/<date>/data.<ext>`) is cited and must keep working
([0019](0019-contract-only-grows.md)).

## Decision

- Storage keys stay equal to URL paths, and file names in URLs stay generic. A download saves under
  a name worked out from the requested path, so it does not depend on how the file is stored.
- Dated text files of 1 KB or more are stored gzipped, with deterministic bytes. The function sends
  them with `Content-Encoding: gzip` to a client that accepts gzip and decodes them for one that
  does not, so every client receives the same bytes as before.
- Formats that readers fetch by byte range stay as written: Parquet, DuckDB, SQLite, Excel,
  GeoPackage, PMTiles, `data.csv.gz` and the publisher's file. So do pages and the query copies
  under `_q/`.
- A version's `data.csv.gz` is served from its gzipped CSV, so no second copy is stored.
- Objects stored before this were rewritten in place, each checked by hash before and after, so the
  bytes a user receives did not change.

### Deferred: storing identical files once

Storing each distinct file once, with version paths resolving to it (issue #68), was proposed with
this record and is deferred.

A scan of `publicdata-dist` on 8 October 2026 (`publicdata r2 shared-report`, before the rewrite to
gzip) found 114 dated files that repeated another file's bytes, 914,622 bytes in all, against
90.32 GB of dated files: about 0.001%. The report then counted a file shared by two datasets more
than once, so the figure is an upper bound (#130). Every writer except CSV and `schema.json` embeds
the version's provenance header, so a file whose rows did not change between versions still
differs in its bytes, and storing by content cannot recover that. Period parts
([0006](0006-period-partitions.md)) reuse an unchanged part by pointing the new manifest at the
earlier version's file, which covers the large case with no lookup on each request.

The report is to be run again once period parts have run for a quarter. The design is worth
building only if it then shows a saving of several gigabytes, enough to pay for the extra R2 read
each request for a shared file would need. When it is built, it has to:

- store each object at `o/<sha256>`, keyed by its stored bytes or by its decoded bytes with the
  encoding recorded, and refer a version path to it only when that hash is already held, so only
  shared files cost the extra read;
- write each object with R2's SHA-256 check and only when absent, so a retried or concurrent push
  never overwrites one;
- follow the reference everywhere the function and the pipeline read R2 today, including the
  `data.csv.gz` alias, ranges on gzipped files and the edge cache's size test, and fail
  `dist-push --expect` on a reference with no object;
- keep an object while any version refers to it, so a takedown removes a reference and deletes the
  object only when no other version refers to it.

## Alternatives considered

- **Storage keys that differ from URL paths**, with manifests mapping each path to an object. It
  would allow storing by content now, but every request would need a second read, and the bucket
  would no longer be browsable by path. Keeping keys equal to paths keeps a request to one read.

## Consequences

- A gzipped text file cannot be fetched by byte range. A range asked of one is answered with the
  whole file and `Accept-Ranges: none`, and readers that seek should use Parquet or `data.csv.gz`.
- A deployment whose function predates #51 serves gzipped objects wrongly, so a Pages rollback past
  it, or a preview from a branch without it, is unsafe.

See `docs/ARCHITECTURE.md` ("Hosting", "URL contract").
