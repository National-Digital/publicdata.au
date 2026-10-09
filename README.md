# publicdata.au

[![CI](https://img.shields.io/github/actions/workflow/status/National-Digital/publicdata.au/ci.yml?branch=main&label=CI)](https://github.com/National-Digital/publicdata.au/actions/workflows/ci.yml)
[![CodeQL](https://img.shields.io/github/actions/workflow/status/National-Digital/publicdata.au/codeql.yml?branch=main&label=CodeQL)](https://github.com/National-Digital/publicdata.au/actions/workflows/codeql.yml)
[![Deploy](https://img.shields.io/github/actions/workflow/status/National-Digital/publicdata.au/deploy.yml?branch=main&label=deploy)](https://github.com/National-Digital/publicdata.au/actions/workflows/deploy.yml)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Data text: CC BY 4.0](https://img.shields.io/badge/register-CC%20BY%204.0-blue.svg)](LICENSE-DATA.md)
[![DCO](https://img.shields.io/badge/DCO-sign--off%20required-blue.svg)](https://developercertificate.org/)
[![PyPI](https://img.shields.io/pypi/v/publicdata-au)](https://pypi.org/project/publicdata-au/)

Australian government open data, republished as CSV, Excel, JSON, Parquet, SQLite, DuckDB, GeoJSON and GeoPackage at URLs
that never change. Every release the publisher makes becomes a dated version that keeps its
content, with its schema, provenance, the publisher's own file and a diff against the release
before.

This is an independent site run by National Digital. No government agency runs it, funds it or
has endorsed it. Each dataset is republished under the publisher's licence with the attribution
that licence asks for.

## Contributing

Contributions are welcome from anyone. A dataset from the [backlog](https://publicdata.au/backlog/)
is one YAML file in `register/`, and a new file format is one writer in
`pipeline/publicdata/serialise/writers/`. [CONTRIBUTING.md](CONTRIBUTING.md) has the steps for
those and for adapters, fixes and the clients, and [publicdata.au/contribute](https://publicdata.au/contribute/)
gives the overview. Every dataset page links to the register entry it is built from.

Taking part means agreeing to the [Code of Conduct](CODE_OF_CONDUCT.md). [SUPPORT.md](SUPPORT.md)
says where to ask for a dataset or for help, and a vulnerability is reported privately as
[SECURITY.md](SECURITY.md) describes. [GOVERNANCE.md](GOVERNANCE.md) says who decides, and
[AGENTS.md](AGENTS.md) is for coding agents.

## Layout

- `register/` one YAML per dataset: source, licence with evidence, attribution, field allow-list,
  key, partitions, status. Backlog and blocked entries live here too.
- `store/` one manifest per fetched version. Bytes are in R2 and pulled by hash before a build.
- `pipeline/` the Python build: `publicdata register validate | fetch | build | gate | split |
  store pull|push | dist-push | shards | purge`.
- `functions/` Cloudflare Pages Functions: votes, requests, saved dashboards, the query API,
  `latest/` redirects, R2 fallback.
- `explorer/` and `package.json` the explorer's browser libraries, copied into the build.
- `clients/` the packages that call the site from other languages: `clients/python` is
  `publicdata-au` on PyPI and `clients/r` is `publicdataau` for CRAN. They take any slug, so a new
  dataset needs no release.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) the rules that decide the code.
- [`docs/CORRECTIONS.md`](docs/CORRECTIONS.md) how a fault in a published file is reported and
  fixed, and the log of every correction.
- [`docs/content-addressed-store.md`](docs/content-addressed-store.md) what storing identical
  files once would save, measured, and why it waits.
- [`docs/ACCESSIBILITY.md`](docs/ACCESSIBILITY.md) the WCAG 2.2 AAA target, what CI enforces and the exceptions.

## Run it

`CONTRIBUTING.md` sets up a working copy, builds the fixtures, fetches a real dataset and walks
through the routine changes. No credentials are needed for any of it.

## Deploy

`.github/workflows/deploy.yml` builds, gates, splits, pushes large files to R2 and deploys to the
Pages project `publicdata-au` on every push to `main`, with a preview per pull request from this
repository. Credentials sit in GitHub environments. `production`, which only `main` can use, holds
the deploy's and the hubs'. `fetch`, also `main` only, holds a token that can write the raw store and
nothing else. `preview` holds one that can deploy a preview and read the raw store.

`.github/workflows/fetch.yml` reads every live source each Tuesday and the live feeds every other
day, and opens one pull request per government whose sources changed.
`.github/workflows/catalogue.yml` harvests the portals' dataset lists. Some portals turn away
cloud addresses, so both run on a runner of our own in Australia. Its runner group admits only
those two workflows on `main`, and they start only on their schedule or when a maintainer runs
them. Both open their pull requests as the GitHub App "publicdata.au fetch", whose key is a secret
only those environments hold. A ruleset lets only that app create or push `data/` branches, so no
person can open a pull request as the automation. `.github/workflows/data-pr.yml` approves those pull requests and nothing else.

## Licence

The code is licensed under the GNU Affero General Public License v3.0 or later (`LICENSE`). If
you run a modified copy as a service, you must offer its users the source of your changes. The
clients in `clients/` are under the MIT licence in each package's `LICENSE`.

The publicdata.au name and mark are not covered by the code licence; [`BRAND.md`](BRAND.md) says what a copy
must change.

The data is not ours to license. Each dataset is published under its publisher's licence, which
its register entry, its manifest and every file name, with the attribution that licence asks for.
The text we write for the register is under CC BY 4.0 ([`LICENSE-DATA.md`](LICENSE-DATA.md)). Test extracts, the
brand typeface and the explorer's libraries are listed in [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md); the typeface
is RandomMaerks' free package, redistributed under its own licence.
