# 0003: The fetch and the catalogue harvest run on our own runner in Australia

- Status: **Accepted** (the initial public release)
- Date: 2026-10-04
- Deciders: National Digital

## Context

Some portals, Queensland's among them, turn away requests from cloud addresses. Some answer them
with an empty response in place of the file. A fetch from GitHub-hosted runners would therefore
miss sources or, without checks, record empty files as versions.

The fetch and the harvest also hold the credentials that write the raw store, and they open data
pull requests that merge on their own. Where they run, and who can start them, decides who can
change the archive.

## Decision

- `fetch.yml` and `catalogue.yml` run on a self-hosted runner in Australia, in the `fetch`
  environment, which only `main` can use. The fetch reads every live source each Tuesday and the
  live feeds on the other six days.
- The runner's group admits only those two workflows, on `main`. They start on their schedule or
  when a maintainer runs them, and only in `National-Digital/publicdata.au`, so a fork runs checks
  only.
- Both open their pull requests as the GitHub App "publicdata.au fetch". A ruleset lets only that
  App create or push `data/` branches, and `data-pr.yml` approves the App's pull requests and no
  others.
- Everything else runs on GitHub-hosted runners. That includes the hubs workflow, which holds the
  same App key in the `production` environment to open its `data(hubs)` pull requests and reaches
  no portal.

## Alternatives considered

- **GitHub-hosted runners for the fetch.** They need no machine of our own, but the portals that
  refuse cloud addresses would be missed.

## Consequences

- The fetch depends on one machine we maintain. When it is down, the schedule waits, and a queued
  job that no runner takes is cancelled by GitHub after a day, so the run never fails and no one is
  told.
- A run can wait for the runner while `main` moves, so the workflows cut their data branches from
  the current `main` before pushing.
- Contributors cannot run the scheduled fetch. They fetch locally, which needs no credentials.
- `expect_page` refuses an empty body or an HTML page where a data file should be, so a refused
  request fails the fetch and never becomes a version.

See `README.md` ("Deploy") and the two workflow files.
