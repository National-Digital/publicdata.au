# publicdata.au — agent conventions

- This repo is public: no credentials, internal hostnames, client names or
  references to other projects in code, comments, commit messages or docs. It stands alone.
- Minimum comments; why-only. Narrative goes in the PR body.
- `pipeline/` is the whole build (Python 3.14). `register/` is data: one YAML per dataset,
  requests included, and `register/publishers/` curates publishers the portals cannot place. `store/` holds committed manifests; the bytes beside them are in R2 and
  gitignored. `functions/` is the Pages Functions layer (votes, requests, `latest/` redirects,
  R2 fallback). `dist/` and `dist-large/` are output and are never committed.
- The published site never fetches upstream. Every file in `dist/` is built from stored
  snapshots. Serialisers are pure functions of the normalised model.
- Nothing derived. Re-keying, re-typing and joining on a declared key only, where a point's
  location against a named version of an ABS boundary layer counts as a declared key (the place
  spine). Joined columns are marked as joined in the schema. No rates, rankings, thresholds or
  imputations, and a publisher's coordinates are never moved.
- Suppression survives: a source cell such as "<5" becomes a null with a typed `suppressed`
  flag, never 0, 5 or blank.
- Ingest is an allow-list. Only columns named in the register are published; new upstream
  columns are reported and held.
- Licence is data. Every register entry carries a licence id, an evidence URL from the
  publisher's own statement, an attribution string and the date a person reviewed it, and
  every version's manifest records where and when the fetch read the licence again. The gate
  refuses to publish a dataset whose licence is not open or has changed since review. ND,
  NC-ND, non-commercial and "restricted" are never published. A grant that is not Creative
  Commons is admitted only through `register/licences/`, quoting the publisher on all four
  tests there.
- Versions are immutable and dated by source change. An unchanged source hash produces no
  version. Two builds of one snapshot are byte-identical; CI proves it.
- A version's build cache key is its inputs and never the build code. An edit to the code that
  changes any published file raises `rebuild:` in the register entry of each dataset it affects,
  or `REBUILD` in `pipeline/publicdata/cache.py` when it reaches across datasets. The deploy's
  real-data check (`publicdata verify`) fails a change that alters a reused version without one.
  A format writer change needs no number.
- This is an archive. Versions are never deleted or rewritten; a withdrawn source keeps its
  version. Portal history is backfilled as versions marked `backfilled`.
- Every payload carries provenance: publisher, licence, attribution, source URL, fetched-at
  and source hash.
- Requesters are recorded by organisation, never by name.
- Every page and catalogue record states the publisher has not endorsed this site.
- Data licences (CC BY) are separate from the code licence and stay that way.
- The only analytics is the Cloudflare Web Analytics beacon, first-party. No other tracking,
  no cookies. The CSP in `_headers` is the enforcement and the test suite checks the beacon.
- Copy follows house language rules: a heading then plain sentences; no "X, not Y" reversals,
  no lists of three for rhythm, no fragments, no em-dashes, no superlatives without a number.
  The gate fails a page that uses seamless, streamline, empower, unlock, leverage or robust.
