# 0014: The build cache lives in its own R2 bucket and the publisher's files are served from the raw store

- Status: **Accepted** (#48; its own bucket: #90)
- Date: 2026-10-06
- Deciders: National Digital
- Relates to: [0008](0008-parquet-is-the-base-format.md), [0018](0018-deterministic-builds.md)

## Context

The deploy kept its build cache in the GitHub Actions cache, which holds 10 GB per repository and
evicts entries unused for seven days. By October 2026 the cache was two entries of about 4.5 GB
each, over the limit, and an eviction meant rebuilding every version, which took about an hour
and a half.

Every version also stored the publisher's file twice: once in the raw store, as fetched, and again
in the published bucket as `source.<ext>`, 7.41 GB in all.

A deploy publishes the files of any cache entry it finds without building them again, so whoever
can write the cache can change what the site publishes.

## Decision

- The build cache is kept in the R2 bucket `publicdata-build-cache`, which only the production
  deploy can write. A pull request's preview can read it.
- An entry holds what the build needs to reuse a version without the Parquet: its manifest, schema,
  SQL and a record of each format's writer and the Parquet it was written from. The build reads a
  cached version's Parquet back from the published bucket, which already holds it.
- The publisher's file is served from the raw store by the function, and the build no longer writes
  `source.<ext>`. A deploy checks that the raw store holds the source of every version it publishes
  before it goes live.

## Alternatives considered

- **A prefix in the raw store**, as #48 first had it. The fetch writes the raw store, so a cache
  kept there could be written with the fetch's credentials and plant an entry a deploy would
  publish. A bucket of its own makes that boundary a permission (#90).

## Consequences

- R2 has no per-repository cap and no expiry, so an entry goes only when the deploy prunes it, after
  a day unused. The cache is saved only after the push to R2 succeeds.
- The publisher's bytes exist in one place, the append-only raw store, from which the archive can
  be rebuilt.
- A `replace` dispatch builds without the cache, so a correction is built from source.
- The `source.*` objects written before #48 are still in the published bucket, and the function
  serves them first. They go only when a maintainer approves a one-off deletion, and so do the
  cache entries #48 wrote under `_build/` in the raw store before #90 moved the cache.

See `docs/ARCHITECTURE.md` ("Hosting", "Raw store") and `README.md` ("Deploy").
