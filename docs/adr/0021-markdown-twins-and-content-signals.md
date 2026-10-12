# 0021: Pages have Markdown twins, and the site signals no to AI training

- Status: Accepted
- Date: 2026-10-13

## Context

Agents read the site's pages to answer questions about the data, and most of an HTML page is
layout an agent throws away. Some agents ask for Markdown by name in the `Accept` header, and others
guess a URL ending in `.md`.

The pages and their wording are the site's own, while the data in them is each publisher's under its
licence. `robots.txt` can state which uses of a site its owner agrees to, in the contentsignals.org
syntax, though the statement binds no crawler.

## Decision

- Every page but a dataset's embed page has a Markdown twin at `index.md` beside it, with the same
  facts as the page and front matter. The page links it as `rel="alternate"`.
- A request that names `text/markdown` in its `Accept` header gets the twin at the page's own URL,
  with `Vary: Accept`. Two zone Transform Rules do this before the request reaches Pages, and a test
  holds them to the site's routes. `functions/_middleware.js` redirects a guessed spelling such as
  `/x.md` to the twin.
- `robots.txt` allows every crawler everywhere but `/api/`. Its line
  `Content-Signal: search=yes, ai-input=yes, ai-train=no` says yes to search and to answers that
  read the site, and no to training. Every `.md` file is served with the same header. Each dataset's
  licence still governs what may be done with its data, and `robots.txt` and the agents page say so.

## Alternatives considered

- **Twins at `.md` URLs alone.** An agent that asks by `Accept` would get HTML.
- **Negotiation in a Pages Function.** Every page request, a browser's included, would run a
  function. The zone rules leave a browser's request to Pages' static files.
- **Saying yes to training.** An answer that reads a page names the version and the attribution the
  page gives, and a model trained on the pages repeats figures without either. The data stays open
  to training under each publisher's licence.

## Consequences

- The rules belong to the zone, so a maintainer applies them by hand in the dashboard and a
  `pages.dev` preview has no negotiation. They count against the zone's Transform Rules allowance.
- The rules read no quality values, so any `Accept` that names `text/markdown` gets Markdown and a
  browser's `*/*` gets HTML. A dated version's page always answers in HTML, since a zone Cache Rule
  caches that tree.
- A rewritten request runs the middleware once, since every `.md` path does.
- The signal is a preference. It binds no crawler and narrows no dataset's licence.

See `docs/ARCHITECTURE.md` ("Markdown at a page's URL") and the agents page (`/agents/`).
