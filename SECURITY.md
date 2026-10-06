# Security policy

## Reporting a vulnerability

Report a vulnerability privately through
[GitHub security advisories](https://github.com/National-Digital/publicdata.au/security/advisories/new)
or by email to security@nationaldigital.com.au. Please do not open a public issue for anything
exploitable. The site publishes the same channels at
[`/.well-known/security.txt`](https://publicdata.au/.well-known/security.txt) (RFC 9116).

We aim to acknowledge a report within a few business days. Please include the steps to reproduce
it and the URL, endpoint or file affected.

## Safe harbour

We welcome good-faith security research. If you report privately through the channels above,
avoid privacy violations and the destruction of data, go no further than you need to show the
issue, and give us reasonable time to fix it before you disclose it, we will treat your research as
authorised and will not take or support legal action against you for it.

This is an independent public-interest project with no bug bounty. We cannot pay for reports, and
we are glad to credit researchers who want to be named. If you are unsure whether a test is in
scope, ask us first through a private channel.

## Scope

- The site and its Pages Functions in `functions/`: the query API, the MCP server, votes,
  requests and the R2 file layer.
- The build in `pipeline/` and the clients in `clients/`.
- The GitHub Actions workflows in `.github/`.

Out of scope: Cloudflare's own platform, which should be reported to Cloudflare, and volumetric
denial-of-service findings.

## What we especially care about

- A published file must be byte-for-byte what the build made from the stored snapshot, and its
  manifest's hashes must match it.
- Votes are stored under a salted hash and never tied to a person. Anything that links a vote to
  an address, a browser or another vote is a security bug.
- Nothing with a licence that is not open may be published. A way to get such a dataset past the
  gate is a security bug.
- The fetch runner takes only scheduled or maintainer-started runs of `fetch.yml` and
  `catalogue.yml` on `main`. A way to run other code on it is a security bug.

## Controls

- Workflows request the least token scope they need, default to read-only, and pin every action
  to a commit.
- Credentials are Actions secrets held in environments. The deploy's and the fetch's can be used
  only from `main`, and each token holds only the access its job needs.
- No credential, private hostname or internal address is committed anywhere in the repository,
  comments and commit messages included. Every push and pull request is scanned for secrets
  across the whole history.
- CodeQL and dependency review run on every pull request. Dependabot raises security updates.
- Changes to `main` go through a pull request with a maintainer's review and green checks. Only
  the fetch app's own data pull requests are approved by automation, under the conditions in
  `.github/workflows/data-pr.yml`, and only that app may push `data/` branches. A way for a person
  to get a pull request approved as the automation is a security bug.
