# 0004: Each source declares how it updates, and only some fetches become versions

- Status: Accepted
- Date: 2026-10-10

## Context

A version for every change to a source's bytes suits a publisher that releases editions, such as
an annual report table. It does not suit two other kinds of source.

- Some sources re-send their whole table on every update, often daily, with a few rows added or
  revised. A road crash file of about 730 MB of published formats became a new version for 16 new
  rows a day. In the week to 6 October 2026, 51 such repeat versions carried 1.47 GB of source and
  about 19 GB of published files, which is about 1 TB a year at the register of the time.
- Some sources publish only the current state, such as storage levels or station readings, and keep
  no history. Their value to an archive is the history we observe.

## Decision

Each register entry declares `update:`, one of three classes. The default is `release`.

- **release.** Each changed fetch is a dated version. It is checked weekly.
- **rolling.** The source sends its whole table each time, and it is read weekly. A changed read is
  stored as a fetch with a change log by key and refreshes `latest/`. Only some fetches become
  snapshots, the dated versions: the first fetch, one that revises a finished period, one that
  changes more than 5% of rows, the first after a period closes and the first change of a month.
  When a later read finds the table unchanged since a fetch that was no snapshot, that fetch
  becomes the snapshot under its own date, so the last state of a month or period is kept and no
  fetch is ever dated again.
- **feed.** The source holds current state only, and it is read daily. It follows the rolling rules
  and also keeps a history of every state a key has held, with the first and last read that saw it
  (`first_seen`, `last_seen`). A feed always has a period ([0009](0009-period-partitions.md)).

Columns an entry lists as `volatile`, such as a publisher's "last updated" stamp, are left out when
deciding whether the data changed. A rolling source or a feed must be one table with a declared
`key`. Each fetch records its class, so a later change of class leaves its versions as they are.

`first_seen` and `last_seen` are the only values besides the place join that the site adds to a
publisher's rows ([0003](0003-nothing-derived.md)). A feed keeps no history of its own, so the
dates of our reads are the only record of when a state held. They describe our observation and
change no value the publisher sent. The schema marks them as computed by publicdata.au
(`publicdata:derived`), and the dataset page says how they are observed.

## Alternatives considered

- **Every changed fetch a full version.** That is the 1 TB a year measured above, most of it formats
  that differ from the day before by a few rows.

## Consequences

- The classes are opt-in, and each move is a reviewed edit to one entry. In October 2026 every one
  of the 375 register entries is a release.
- An entry may still set `source.feed: true`, a release read daily that makes at most one version a
  day. It cannot set both that and `update: feed`.
- For a rolling source or a feed, `latest/` serves its newest fetch built whole, in place, with a
  five-minute cache. A stable citation has to name a snapshot.
- Every changed fetch stays in the raw store with its change log, and `/d/<slug>/changes/`
  publishes the logs, so the site keeps every state it read between snapshots.
- The hubs take a rolling source or a feed at most once a month, from a snapshot
  ([0018](0018-copies-on-the-hubs.md)).

See `docs/ARCHITECTURE.md` ("Update classes", "Archive") and `CONTRIBUTING.md` ("Reference").
