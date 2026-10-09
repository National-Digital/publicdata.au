# 0021: Every page meets WCAG 2.2 AAA, and every preview meets the Lighthouse targets

- Status: **Accepted** (AAA held in CI: #84; Lighthouse on every preview and PageSpeed Insights on
  production weekly: #99)
- Date: 2026-10-09
- Deciders: National Digital

## Context

Readers said the type was too small on large screens. The accessibility job then tested WCAG 2.2 AA
with axe at one viewport, and every size in the stylesheet was a fixed pixel value, so a 2560px
screen got the same 13.5px grey text and a wider margin. An audit of the live site against AAA in
October 2026 found 88 nodes under 7:1 contrast, paragraphs of 95 to 198 characters a line,
hundreds of targets under 44px, links named only "csv" or "download", no expanded abbreviations,
skipped heading levels, and two failures of AA itself.

PageSpeed Insights scores had never been checked, and a page's speed, SEO and agentic browsing
audits can slip in any change to the templates.

## Decision

- Every page meets WCAG 2.2 AAA. Dense data regions (tables, dataset listings, the explorer) are
  held to AA for target size and type floor, and are marked as such in the markup.
- CI holds what a machine can test: axe's AAA rules in both colour schemes, and house checks for
  type size, line length, targets, link purpose, focus, reflow, text spacing and the narrow-screen
  menu at three widths, each check proved to fire on a page built to fail it. The gate holds every
  abbreviation in the site's prose to the glossary.
- `docs/ACCESSIBILITY.md` records the target, the checks, the exceptions and how each criterion
  with no machine test is met, and the public statement at `/accessibility/` follows it.
- Every pull request's preview is run through Lighthouse on six kinds of page, mobile and desktop,
  and held to performance 90 or more on the median of three runs and to every audit in
  accessibility, best practices, SEO and agentic browsing. The job is not yet a required check. A
  weekly PageSpeed Insights run holds production to the same targets, runs a page that misses
  once more, and opens an issue when it misses again.

## Alternatives considered

- **WCAG 2.2 AA**, the usual target and what the job tested before. It left the faults readers
  reported, and the audit showed the gap to AAA was a set of fixable defaults: type in rem that
  grows with the screen, a measure of 80 characters, 7:1 colours and 44px targets.
- **Lighthouse CI and its GitHub Action.** Their newest release bundled a Lighthouse with no
  agentic browsing category, so the job runs a pinned Lighthouse directly.

## Consequences

- A change to the templates, the stylesheet or the colours has to pass AAA in both schemes before
  it merges, and the 7:1 contrast rule changed the brand's button colour.
- The explorer's third-party viewer, publishers' own wording and files, and maps are named as known
  limitations on the statement, which claims no full conformance.
- The preview is served noindex, so the crawlability audit is held on production only, and the
  explorer page is exempt from it because it is noindex by design.
- The weekly run needs an API key, held in its own environment so that only that job can read it.

See `docs/ACCESSIBILITY.md` and `docs/ARCHITECTURE.md` ("Rules that decide the code").
