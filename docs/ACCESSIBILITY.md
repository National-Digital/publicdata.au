# Accessibility

The site is held to WCAG 2.2 level AAA, with one stated exception for dense data regions, which are
held to level AA. CI enforces what a machine can test and this page records how the rest is met, so
the standard does not drift as pages change.

The public statement at `/accessibility/` (in `site.py`'s `PROSE`) says the same in plain words and is held to this page: a check added or removed here changes it too.

## What the gate enforces

`scripts/a11y.mjs` runs in CI over a fixture build of every kind of page, in light and in dark, and
fails the pull request on any of the following. `scripts/a11y.test.mjs` first proves each house
check fires on a page built to fail it, as every gate in this repo must.

| Check | Criterion | Rule |
| --- | --- | --- |
| axe-core, AAA tags | every rule axe has through WCAG 2.2, including 1.4.6 enhanced contrast at 7:1, its best practices (landmarks, heading order) and its experimental rules (2.5.3 label in name, 1.3.1 headings and table headers, 1.3.4 orientation, focusable roles); `hidden-content` is off because it can only ask for review | no violation of any impact |
| needs review | every criterion axe tests | a result axe cannot decide fails the gate unless the painted-contrast check settles it or an exception below covers it |
| painted contrast | 1.4.3, 1.4.6 | where axe cannot name one background (a gradient, a pseudo-element, an overlapping element, a chart), the node is captured with its text and without it, and the text colour must clear 7:1, or 4.5:1 for large text, against every pixel behind its glyphs; a halo drawn as the text's own stroke counts as background |
| type floor | 1.4.4, 1.4.8 | text a person reads (paragraphs, lists, cells, links, controls, headings) is at least 0.875rem; every other visible text at least 0.75rem; inside a dense data region, 0.75rem |
| root scale | 1.4.4, 1.4.8 | the root font size grows with the viewport: at least 16px at 1280px wide and 19px at 2560px, so a wide screen gets larger type, not a wider margin |
| measure | 1.4.8 | no paragraph, list item or definition averages more than 80 characters a line |
| target size | 2.5.5, 2.5.8 | every link, button, field and disclosure is at least 44 by 44 CSS pixels, or 24 by 24 inside a dense data region; a link in a sentence is exempt, as the criterion allows; a checkbox is measured by its label |
| link purpose | 2.4.9 | no link is named only "csv", "download", "open", "more" or another word that does not say where it goes; format links carry the dataset's name in `aria-label` |
| focus visible | 2.4.7, 2.4.13 | every focusable element shows an outline of at least 2px when focused |
| reflow | 1.4.10 | no page scrolls sideways at 320px wide |
| menu | 2.1.1, 2.4.3, 4.1.2 | at 320px wide, the header's menu button opens from the keyboard with `aria-expanded` set and its links pass the target, name and focus checks; Escape from inside the menu closes it and returns focus to the button; with script turned off, every site link is shown |
| text spacing | 1.4.12 | with line height 1.5, letter spacing 0.12em, word spacing 0.16em and paragraph spacing 2em forced, nothing is cut off and the page does not scroll sideways |

The Python gate (`python -m publicdata gate`) adds, on every built page including production
builds, the abbreviation rule below, and `publicdata register validate` applies it to every part
of a register entry a page shows as prose (title, summary, description, search title, collection
copy, questions and answers, sample label and notes), so a new abbreviation is caught in the pull
request that introduces it.

Every size in the stylesheet is in rem, never px, so a reader's own browser font size is honoured,
and the root size is `clamp(100%, .75rem + .35vw, 125%)`: the browser default on a phone or laptop,
a quarter larger on a 2560px screen. The content column is 70rem and grows with it; prose is capped
at 65ch.

## Exceptions

`scripts/a11y-exceptions.json` lists the results a person has judged acceptable. Each entry names
one rule, a container selector and a page pattern where `*` stands for one path segment, and says
why and when it was reviewed. An entry that no longer matches anything fails the gate, so the list
cannot outlive the markup it describes. The one entry today is the explorer's placeholder, a
dimmed, `aria-hidden` preview shown until the explorer loads, which as inactive text has no contrast
requirement.

## Dense data regions, held to AA

A table of many rows, a listing of many datasets or an interactive data explorer cannot give every
link a 44px target or every label 14px type without ceasing to be a table or a listing. These
regions carry `data-conformance="aa"` in the template and are held to WCAG 2.2 AA for target size
(2.5.8, 24px) and to the 0.75rem type floor. Everything else about them, including contrast, names,
focus and reflow, is held to AAA like the rest of the page. The regions are:

- every scrollable table wrapper (`.tbl`): schema tables, sample rows, catalogue and backlog lists;
- dataset card listings (`.cards`) on the home, topic and government pages, and the ticker of newest
  versions on the home page;
- the lists of places a dataset is partitioned by (`.places`) and of datasets coming next (`.tcoming`);
- the explorer (`.xhost`, a third-party component rendered by Perspective) and the dashboard preview
  that links to it (`.dash`).

Adding a region is a template change reviewed like any other; the attribute is the record of the
decision, and this list is updated with it.

## Criteria met by design and reviewed by hand

These AAA criteria have no reliable machine test. The site meets them as follows, and a change that
touches them is reviewed against this list.

- **1.4.8 Visual presentation.** Text is never justified. Line height is 1.55 and paragraph spacing
  larger than the line height. Colours come from custom properties on `:root` with no `!important`,
  so a reader's own stylesheet or forced-colours mode wins, and both colour schemes are first-class.
- **1.4.9 Images of text.** None. Maps are drawn, not photographed, with an alt worked out from the
  cells; the wordmark is text.
- **2.2.3, 2.2.4, 2.2.6 Timing and interruptions.** Nothing is timed. The only live region is the
  copy-to-clipboard toast, which is `role="status"` and never steals focus.
- **2.3.3 Animation from interactions.** The only animation is the explorer's busy indicator, which
  stops under `prefers-reduced-motion`.
- **2.4.10 Section headings.** Every page has one `h1`, sections have `h2`, and axe's heading-order
  rule fails a skipped level.
- **2.4.12 Focus not obscured (enhanced).** Nothing is sticky over content except a table's header
  row inside its own scrolling region.
- **3.1.3 Unusual words, 3.1.4 Abbreviations.** Every abbreviation the site's own prose uses is in
  the glossary at `/glossary/`, linked from every footer, and the gate fails a page or a register
  entry that uses one the glossary does not hold and the page does not expand inline. A publisher's
  own words (titles and descriptions quoted from a portal, cell values, codes) are not rewritten and
  are outside the rule, as are code samples. A template that sets a publisher's value inside the
  site's own sentence, such as a place name or a chart's series, marks it `data-quoted` so the
  gate reads past it.
- **3.1.5 Reading level.** Copy follows the house language rules: a heading then plain sentences, no
  reversals, fragments or superlatives, and the gate's banned-word list. A dataset's caveats are
  written as questions a reader would ask.
- **3.2.5 Change on request.** No control navigates or submits on change; selects and checkboxes
  redraw the panel they sit in. The header search submits on Enter or the button.
- **3.3.5 Help.** The two forms, search and dataset request, each say what to type beside the field.
- **3.3.6 Error prevention (all).** The only submissions are a search and a vote, both reversible.

## Running it

```sh
python -m publicdata build --fixtures --out dist
node --test scripts/a11y.test.mjs
node scripts/a11y.mjs dist                 # the fixed set of pages
node scripts/a11y.mjs dist /about/ /qld/   # chosen pages
```

Chrome is read from `CHROME_PATH` or `/usr/bin/google-chrome`.
