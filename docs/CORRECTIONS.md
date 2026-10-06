# Corrections

How a problem with a published file is reported, checked and fixed, and what a correction never
changes. The [about page](https://publicdata.au/about/#corrections) has the short version for
readers.

## Report a problem

publicdata.au republishes what the publisher publishes. Where a report goes depends on where the
fault is.

- **The value is wrong in the publisher's own file.** The publisher corrects it, and the next
  version made from their release carries the fix. Each dataset page names the publisher and links
  to the source.
- **Our copy differs from the publisher's file.** Examples are a column typed badly, a row
  missing, a suppressed cell read as a number or a file that fails to open. Open a
  [data problem](https://github.com/National-Digital/publicdata.au/issues/new?template=data-problem.yml)
  issue, or write to [National Digital](https://nationaldigital.com.au/contact/) if you would
  rather not post in public. Give the version URL, what differs, and a link to the publisher's copy
  that shows the right value.
- **A copyright, licence or privacy concern, or a request from a publisher to remove a dataset.**
  Write to [National Digital](https://nationaldigital.com.au/contact/). Don't open a public issue.
- **A security problem.** Report it privately as [SECURITY.md](../SECURITY.md) describes.

## Check the report

A maintainer compares the files at the reported version with the publisher's file, which is kept
beside every version as `source.<ext>` with its SHA-256 in `manifest.json`.

- If the publisher's file has the same value, the issue is closed with a pointer to the publisher.
- If our copy differs, the issue is confirmed, and the maintainer lists every version and dataset
  that the same fault reaches.

## Fix it

1. **Fix the conversion.** A pull request fixes the cause and adds a test that fails without the
   fix. Its description lists the version prefixes it affects, such as
   `d/qld-road-crash-locations/v/2026-04-24/`. A wrong attribution, such as a dead link or a
   misnamed publisher, is fixed in the register entry and needs no test.
2. **Note each affected version.** The same pull request adds a dated line to the `notes` of each
   affected version's manifest in `store/`. The line says what was wrong and what changed. Notes
   are shown on the version's page and stay there.
3. **Rebuild the versions.** Once the pull request merges, a maintainer runs the Deploy workflow
   with `replace` set to those prefixes. The run rebuilds the files and purges the old copies from
   the edge cache.
4. **Close the issue.** The issue links the pull request and the versions it rebuilt, and the
   correction goes in the log below.

## What a correction never changes

- **The publisher's file.** `source.<ext>` and its hash stay as they were fetched. The raw store is
  append-only.
- **The version itself.** A version is never deleted, and its URL keeps working. Its converted
  files change only to correct a fault in our conversion or in the publisher's attribution, to
  comply with the law, or when a publisher asks for its dataset to be removed, as the
  [terms](https://publicdata.au/terms/) say. Each such change is recorded in the version's notes.
- **The history.** A legal takedown is the only time a file is removed. It is recorded in
  `changes.json` as a tombstone that keeps the manifest and hash. When a publisher withdraws a
  source, its register entry records the reason and the versions already published stay where
  they are.

## Correction log

Each correction to a published version is recorded here. The log starts with the public
repository on 4 October 2026, and no published version has been corrected since then.

| Date | Versions | What was wrong | What changed | Pull request |
| --- | --- | --- | --- | --- |
