# 0005: A fetch is not always a version; each source declares how it updates

- Status: **Accepted** (#53)
- Date: 2026-10-06
- Deciders: National Digital
- Refines: [0002](0002-versions-are-kept.md)

## Context

Until #53 every change to a source's bytes made a dated version, with every format. That suits a
publisher that releases an edition, such as an annual report table. It does not suit two other kinds
of source.

- Some sources re-send their whole table on every update, often daily, with a few rows added or
  revised. A road crash file of about 730 MB of published formats became a new version for 16 new
  rows a day. In the week to 6 October 2026, 51 repeat versions carried 1.47 GB of source and about
  19 GB of published files, which is about 1 TB a year at the register of the time.
- Some sources publish only the current state, such as storage levels or station readings, and keep
  no history. Their value to an archive is the history we observe.

## Decision

Each register entry declares `update:`, one of three classes. The default is `release`.

- **release.** Each changed fetch is a dated version, as before. It is checked weekly.
- **rolling.** The source sends its whole table each time, and it is read weekly. A changed read is
  stored as a fetch with a change log by key and refreshes `latest/`. Only some fetches become
  snapshots, the dated versions: the first fetch, one that revises a finished period, one that
  changes more than 5% of rows, the first after a period closes and the first change of a month.
  When a later read finds the table unchanged since a fetch that was no snapshot, that fetch
  becomes the snapshot under its own date, so the last state of a month or period is kept and no
  fetch is ever dated again.
- **feed.** The source holds current state only, and it is read daily. It follows the rolling rules
  and also keeps a history of every state a key has held, with the first and last read that saw it
  (`first_seen`, `last_seen`). A feed always has a period ([0006](0006-period-partitions.md)).

Columns an entry lists as `volatile`, such as a publisher's "last updated" stamp, are left out when
deciding whether the data changed. A rolling source or a feed must be one table with a declared
`key`. Each fetch records its class, so a later change of class leaves its versions as they are.

`first_seen` and `last_seen` are a deliberate exception to the rule that nothing is derived. A feed
keeps no history of its own, so the dates of our reads are the only record of when a state held.
They describe our observation and change no value the publisher sent. The schema marks them as
computed by publicdata.au (`publicdata:derived`), as it marks a joined column, and the dataset page
says how they are observed.

## Alternatives considered

- **Every changed fetch a full version**, as before. That is the 1 TB a year measured above, most of
  it formats that differ from the day before by a few rows.

## Consequences

- The change is opt-in. In October 2026 every one of the 375 register entries, 364 of them live, is
  a release, so no entry yet uses `rolling`, `feed` or `volatile`. Each move is a reviewed edit to
  one entry.
- Six entries set the older `source.feed: true`, a release read daily that makes at most one version
  a day. It stays, and an entry cannot set both it and `update: feed`.
- For a rolling source or a feed, `latest/` serves its newest fetch built whole, in place, with a
  five-minute cache. A stable citation has to name a snapshot.
- Every changed fetch stays in the raw store with its change log, and `/d/<slug>/changes/`
  publishes the logs, so the site keeps every state it read between snapshots.
- The hubs take a rolling source or a feed at most once a month, from a snapshot
  ([0022](0022-copies-on-the-hubs.md)).
- Of the versions published before #53, 37 are ones these rules would not have made, holding
  13.8 GB. They stay published under [0002](0002-versions-are-kept.md).

See `docs/ARCHITECTURE.md` ("Update classes", "Archive") and `CONTRIBUTING.md` ("Reference").
