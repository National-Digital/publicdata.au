# Working in this repository

Read `CONTRIBUTING.md` for setup, test and pull-request steps and `docs/ARCHITECTURE.md` for
the rules the build enforces. The points below are the ones no other file states.

- No credentials or private hostnames anywhere, including comments and commit messages.
- A comment states a constraint the code cannot show, in one or two lines of why.
- Site copy is a heading followed by plain sentences. The gate fails a page that uses
  seamless, streamline, empower, unlock, leverage or robust.
- The only analytics is the first-party Cloudflare Web Analytics beacon. No cookies, no other
  tracking. The CSP in `_headers` is the enforcement and the test suite checks the beacon.
