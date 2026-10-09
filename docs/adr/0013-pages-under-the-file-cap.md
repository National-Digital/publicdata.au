# 0013: Every page stays on Pages, and the deploy watches the account's file cap

- Status: **Accepted** (#133)
- Date: 2026-10-09
- Deciders: National Digital
- Relates to: [0017](0017-hosting-on-cloudflare.md)

## Context

Cloudflare Pages caps the files in one deployment, and the account's plan sets the cap. Dated
version files, query copies and files over Pages' per-file limit already go to R2, but every
dataset page, place page and static asset is a Pages file, and place pages grow with each dataset
that gains a place field.

An earlier draft of this record took the free plan's 20,000 files as the cap and proposed serving
place pages, station pages and version pages from R2 through the Pages Function. #132 built that
for place pages. The project's upload token then gave the account's real cap, 100,000 files, and a
preview of October 2026 left 14,348 files for Pages, 14% of it. Production left about 11,800.

## Decision

- Dataset pages, place pages, the directory and the static assets stay on Pages.
- The deploy reads the cap from the `max_file_count_allowed` claim in the project's upload token, as
  wrangler does, and takes 20,000 when the token states none. It counts the files left for Pages and
  puts both in its summary. It warns from 80% of the cap, and above the cap it fails before anything
  is published.
- When the cap cannot be read, the deploy warns that it was not read and goes ahead with the count
  alone, since Pages may well accept it.

## Alternatives considered

- **Serve place pages from R2 (#132).** It keeps the Pages file count tied to the number of
  datasets, but it adds a second way to publish a page: a step that removes R2 pages the build no
  longer writes, a two-phase push because a page can show an image only the new deployment holds,
  and gate checks on pages outside the Pages tree. At 14% of the cap that cost buys nothing yet.
  #132 is finished and reviewed, and can be reopened.
- **A plan with a higher cap.** That is the other answer when the warning fires.

## Consequences

- A preview keeps the version files it builds on Pages, so its count runs ahead of production's and
  it warns first.
- The warning at 80% is the point to choose between a plan change and reopening #132.
- The cap is read from the account at each deploy, so a plan change reaches the check with no
  change to the code.

See `docs/ARCHITECTURE.md` ("Hosting").
