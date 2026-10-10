# Decision records

Each record states one decision that shapes publicdata.au: the problem it answers, what was chosen,
what was weighed against it and what it costs. The rules and the code that carries them out are
described in `docs/ARCHITECTURE.md`, `CONTRIBUTING.md` and `docs/CORRECTIONS.md`, and each record
names the section to read. Where a record and those documents disagree on detail, the documents
and the code are right and the record is corrected.

Records 0001 to 0020 were written together on 10 October 2026 to set down the decisions the site
was built on. The measurements in them are dated and stay as they were.

## What is published

- [0001](0001-licence-is-data.md) A dataset is published only under a licence a person has checked
- [0002](0002-versions-are-kept.md) A dated version is kept for good and keeps its content
- [0003](0003-nothing-derived.md) The site publishes what the publisher published, and joins points
  to ABS areas by location
- [0004](0004-update-classes.md) Each source declares how it updates, and only some fetches become
  versions

## How sources are read

- [0005](0005-fetch-runs-on-our-own-runner.md) The fetch and the catalogue harvest run on our own
  runner in Australia
- [0006](0006-no-circumventing-bot-checks.md) Publishers' bot checks are respected, and blocked
  files are fetched by hand

## Formats

- [0007](0007-formats-each-version-carries.md) The formats each version carries, and their size caps
- [0008](0008-parquet-is-the-base-format.md) Parquet is the format everything reads
- [0009](0009-period-partitions.md) Large dated tables are split into period parts

## Hosting, storage and builds

- [0010](0010-hosting-on-cloudflare.md) The site is hosted on Cloudflare, with its files kept as
  plain objects
- [0011](0011-files-stored-at-their-url-paths.md) Each file is stored at its URL path, and text is
  stored compressed
- [0012](0012-storage-budget.md) Each dataset has a storage budget, checked in CI
- [0013](0013-deterministic-builds.md) Two builds of one snapshot are byte-identical, and every tool
  is pinned
- [0014](0014-build-cache-in-r2.md) The build cache has its own bucket, and the publisher's files
  are served from the raw store

## Readers and agents

- [0015](0015-contract-only-grows.md) The public URLs and the API only grow, and a change that
  breaks a caller is marked
- [0016](0016-mcp-server.md) Agents reach the site through one remote MCP server, described by one
  spec
- [0017](0017-how-queries-are-answered.md) The MCP tools count from rollups first, and read D1 for
  the newest versions and Parquet for the rest
- [0018](0018-copies-on-the-hubs.md) The newest version of each dataset is copied to Hugging Face,
  Zenodo and Kaggle

## The site

- [0019](0019-accessibility-aaa.md) Every page meets WCAG 2.2 AAA, and previews are checked against
  Lighthouse targets
- [0020](0020-free-typeface-is-in-the-repository.md) The typeface's free styles are in the
  repository under their own licence

## Changing a decision

A change that reverses or narrows a decision adds a record with the next number and the same
headings (Context, Decision, Alternatives considered, Consequences), in the same pull request as
the change. The record it replaces stays, with its status changed to "Superseded by NNNN" and a
link. A record is added only with the change that carries it out, so every record describes what
the site does.

A record's status is **Accepted** or **Superseded by NNNN**. An open question a decision leaves is
named in its consequences, and the record that settles it supersedes or refines it.
