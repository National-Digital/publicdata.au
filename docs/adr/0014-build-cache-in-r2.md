# 0014: The build cache has its own bucket, and the publisher's files are served from the raw store

- Status: Accepted
- Date: 2026-10-10

## Context

A deploy reuses every version it has built before, and building them all again takes hours
([0013](0013-deterministic-builds.md)). The GitHub Actions cache holds 10 GB per
repository and evicts entries unused for seven days. In October 2026 the build cache was two entries
of about 4.5 GB each, already over that limit.

A deploy publishes the files of any cache entry it finds without building them again, so whoever
can write the cache can change what the site publishes.

The raw store already holds every publisher's file as fetched. A second copy of each in the
published bucket would have held 7.41 GB in October 2026.

## Decision

- The build cache is kept in the R2 bucket `publicdata-build-cache`, which only the production
  deploy can write. A pull request's preview can read it.
- An entry holds what the build needs to reuse a version without its `data.parquet`: its manifest,
  schema, SQL and a record of each format's writer and the Parquet it was written from. The build
  reads a cached version's Parquet back from the published bucket, which already holds it.
- The function serves the publisher's file from the raw store, and the build writes no copy of it
  to the published bucket. A deploy checks that the raw store holds the source of every version it
  publishes before it goes live.

## Alternatives considered

- **The GitHub Actions cache.** The cache had already outgrown its 10 GB, and an eviction would
  rebuild every version.
- **A prefix in the raw store.** The fetch writes the raw store, so a cache kept there could be
  written with the fetch's credentials and plant an entry a deploy would publish. A bucket of its
  own makes that boundary a permission.

## Consequences

- R2 has no per-repository cap and no expiry, so an entry goes only when the deploy prunes it, after
  a day unused. The cache is saved only after the push to R2 succeeds.
- The publisher's bytes exist in one place, the append-only raw store, from which the archive can
  be rebuilt.
- A `replace` dispatch builds without the cache, so a correction is built from source.

See `docs/ARCHITECTURE.md` ("Hosting", "Raw store") and `README.md` ("Deploy").
