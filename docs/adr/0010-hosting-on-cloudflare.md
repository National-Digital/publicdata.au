# 0010: The site is hosted on Cloudflare, with its files kept as plain objects

- Status: Accepted
- Date: 2026-10-10

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
  repository. Every dataset page, place page and static asset, and the directory, is a Pages file.
  `_headers` sets the security headers and `_routes.json` names the paths that run a function.
- **Pages Functions** in `functions/` serve the dynamic paths.
- **R2** holds what Pages does not, read and written by the pipeline over the S3 API.
  `publicdata-dist` holds every dated version file, version pages included, and any file over
  Pages' per-file limit, at keys equal to their URL paths, with the query copies and rollups beside
  them outside the published tree. `publicdata-raw` holds the publishers' bytes and is append-only,
  `publicdata-votes` holds votes and saved dashboards, and `publicdata-build-cache` holds the build
  cache.
- **D1** answers the query API for the newest versions and holds the catalogue search index.
- **The edge cache** holds dated files as immutable for a year and query answers by version, and
  `publicdata purge` clears a rewritten version from it.
- **Web Analytics** is the only analytics: Cloudflare's beacon, with no cookies, named alone in the
  Content-Security-Policy.

A Pages deployment may hold only so many files, and the account's plan sets how many. The deploy
reads the cap from the `max_file_count_allowed` claim in the project's upload token, as wrangler
does, and takes 20,000 when the token states none. It puts the count of files left for Pages beside
the cap in its summary, warns from 80% of the cap, and above the cap fails before anything is
published. When the cap cannot be read, it warns and goes ahead with the count alone.

## Alternatives considered

- **A host that bills for bytes sent.** The cost of a dataset would grow with its use, which is the
  opposite of what a public archive wants. R2 charges nothing for egress, so what an entry stores
  and the rows it writes to D1 are the costs left, and [0012](0012-storage-budget.md) budgets both.
- **Serving place pages from R2 through the function**, so the Pages file count stays tied to the
  number of datasets. It adds a second way to publish a page: a step that removes R2 pages the build
  no longer writes, a two-phase push because a page can show an image only the new deployment
  holds, and gate checks on pages outside the Pages tree. In October 2026 the account's cap was
  100,000 files and a preview used 14,348 of them, so that cost buys nothing yet. A finished
  implementation is kept on a closed pull request and can be reopened.

## Consequences

Leaving Cloudflare would cost the dynamic layer and keep the data.

- **Portable.** The published tree is plain files at keys equal to their URL paths, and the raw
  store is plain objects whose manifests are committed in `store/`. Both copy to any static host or
  S3-compatible store as they are. The archive can be rebuilt from the raw store and this
  repository, and the newest versions are also copied to the hubs
  ([0018](0018-copies-on-the-hubs.md)).
- **Cloudflare-specific.** The functions use Pages' routing, its R2, D1 and asset bindings and the
  Workers cache API. D1's tables would move to another SQLite host, and the query builder is already
  tested against `node:sqlite`. The zone's rate-limit and cache rules, the purge call and the
  analytics beacon would each need a replacement.
- The functions and the cache run at the edge in front of R2 and D1, and the `/d/` function answers
  byte ranges, so DuckDB can attach a version's database over HTTPS and a Parquet reader can fetch
  only the parts it needs.
- An edge cache that holds dated files for a year makes every correction a purge
  ([0002](0002-versions-are-kept.md)).
- A preview keeps the version files it builds on Pages, so its count runs ahead of production's and
  it warns first. The warning at 80% is the point to choose between a plan with a higher cap and
  serving place pages from R2. The cap is read at each deploy, so a plan change reaches the check
  with no change to the code.

See `docs/ARCHITECTURE.md` ("Hosting", "Raw store", "Query API", "Analytics") and `README.md`
("Deploy").
