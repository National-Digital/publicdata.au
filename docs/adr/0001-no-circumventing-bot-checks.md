# 0001: Publishers' bot checks are respected, and blocked files are fetched by hand

- Status: **Accepted** (the initial public release)
- Date: 2026-10-04
- Deciders: National Digital

## Context

A few publishers serve their files from hosts that turn automated clients away, with a browser
check or a refusal. The portal record that lists each file is still readable, so the fetch can see
when a file has changed but cannot download it.

A fetch that got past those checks would keep these datasets current with no one involved. It would
also mean a site that serves government data getting around controls that government hosts chose to
put in place.

## Decision

The fetch does not get around a host's bot check. It has no headless browser, and the user agent it
sends names the site: `publicdata.au/1.0 (+publicdata.au/about/)`.

A source whose file host turns automated clients away sets `manual: true` in its register entry.
The weekly fetch reads its portal record, and when the record shows a new file it lists the dataset
in the open issue **Manual downloads due**. A maintainer downloads the file in an ordinary browser
and hands it to the fetch, which reads the portal record and the licence as usual and takes the
bytes from that file. When a publisher allows the fetch's address, its entries drop `manual: true`
and go back to the weekly run.

## Alternatives considered

- **A headless browser.** It would keep these sources current unattended.
  It was rejected because it overrides a choice the publisher made about its own host, and a site
  that depends on publishers' goodwill cannot do that.

## Consequences

- A manual source can lag its publisher until a maintainer clears the issue. In October 2026 the
  register had 16 manual entries: 9 from Queensland, 6 from Victoria and 1 from the Northern
  Territory.
- Each manual download needs a person with store credentials to push the raw bytes.
- An empty body or an HTML page in place of a data file is refused (`expect_page`), so a blocked
  download cannot become a version by accident.
- The list shrinks only when a publisher allows the fetch's address. Requests to allow it are not
  recorded in the repository. Requests for permission to publish are recorded on the dataset's
  register entry ([0020](0020-licence-is-data.md)).

See `CONTRIBUTING.md` ("Manual sources") and `docs/ARCHITECTURE.md` ("Raw store").
