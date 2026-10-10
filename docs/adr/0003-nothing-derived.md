# 0003: The site publishes what the publisher published, and joins points to ABS areas by location

- Status: Accepted
- Date: 2026-10-10

## Context

A republished value is worth citing only if a reader can trust that the publisher published it.
Every rate, estimate or adjusted value the site worked out for itself would need its own method,
its own caveats and its own corrections, and a reader could not tell it from the publisher's.

Many datasets give each row a point and no area, or name an area in a scheme of their own, so a
reader cannot count crashes by council or join two datasets by suburb without doing spatial work
first. The ABS publishes the boundaries of each area under CC BY 4.0, by edition, and the register
holds six of those layers as datasets of their own: SA2 (2021), LGA (2025), suburb and locality
(2021), postal area (2021), and state and federal electoral division (2025).

## Decision

- The published site never fetches upstream. Every file is built from the stored snapshots.
- Every payload carries its provenance: the publisher, the licence, the attribution, the source
  URL, the fetch time and the source hash. Every page and catalogue record says the publisher has
  not endorsed the site.
- Nothing is derived. The build re-keys, re-types and joins on a key the data declares. It
  publishes no rates, rankings, thresholds, estimates or imputations, and never moves a
  publisher's coordinates. A database's views are the publisher's own scripts, kept in the
  register. Counts of the publisher's own rows, such as the figures, the place pages, the diffs and
  the rollups, are not derived values.
- A suppressed cell such as "<5" becomes a null with a typed `suppressed` flag, never 0, 5 or a
  blank.
- Ingest is an allow-list. Only the columns a register entry names are published, and new upstream
  columns are reported and held.
- A point's location against a named version of an ABS boundary layer counts as a declared key.
  `spine.py` holds the layers the build may use, and a point dataset lists them in `enrich`. Each
  row gains the code and name of the area its point falls in, under field names that carry the
  edition's year, such as `lga_2025_code`. The point is moved to the boundaries' datum for the join
  only.
- Each joined column is marked in `schema.json` as joined by location, with the layer's dataset and
  version, and its description says the publisher did not publish it. The manifest records each
  layer read, and every file's provenance names the layers and carries the ABS attribution.
- The only other columns the site adds to a publisher's rows are a feed's observation dates
  ([0004](0004-update-classes.md)).

## Alternatives considered

- **No join at all.** Readers would each do the same spatial work, and two datasets could not be
  compared by area without it. A join against the ABS's own boundaries gives every joined dataset
  the same codes.
- **Derived figures, such as rates per head.** A rate needs a denominator and an edition the
  publisher did not choose, and it would sit beside the publisher's figures looking like one of
  them.

## Consequences

- Naming the edition in each field and recording the layer version on each version means a reader
  can tell which boundaries a code came from, and a later edition never changes what an earlier
  column meant.
- A joined dataset's build needs DuckDB's spatial extension, installed from a pinned copy, and the
  sources of every layer it reads. Those sources, their register entries and the extension's
  version are in the joined version's build key ([0013](0013-deterministic-builds.md)).
- The real-data check pulls a sampled joined dataset's layers beside it, so a change to the join is
  checked whenever a joined dataset is drawn.
- **Open: a new ABS edition.** The intent is to add a new edition as a new layer beside the old one
  and to move a dataset by changing `enrich`. A dated file in R2 keeps its first join, but a rebuild
  caused by a new layer writes a new cache entry, and the pages built from it would describe the new
  join. No dataset moves to a new edition until a record settles how a published version's pages
  stay in step with its files.

See `docs/ARCHITECTURE.md` ("Rules that decide the code") and `CONTRIBUTING.md` ("Reference").
