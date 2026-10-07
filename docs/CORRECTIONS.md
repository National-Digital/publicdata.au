# Corrections

This policy sets out how a problem with a published file is reported, checked and fixed, and what
a correction leaves alone. The [about page](https://publicdata.au/about/#corrections) has the short
version for readers.

## Report a problem

publicdata.au republishes what the publisher publishes. Where a report goes depends on where the
fault is.

- **A value that is wrong in the publisher's own file** goes to the publisher. The publisher
  corrects it, and the next version made from their release carries the fix. Each dataset page
  names the publisher and links to the source.
- **A copy here that differs from the publisher's file** is a fault of ours. Examples are a column
  typed badly, a row missing, a suppressed cell read as a number, a file that fails to open, or an
  attribution with a dead link or the wrong publisher. Open a
  [data problem](https://github.com/National-Digital/publicdata.au/issues/new?template=data-problem.yml)
  issue, or write to [National Digital](https://nationaldigital.com.au/contact/) if you would
  rather not post in public. Give the version URL, what differs, and a link to the publisher's copy
  that shows the right value.
- **A copyright, licence or privacy concern, or a publisher's request to remove a dataset,** goes
  to [National Digital](https://nationaldigital.com.au/contact/) in writing. Don't open a public
  issue for it.
- **A security problem** is reported privately, as [SECURITY.md](../SECURITY.md) describes.

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
   are shown on the version's page and stay there. They go in this pull request because the
   rebuild in the next step is what writes the corrected manifest to R2.
3. **Rebuild the versions.** Straight after the pull request merges, a maintainer runs the Deploy
   workflow with `replace` set to those prefixes. The run rebuilds the files and purges the old
   copies from the edge cache. Until it finishes, the version's page already shows the note while
   the old files are still served.
4. **Close the issue.** The issue links the pull request and the versions it rebuilt, and the
   correction goes in the log below.

## What a rebuild does not reach

The Deploy run replaces the files in R2 and purges `d/<slug>/v/<date>/` at the edge. Some copies
sit outside that, and the maintainer checks each one.

- The query API loads a version into D1 again only when its fields change. A fix that changes
  values and keeps the fields leaves the old rows in D1 until that version is loaded again.
- A dated query API answer is cached for a year, and the purge does not cover `/api/`.
- The Python and R clients, with their cache turned on, keep a downloaded version and do not
  fetch it again. Users clear it with `cache_clear()` in Python or `pd_cache_clear()` in R.
- The purge needs the `CLOUDFLARE_PURGE_TOKEN` secret. Without it the run only warns, and the
  prefixes must be purged by hand.

Each correction in the log says which of these it needed.

## What a correction leaves alone

- `source.<ext>` and its hash stay as they were fetched. The raw store is append-only.
- A version is never deleted. Its converted files change only to correct a fault in our conversion
  or in the publisher's attribution, to comply with the law, or when a publisher asks for its
  dataset to be removed, as the [terms](https://publicdata.au/terms/) say. Each such change is
  recorded in the version's notes.
- The version's URL keeps working, except in the cases under "Withdrawal, withholding and
  removal".

## Withdrawal, withholding and removal

These are not corrections, and they change what a version's URL serves.

- When a publisher withdraws a source, its register entry stays `live` so the versions already
  published are still served, and its `note` says the source was withdrawn. Any other status takes
  the dataset out of `latest.json`, and every file of every version then answers 410.
- When a licence turns out not to allow publication, a maintainer withholds the dataset or the
  affected files. A withheld dataset answers 410 for every file R2 still holds, and a file listed
  in `withheld.json`, such as the publisher's file of an entry with `source_withheld`, answers 410
  too. The site says why.
- A file is removed only for a legal takedown or a publisher's request to remove its dataset. The
  removal is recorded in `changes.json` as a tombstone that keeps the manifest and hash, and the
  dataset's files answer 410.

## Correction log

Each correction to a published version is recorded here. The log starts with the public
repository on 4 October 2026, and no published version has been corrected since then.

| Date | Versions | What was wrong | What changed | Copies cleared | Pull request |
| --- | --- | --- | --- | --- | --- |
