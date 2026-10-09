# 0016: Points are joined by location to named ABS boundary layers, and the join is marked

- Status: **Accepted, one part open** (the join and its marks: the initial public release; layer
  sources in the build key: #77; the spatial extension from a pinned copy: #91; the rule for a new
  ABS edition is open)
- Date: 2026-10-08
- Deciders: National Digital
- Relates to: [0002](0002-versions-are-kept.md), [0018](0018-deterministic-builds.md)

## Context

The site publishes nothing derived. It re-keys and re-types, and it joins only on a key the data
declares. Many datasets give each row a point and no area, or name an area in a scheme of their
own, so a reader cannot count crashes by council or join two datasets by suburb without doing
spatial work first.

The ABS publishes the boundaries of each area under CC BY 4.0, by edition, and the register holds
six of those layers as datasets of their own: SA2 (2021), LGA (2025), suburb and locality (2021),
postal area (2021), and state and federal electoral division (2025).

## Decision

- A point's location against a named version of an ABS boundary layer counts as a declared key. With
  a feed's observation dates ([0005](0005-update-classes.md)), it is one of the two places the site
  adds values the publisher did not publish, and `spine.py` holds the layers it may use.
- A point dataset lists the layers in `enrich`, and each row gains the code and name of the area its
  point falls in for each. Field names carry the edition's year, such as `lga_2025_code`.
- The point is moved to the boundaries' datum for the join only. The published coordinates stay as
  the publisher gave them.
- Each joined column is marked in `schema.json` as joined by location, with the layer's dataset and
  version, and its description says the publisher did not publish it. The manifest records each
  layer read, and every file's provenance names the layers and carries the ABS attribution.

**Open: a new edition.** When the ABS publishes a new edition, the intent is to add it as a new
layer beside the old one, with field names carrying its year, and to move a dataset by changing
`enrich`. A dated file in R2 keeps its first join, but a rebuild caused by a new layer writes a new
cache entry, and the pages built from it would describe the new join. No dataset moves to a new
edition until a record settles how a published version's pages stay in step with its files.

## Alternatives considered

- **No join, as the strict rule would have it.** Readers would each do the same spatial work, and
  two datasets could not be compared by area without it. A join against the ABS's own boundaries
  gives every joined dataset the same codes.

## Consequences

- Naming the edition in each field and recording the layer version on each version means a reader
  can tell which boundaries a code came from, and a later edition never changes what an earlier
  column meant.
- A joined dataset's build needs DuckDB's spatial extension, installed from a pinned copy, and the
  sources of every layer it reads. The layer's source and register entry and the extension's
  version are in the joined version's build key, so a change to any of them builds the joined
  versions again.
- The real-data check pulls a sampled joined dataset's layers beside it, so a change to the join is
  checked whenever a joined dataset is drawn.

See `docs/ARCHITECTURE.md` ("Rules that decide the code") and `CONTRIBUTING.md` ("Reference").
