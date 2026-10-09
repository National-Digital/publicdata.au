# 0022: The newest version of each dataset is copied to Hugging Face, Zenodo and Kaggle

- Status: **Accepted** (the initial public release; the Zenodo listing kept in step: #83; one open
  hubs pull request at a time: #98)
- Date: 2026-10-04 (recorded 2026-10-10)
- Deciders: National Digital
- Relates to: [0002](0002-versions-are-kept.md), [0017](0017-hosting-on-cloudflare.md),
  [0020](0020-licence-is-data.md)

## Context

Many people look for data on Hugging Face, Zenodo and Kaggle and never visit a government portal.
A dataset that lives on one site also lives on one host, under one account
([0017](0017-hosting-on-cloudflare.md)).

## Decision

- `publicdata hubs` copies each served dataset's newest version to Hugging Face, Zenodo and Kaggle.
  It runs after each production deploy and weekly, reads the live catalogue, and uploads only a
  version newer than the one each hub holds.
- Every copy carries `publicdata.json`, with the version, the licence, the attribution and the hash
  of the publisher's file, and text that links the version URL, so the link back survives a
  re-upload. Zenodo gives each version a DOI under one concept DOI per dataset.
- A dataset is not copied when no hub can carry its licence or its condition of use
  ([0020](0020-licence-is-data.md)), when it is a database, since the hubs take one table, or when
  its newest version is stored as parts alone, in which case each hub keeps the newest whole
  version.
- A rolling source or a feed goes to each hub at most once a month, from a snapshot, and Zenodo
  takes only its Parquet and gzipped CSV.
- Each run records where every copy is in `store/hubs.json`, which goes up as a `data(hubs)` pull
  request that merges itself once its checks are green. The dataset page lists the copies, and its
  JSON-LD names them.

## Alternatives considered

No alternative was written down when this was decided. The choice of the newest version alone
leaves the archive of every version on publicdata.au, which each copy links back to.

## Consequences

- The copies reach people where they already look, and each one names the version it holds, so a
  citation of a copy leads back to the dated version.
- A correction is refreshed on Hugging Face and Kaggle after its rebuild. A Zenodo record keeps the
  files it was published with, so a Zenodo copy keeps the fault, and `docs/CORRECTIONS.md` says so.
- The hubs workflow holds the fetch App's key to open its pull requests, in the `production`
  environment on a GitHub-hosted runner ([0003](0003-fetch-runs-on-our-own-runner.md)).
- Each hub's rate limits and usability rules shape the run, such as the cards, settings and starter
  notebook Kaggle's usability rating counts.

See `docs/ARCHITECTURE.md` ("Hubs").
