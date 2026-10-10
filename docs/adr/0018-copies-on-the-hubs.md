# 0018: The newest version of each dataset is copied to Hugging Face, Zenodo and Kaggle

- Status: Accepted
- Date: 2026-10-10

## Context

Many people look for data on Hugging Face, Zenodo and Kaggle and never visit a government portal.
A dataset that lives on one site also lives on one host, under one account
([0010](0010-hosting-on-cloudflare.md)).

## Decision

- `publicdata hubs` copies each served dataset's newest version to Hugging Face, Zenodo and Kaggle.
  It runs after each production deploy and weekly, reads the live catalogue, and uploads only a
  version newer than the one each hub holds.
- Every copy carries `publicdata.json`, with the version, the licence, the attribution and the hash
  of the publisher's file, and text that links the version URL, so the link back survives a
  re-upload. Zenodo gives each version a DOI under one concept DOI per dataset.
- A dataset is not copied when no hub can carry its licence or its condition of use
  ([0001](0001-licence-is-data.md)), or when it is a database, since the hubs take one table. When
  its newest version is stored as parts alone, each hub keeps the newest whole version.
- A rolling source or a feed goes to each hub at most once a month, from a snapshot, and Zenodo
  takes only its Parquet and gzipped CSV.
- Each run records where every copy is in `store/hubs.json`, which goes up as a `data(hubs)` pull
  request that merges itself once its checks are green. The dataset page lists the copies, and its
  JSON-LD names them.

## Alternatives considered

- **No copies.** People who look only on the hubs would not find the data, and every dataset would
  depend on one host.
- **Every version to every hub.** Each hub would hold a second archive under rate limits and rules
  we do not set, and a correction would have to reach every copy, though a Zenodo record cannot
  change the files it was published with. The newest version, linking back to the dated archive,
  serves the people who look there.

## Consequences

- Each copy names the version it holds, so a citation of a copy leads back to the dated version.
- A correction is refreshed on Hugging Face and Kaggle after its rebuild. A Zenodo record keeps the
  files it was published with, so a Zenodo copy keeps the fault, and `docs/CORRECTIONS.md` says so.
- The hubs workflow holds the fetch App's key to open its pull requests, in the `production`
  environment on a GitHub-hosted runner ([0005](0005-fetch-runs-on-our-own-runner.md)).
- Each hub's rate limits and usability rules shape the run, such as the cards, settings and starter
  notebook Kaggle's usability rating counts.

See `docs/ARCHITECTURE.md` ("Hubs").
