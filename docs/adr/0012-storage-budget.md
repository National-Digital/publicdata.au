# 0012: Each dataset has a storage budget, checked in CI

- Status: **Accepted** (#50; narrowed to edits that move cost: #102; Cloudflare's measured storage
  published beside the projection: #119)
- Date: 2026-10-06
- Deciders: National Digital
- Relates to: [0005](0005-update-classes.md), [0007](0007-formats-each-version-carries.md)

## Context

publicdata.au is a civic service, run at a nominal cost, and open to contributions from anyone
who can write a register entry. One entry can commit the site to terabytes over a few years: a
large table fetched daily with every format kept for every version. A reviewer reading a YAML file
cannot see that.

Cloudflare R2 bills storage by the GB-month and charges nothing for egress. Versions are kept for
good ([0002](0002-versions-are-kept.md)), so the cost that compounds is what is stored. The query
layer's loader also writes every row of a new version to D1, which bills rows written.

## Decision

- CI projects each register entry's storage growth in GB a year and its D1 rows written a year,
  from the bytes one version adds and the versions a year its cadence and its history give.
- The budget is 5 GB a year and 10 million D1 rows a year per entry. A pull request that adds an
  entry over budget, or edits something an entry's cost depends on and leaves it over budget, fails
  the check until a maintainer other than the author adds the label `cost-approved` after the head
  commit. An entry whose size cannot be worked out fails closed.
- A pull request's check summary shows the projection for each entry it prices and the fleet
  totals.
- `/health.json` publishes the build's projection and, beside it, Cloudflare's own measurement of
  what the two buckets hold. Neither is given as a price.

## Alternatives considered

- **Review by eye.** The size of a dataset over years is not visible in its register entry, and the
  cost lands long after the pull request that caused it.

## Consequences

- 5 GB a year covers almost every dataset in the register. The large ones get a person's attention
  and, usually, an update class or a period ([0005](0005-update-classes.md),
  [0006](0006-period-partitions.md)).
- A new entry's projection is an estimate until its first version exists, and the summary says so.
- An edit that cannot change what an entry costs, such as a description, is not priced, so an entry
  already over budget does not need approval for it. Routine fetch pull requests only add versions
  to entries already approved, so they are not gated.
- The check runs the base branch's code against the pull request's register, so a pull request
  cannot change the rule that judges it, and a push removes the label.
- The measured figure counts every object in the two buckets, query copies included, which the
  projection leaves out, so the two are expected to differ. The account also carries a budget
  alert on its usage.

See `docs/ARCHITECTURE.md` ("Storage cost") and `CONTRIBUTING.md` ("Add a dataset").
