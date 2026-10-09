# 0002: A dated version is kept for good and keeps its content

- Status: **Accepted** (the rule predates the public release; its wording on corrections and
  removals: #46; the raw store kept append-only: #105; checksums: #73; `partition_by` edits as
  corrections: #110)
- Date: 2026-10-04
- Deciders: National Digital

## Context

Government portals replace files in place and rarely keep the releases before. publicdata.au keeps
that history: each change to a source becomes a dated version at `/d/<slug>/v/<date>/`, with its
formats, its manifest and the publisher's own file. People cite these URLs, the clients read them,
and the edge serves them with a year-long cache.

A rule that a version can never change at all would leave a fault in our own conversion published
for good under our name. A rule that lets files change freely would make a citation worthless.

## Decision

- A change to a source makes a version, and an unchanged source makes none. For a release, a
  version needs a new source hash and rows that hash differently (`rows_sha256`). Rolling sources
  and feeds make a dated version only when a fetch becomes a snapshot
  ([0005](0005-update-classes.md)).
- Every version is kept indefinitely, including after the publisher withdraws or replaces the
  source. Its source bytes never change.
- A version's content is every file under its dated path, its `by/<field>/` files and its
  `SHA256SUMS` included. Its converted files are rebuilt only to correct a fault in our conversion
  or in the publisher's attribution, to comply with the law, or when a publisher asks for removal.
  The change goes in the version's notes, is published by a `replace` dispatch, and is purged from
  the edge cache.
- A file is removed only for a legal takedown or a publisher's request to remove its dataset. Each
  affected manifest keeps a `tombstone` with the date and the reason, so the manifest and the
  source hash stay on record.
- Withholding a dataset stops serving it without removing it. A dataset taken out of `live`, or a
  file listed in `withheld.json`, answers 410 while R2 keeps the bytes.
- How a file is stored may change when the bytes a client receives do not
  ([0011](0011-text-stored-compressed.md), [0014](0014-build-cache-in-r2.md)).

Files outside a dated path, such as diffs, `changes.json`, `history.tar.zst` and `latest/`, are
views over the versions. They are rebuilt whenever their inputs change and cached for five minutes.

## Alternatives considered

- **Keep the newest file only**, as the portals do. The history is the part of the site the portals
  cannot provide, so this would remove the reason for the site.
- **Never change a published file, even to correct it.** A conversion fault would then stay in every
  cited copy. Correcting it in place keeps the version faithful to the publisher's file, which is
  what the version promises, and the notes say what changed.

## Consequences

- Storage grows with every version and is never reclaimed. [0005](0005-update-classes.md),
  [0007](0007-formats-each-version-carries.md) and [0012](0012-storage-budget.md) keep that growth
  in proportion to what changes.
- R2 keeps no earlier copy of an object, so the raw store is append-only because the push refuses
  to overwrite an object a committed version holds (#105).
- An edit to an entry's `partition_by` changes the `by/` files of every published version, so it is
  a correction and reaches those versions only through a `replace` with a note in each manifest.
  The deploy fails such an edit until the notes are there (#110).
- A correction rebuilds and purges every affected version, and the hubs' copies are refreshed
  where the hub allows it. `docs/CORRECTIONS.md` keeps the log; its first entry, the ABS attribution
  links of 6 October 2026, records a rebuild still to run.
- The terms page ("Versions and changes") states the same rule for readers.

See `docs/ARCHITECTURE.md` ("Rules that decide the code", "Archive") and `docs/CORRECTIONS.md`.
