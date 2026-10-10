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
   `d/qld-road-crash-locations/v/2026-04-24/`. It raises the rebuild number of each affected
   dataset, or `REBUILD` in `cache.py` when the fault reaches across datasets, as
   [Change the build code](../CONTRIBUTING.md#change-the-build-code) sets out. A wrong
   attribution, such as a dead link or a misnamed publisher, is fixed in the register entry and
   needs no test.
2. **Note each affected version.** The same pull request adds a dated line to the `notes` of each
   affected version's manifest in `store/`. The line says what was wrong and what changed. Notes
   are shown on the version's page and stay there. They go in this pull request because the
   rebuild in the next step is what writes the corrected manifest to R2.
3. **Rebuild the versions.** Once the deploy that the merge started has finished, a maintainer
   runs the Deploy workflow with `replace` set to those prefixes. Push and dispatch runs on `main`
   share one queue, and a newer run cancels one that is still waiting, so the dispatch goes in
   only when nothing else is queued. The run rebuilds the files and purges the old copies from
   the edge cache. Until it finishes, the version's page already shows the note while the old
   files are still served.
4. **Confirm the rebuild.** The maintainer checks that the `replace` run completed and that its
   "Purge replaced versions from the edge" step passed, then fetches one corrected file to see the
   fix. A cancelled or failed run is dispatched again.
5. **Close the issue.** The issue links the pull request, the `replace` run and the versions it
   rebuilt, and the correction goes in the log below.

## A change to `partition_by`

The partition files under `by/<field>/` are part of each version, so an edit to an entry's
`partition_by` corrects every version already published. The field is in each version's key, so
the deploy builds every stored version again with the new partitions and no rebuild number is
needed. The pull request notes each stored version as step 2 sets out. The deploy's plan fails it
until every stored version's manifest gains a note, and its error names the prefixes the
`replace` run takes.

The deploy the merge starts builds the new partition files and stops before it writes any of them.
Outside a `replace`, `dist-push` refuses to add a file under `by/` to a version whose
`manifest.json` R2 already holds, and it names those versions. Every deploy of `main` stops at
that point until a maintainer runs the Deploy workflow with `replace` set to the prefixes, so the
dispatch follows the merge straight away. A replace overwrites and adds files and deletes none, so
the files of a field taken out of `partition_by` stay in R2 under each version.

## A withdrawn boundary layer

A joined version keeps the ABS layer versions its fetch pinned, and a `replace` builds from the
committed manifest, pin included. When the ABS withdraws a layer version as wrong, the pull
request edits the `spine` pin in each affected version's manifest in `store/` to the layer
version that replaces it, which must already be committed, and notes each one as step 2 sets
out. The new pin moves each version's key, so the deploy builds them again, and the `replace`
run of step 3 writes them over the published files and purges the edge.

## What a rebuild does not reach

The Deploy run replaces the files in R2, and `publicdata purge` clears `d/<slug>/v/<date>/` and
that version's query API answers under `api/v1/datasets/<slug>/versions/<date>/` at the edge. Some
copies sit outside that, and the maintainer deals with each one.

- The query API loads a version into D1 again only when its fields change, and it holds only each
  dataset's newest version. Its answers carry the attribution from D1 as well as the values. When a
  fix changes values or the attribution in a newest version and keeps its fields, the maintainer
  deletes that version's row from D1 before dispatching the rebuild, with
  `npx wrangler d1 execute publicdata --remote --command "DELETE FROM _versions WHERE slug = '<slug>' AND version = '<date>'"`.
  The `replace` run then loads the version again. An older version is not in D1, so it needs
  nothing.
- The Python and R clients, with their cache turned on, keep a downloaded version and do not
  fetch it again. Users clear it with `cache_clear()` in Python or `pd_cache_clear()` in R, and
  the client documentation says so. The version's notes are how they learn of the correction.
- The purge needs the `CLOUDFLARE_PURGE_TOKEN` secret. Without it the run only warns, and the
  maintainer runs `publicdata purge` with the same prefixes and the token.

Each correction in the log says which of these it needed. A copy that could not be cleared is
recorded as stale, with the date it stops being served.

## What a correction leaves alone

- `source.<ext>` and its hash stay as they were fetched. The raw store is append-only.
- A version is never deleted, and its converted files change only for the reasons the
  [Archive](ARCHITECTURE.md#archive) rules give, which the [terms](https://publicdata.au/terms/)
  state for readers. Each such change is recorded in the version's notes.
- The version's URL keeps working, except in the cases under "Withdrawal, withholding and
  removal".

## Withdrawal, withholding and removal

These are not corrections, and they change what a version's URL serves.

- When a publisher withdraws a source, its register entry stays `live` so the versions already
  published are still served, and its `note` says the source was withdrawn. Any other status takes
  the dataset out of `latest.json`, and every file of every version then answers 410. A live entry
  is still fetched every day. While the source is gone each fetch reports the dataset as failed,
  and if the publisher later serves a file at the same address, the fetch makes a new version
  from it. A maintainer checks any such version before it merges and reverts it when the file is
  not the same dataset.
- When a licence turns out not to allow publication, a maintainer withholds the dataset or the
  affected files. A withheld dataset answers 410 for every file R2 still holds, and a file listed
  in `withheld.json`, such as the publisher's file of an entry with `source_withheld`, answers 410
  too. The site says why.
- A file is removed only for a legal takedown or a publisher's request to remove its dataset.
  Nothing in the build does this on its own, so a maintainer does it by hand. They withhold the
  dataset by moving its register entry out of `live`, so every file R2 still holds answers 410.
  They delete the removed files from R2 and run `publicdata purge` on each affected version. They
  set `tombstone` in each affected version's manifest in `store/` to the date and the reason,
  which keeps the manifest and the source hash on record. The removal then goes in the log below.

## Correction log

Each correction to a published version is recorded here. The log starts with the public
repository on 4 October 2026. An entry whose rebuild has not yet run says so, and it is updated
once the rebuild is confirmed.

| Date | Versions | What was wrong | What changed | Copies cleared | Pull request |
| --- | --- | --- | --- | --- | --- |
| 6 October 2026 | `d/au-births-by-age-of-mother/v/2026-10-03/`, `d/au-births-by-age-of-father/v/2026-10-03/`, `d/au-births-by-state/v/2026-10-03/`, `d/au-building-approvals-by-lga-2025-26/v/2026-10-03/` | The attribution in every file linked the ABS dataflow at `explore.data.abs.gov.au`, a host that no longer resolves. | The register entries link the same dataflows at `dataexplorer.abs.gov.au`, and the dataset pages have shown the working link since the deploy of 6 October 2026. The data is unchanged. Each version's manifest carries a dated note saying so. The `replace` run of 10 October 2026 rebuilt the four versions, and their dated files now link the working host. | The four D1 rows were deleted before the `replace` run, which loaded the versions again and purged the edge. Raising each entry's `rebuild` number rebuilds its history archive from the corrected files. The Hugging Face and Kaggle copies are refreshed by a Hubs run with `refresh` set. The files in the four Zenodo records keep the dead link. Cached client copies keep the dead link until the user clears the cache. | [#33](https://github.com/National-Digital/publicdata.au/pull/33), [#118](https://github.com/National-Digital/publicdata.au/pull/118), [run 38038585612](https://github.com/National-Digital/publicdata.au/actions/runs/38038585612) |
