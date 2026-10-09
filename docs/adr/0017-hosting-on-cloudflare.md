# 0017: The site is hosted on Cloudflare, with its files kept as plain objects

- Status: **Accepted** (the initial public release; the build cache's bucket: #48 and #90)
- Date: 2026-10-08
- Deciders: National Digital
- Relates to: [0011](0011-text-stored-compressed.md), [0012](0012-storage-budget.md),
  [0013](0013-pages-under-the-file-cap.md), [0014](0014-build-cache-in-r2.md)

## Context

publicdata.au serves large files that people and programs download again and again, often by byte
range, and it keeps every version for good. What it costs to run grows with what it stores and
with what it sends. It is run at a nominal cost, so a host that bills for bytes sent would charge
more for a dataset the more it is used.

The site is static apart from a few dynamic paths: the version files read from storage, the
`latest/` redirects, the query API, the MCP server, votes, requests and saved dashboards.

## Decision

The site runs on Cloudflare.

- **Pages** serves the built site, with a preview deployment for each pull request from this
  repository. `_headers` sets the security headers and `_routes.json` names the paths that run a
  function.
- **Pages Functions** in `functions/` serve the dynamic paths.
- **R2** holds every file, read and written by the pipeline over the S3 API. `publicdata-dist`
  holds the published tree at keys equal to URL paths, `publicdata-raw` holds the publishers' bytes
  and is append-only, `publicdata-votes` holds votes and saved dashboards, and
  `publicdata-build-cache` holds the build cache.
- **D1** answers the query API for the newest versions and holds the catalogue search index.
- **The edge cache** holds dated files as immutable for a year and query answers by version, and
  `publicdata purge` clears a rewritten version from it.
- **Web Analytics** is the only analytics: Cloudflare's beacon, with no cookies, named alone in the
  Content-Security-Policy.

## Alternatives considered

- **A host that bills for bytes sent.** The cost of a dataset would grow with its use, which is the
  opposite of what a public archive wants. R2 charges nothing for egress, so what an entry stores
  and the rows it writes to D1 are the costs left, and [0012](0012-storage-budget.md) budgets both.

## Consequences

Leaving Cloudflare would cost the dynamic layer and keep the data.

- **Portable.** The published tree is plain files at keys equal to their URL paths, and the raw
  store is plain objects whose manifests are committed in `store/`. Both copy to any static host or
  S3-compatible store as they are. The archive can be rebuilt from the raw store and this
  repository, and the newest versions are also copied to the hubs
  ([0022](0022-copies-on-the-hubs.md)).
- **Cloudflare-specific.** The functions use Pages' routing, its R2, D1 and asset bindings and the
  Workers cache API. D1's tables would move to another SQLite host, and the query builder is already
  tested against `node:sqlite`. The zone's rate-limit and cache rules, the purge call and the
  analytics beacon would each need a replacement.
- The functions and the cache run at the edge in front of R2 and D1, and the `/d/` function answers
  byte ranges, so DuckDB can attach a version's database over HTTPS and a Parquet reader can fetch
  only the parts it needs.
- Pages caps the files in one deployment ([0013](0013-pages-under-the-file-cap.md)), and an edge
  cache that holds dated files for a year makes a rewritten file a purge, as
  [0002](0002-versions-are-kept.md) requires.

See `docs/ARCHITECTURE.md` ("Hosting", "Raw store", "Query API", "Analytics") and `README.md`
("Deploy").
