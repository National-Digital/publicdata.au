# Contributing

publicdata.au republishes Australian government open data as dated versions that keep their
content. Contributions are welcome: a new dataset, a fix to an entry, a new output format, an
adapter for a portal we cannot read yet, or a bug fix. This guide covers the routine changes step
by step, then the rules every change is held to.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Report security issues
privately as [SECURITY.md](SECURITY.md) describes.

Questions and ideas that are not a fault go in
[Discussions](https://github.com/National-Digital/publicdata.au/discussions).

## Ground rules

The build enforces the project's rules, so a change that breaks one fails its checks.
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#rules-that-decide-the-code) states them and explains
the code they shape, and its [Archive](docs/ARCHITECTURE.md#archive) section says what may change
once a version is published.

## Set up

You need Python 3.14, Node 24 and `uv`. No credentials are needed to build, test or fetch.

```
git config core.hooksPath .githooks   # sign-off, lint and test hooks (see Git hooks)
uv venv .venv && . .venv/bin/activate
uv pip install -e './pipeline[dev]'
npm ci --ignore-scripts
cd pipeline
python -m publicdata register validate
python -m publicdata build --fixtures --out /tmp/pd
python -m publicdata gate /tmp/pd
pytest -n auto
cd .. && node --test functions/*.test.mjs scripts/*.test.mjs
```

The fixtures are small and stand in for the real store. To build a real dataset, fetch it from
the publisher into a local store and build from that:

```
python -m publicdata fetch <slug> --store /tmp/store
python -m publicdata build <slug> --store /tmp/store --out /tmp/pd
python -m publicdata gate /tmp/pd
```

The raw bytes of every published version are kept in a private bucket that only the deploy reads.
The manifests in `store/` record each one's URL, hash and fetch time.

The site's typeface, Random Grotesque, is in `pipeline/publicdata/static/fonts/` under its own
licence, not the AGPL. Only styles from its free package may be added there; see
`THIRD-PARTY-NOTICES.md`.

## Git hooks

The `core.hooksPath` line in Set up switches on three hooks in `.githooks/`. They catch on your
machine what CI would fail a few minutes later, and they replace no CI check.

- `prepare-commit-msg` adds the DCO `Signed-off-by` trailer to every commit, once.
- `pre-commit` runs `ruff check` and `ruff format --check` on the staged content of each staged
  Python file under `pipeline/` or `clients/python/`. It runs from that file's directory, so each
  takes the settings CI uses for it. It names each file and rule that fails and stops the commit. A
  commit with no staged Python file in either directory runs no check. It takes well under a
  second.
- `pre-commit` also checks the files the Workflow lint job reads, when a commit stages one of
  them: `.github/workflows/`, `.github/actions/`, `.github/actionlint.yaml` and
  `.github/dependabot.yml`. A change to a template, `CODEOWNERS` or the lint fixture runs no
  check. It writes those staged files to a temporary directory, so an unstaged edit cannot hide a
  fault, and runs actionlint with shellcheck and
  `zizmor --offline --persona auditor --strict-collection` over that copy, so a workflow zizmor
  cannot parse fails too. It names each file and rule that fails and stops the commit once the
  Python check has run. Offline, zizmor leaves out the audits that ask GitHub: `impostor-commit`,
  `ref-confusion`, `known-vulnerable-actions`, `stale-action-refs` and `ref-version-mismatch`.
  The Workflow lint job in CI runs those too, so a commit the hook passes can still fail there.
  Each tool should be the version `.github/workflows/ci.yml` pins; the hook warns, naming both
  versions, when one differs, and runs it anyway.
- `pre-push` runs the fast tests in the working tree: `pytest -m "not slow" -n auto` in `pipeline/`
  and `node --test functions/*.test.mjs scripts/*.test.mjs`. It stops the push when a test fails.
  It should take under a minute on a laptop.

The fast tests are every test that is not marked `slow`. `pipeline/tests/conftest.py` decides
which tests are slow: those that use the fixture store or a fixture site build (`SLOW_FIXTURES`)
and those named in `SLOW_TESTS`. Move a test in or out of the fast run there. CI runs every test.

A hook whose tool is missing prints one line saying what it skipped and lets the commit or push
through: `ruff` for `pre-commit`, `pytest` or the activated virtual environment for the Python
tests, and `node` for the JavaScript tests. The workflow check is the exception: a commit that
stages `.github/` fails when actionlint, shellcheck or zizmor is missing, with a line naming the
version CI pins and where to get it. To skip the hooks once, pass `--no-verify` to `git commit` or
`git push`.

CI's pipeline job puts the same pinned actionlint, shellcheck and zizmor on its PATH, so the
hook's tests in `pipeline/tests/test_hooks.py` run there. Locally they skip when a tool is not
installed.

## Private copies

Workflows with side effects (deploy, fetch, catalogue, hubs, publishing the clients, approving
data PRs) run only in `National-Digital/publicdata.au`. In a fork or a private copy, only the
checks run: tests, sign-off, PR title and secret scanning. `pipeline/tests/test_repo.py` fails if a
new job with side effects lacks that condition. Renovate opens dependency pull requests only in a
repository its GitHub app covers. It skips forks, and a private copy gets them only if the app is
installed on it; delete `renovate.json` there if you do not want them.

## Commits and pull requests

- Sign off every commit (`git commit -s`). This is the
  [Developer Certificate of Origin](https://developercertificate.org/): you certify that you wrote
  the change or have the right to submit it under the project's licence. The DCO check fails a pull
  request with an unsigned commit; `git rebase --signoff main` fixes it.
- Title the pull request as a Conventional Commit, `<type>(<scope>): <subject>`. The types are
  `feat fix docs chore perf refactor test build ci style revert data`; `data` is for register
  entries and store manifests. The squash merge makes the title the commit subject on `main`, and
  the title decides the release number (see Versioning).
- Keep one concern to a pull request. An unrelated fix found on the way gets its own.
- Say in the description what changed and why, what you ran to check it and what you could not
  check. The template asks. Name anything a maintainer must do beyond merging, such as a secret, a
  manual download or the versions a `replace` deploy must rebuild.
- A pull request merges when a maintainer has approved it and every required check is green:
  tests, the register, the build and gate, the clients, secret scanning, CodeQL, dependency review,
  the DCO and the title. Pull requests are squash-merged.
- The fetch's own data pull requests are approved by `.github/workflows/data-pr.yml`, only when
  the fetch app opened them from a run on `main`, with one signed-off commit of store manifests.
  Only the app may push `data/` branches. They merge themselves when their checks are green.
  Every other pull request, a person's change to `store/` included, needs a maintainer.
- A change under `.github/` must pass the Workflow lint job in `ci.yml`. It runs zizmor at its
  auditor persona, which reports every finding zizmor has, and actionlint with shellcheck over every
  workflow's `run:` blocks. actionlint does not read the composite action in `.github/actions/`, so
  its steps get zizmor alone. The `pre-commit` hook runs both offline (see Git hooks). The online
  audits need a token, so run zizmor with one before you push, at the versions that job pins:

  ```sh
  GH_TOKEN=$(gh auth token) uvx zizmor==1.30.1 --persona auditor --strict-collection .github
  actionlint -shellcheck "$(command -v shellcheck)"
  ```

  Fix each finding. Where a rule truly cannot apply, a `# zizmor: ignore[<rule>]` comment on the
  line it covers, or a `# shellcheck disable=<code>` comment on the line before, gives the reason.

## Reviewing a pull request

- Read the whole changed file and the code that calls it before judging a line. Comment only on
  what the change adds or makes worse.
- Raise only genuine issues. Each one gives its file and line, the input or state that breaks it,
  and a fix or the existing code to use instead. A doubt you cannot settle is marked as
  unconfirmed, with what would settle it. When there is nothing to raise, a maintainer approves.
- A test must be able to fail. Flag a test that restates the implementation, mocks the thing it
  tests or asserts nothing that matters, and say what it should assert.
- Ask why any new `noqa`, `type: ignore` or skipped test is needed. Leave formatting and lint to
  CI.
- Check the change against the [rules the build enforces](docs/ARCHITECTURE.md#rules-that-decide-the-code),
  and check that the docs describing the changed behaviour were updated.
- Read the earlier reviews first, so a point already resolved is not raised again.

## Site copy

Site copy states facts and gives the figure behind any comparison; it makes no absolute or
superlative claims. [`BRAND.md`](BRAND.md#typography) sets the typography the gate checks.

## Versioning

publicdata.au has four kinds of version, and each has its own rule.

- **Datasets** are versioned by date, not by number. A version is named for the day the publisher
  changed the source, and an unchanged source makes no version. The
  [Archive](docs/ARCHITECTURE.md#archive) section says what may change once one is published.
- **The site, API and MCP server** follow [semantic versioning](https://semver.org/) through
  release tags (`v2.21.1`). Each merge to `main` releases one: `feat` raises the minor number,
  any other type the patch number, and a breaking change, marked with `!` after the type
  (`feat(api)!: ...`), raises the major. `scripts/next-version.mjs` works out the number and the
  deploy tags it, cuts a GitHub release and updates the MCP Registry entry.
- **The query API** is also versioned in its path, `/api/v1/`. A change that would break a v1
  caller goes into `/api/v2/`, and v1 keeps answering for at least twelve months after v2 ships,
  with the date it stops given in its responses and on the API page.
- **The clients**, `publicdata-au` on PyPI and `publicdataau` on CRAN, follow semantic versioning
  of their own, set in `clients/python/pyproject.toml` and `clients/r/DESCRIPTION`. While they are
  0.x a minor release may break a caller and says so in `clients/r/NEWS.md` and the release
  notes. From 1.0 only a major release may break one. Both take any slug, so a new dataset needs
  no client release.

The pipeline package in `pipeline/` is not published, so it makes no versioning promise.

## Pick up a dataset task

Some of the most-wanted datasets have an issue labelled `good first issue` and `dataset`, with the
portal page, the licence and its evidence, the vote count and a starting register entry. Say on the
issue that you are taking it, follow Add a dataset below, and write `Closes #<issue>` in the pull
request.

`.github/workflows/contribute.yml` keeps these issues each day with `python -m publicdata
contribute sync`. It opens them, most voted first, for backlog entries whose licence is open and
for catalogue records with an open licence, a file and at least `CONTRIBUTE_VOTES` votes (1 when
unset), and never leaves more than `CONTRIBUTE_CAP` (10 when unset) open. Both are repository
variables. An open issue keeps its place when another dataset gains votes; when the cap is
lowered, the least wanted close first and an entry already `building` closes last.

An issue is written only from the register and the catalogue's own record; a vote adds only its
count. The sync changes only issues it opened, finds them again by the key in a hidden marker,
updates their text, and closes one when its dataset is live, when its licence or status takes it
off the list, or when it falls outside the cap. A dataset whose issue was closed does not get
another; a maintainer reopens the old one instead. `--dry-run` prints what a run would do and
changes nothing. Only the workflow runs it without, because the next run reads only the issues the
workflow's own account opened. The backlog and the publisher's page link an open issue from the
deploy after the sync opens it until the deploy after it closes; the catalogue search does not.

## Add a dataset

1. Find the dataset on its publisher's portal and read the licence. It must be open (CC BY, CC0 or
   an open grant the publisher states), and the evidence must be the publisher's own words. A
   non-commercial or no-derivatives licence, or none, means the dataset cannot be published.
2. Draft the entry: `python -m publicdata register draft <portal dataset url>`. It writes
   `register/<slug>.yaml` with the publisher, licence and attribution from the portal and every
   field typed from a sample of the file, at status `building`.
3. Finish the entry by hand. Write the title, `search_title`, description and the TODOs the draft
   left. Keep only the fields that should be published (the allow-list), set `key`,
   `partition_by` and `sort` where they apply, and give geometry as Reference below describes. Set
   `licence.reviewed` to the day you read the licence.
4. Label the fields: `python -m publicdata register labels <slug> --write`, then read the drafts.
5. Fetch and build it locally as Set up shows, and run the gate. The gate prints the page's
   example query; if it reads poorly, set `example` and `chart` in the entry.
6. Set `status: live` and open a pull request titled `data(register): add <what it is>`. Say where
   the licence evidence is and what you checked.
7. The Storage cost check projects what the entry adds in a year: the bytes one version stores
   in R2 times the versions its cadence implies, plus a rebuild of every stored version when an
   edit changes what a version publishes, and the rows its versions write to D1, once for the
   table and once for each index. An entry over 5 GB or 10,000,000 D1 rows a year fails it until
   a maintainer other than the pull request's author adds the `cost-approved` label. The label
   approves the commit it was added on: a later push, reopening the pull request or changing its
   base needs it added again. The check runs the base branch's code against the pull request's
   register, so a change to the check takes effect once it is merged. Run
   `python -m publicdata cost <slug>` to see the figures first.

A dataset that cannot be published yet keeps its entry at `backlog`, `assessing` or `blocked`, with
the reason, so the site can say why.

## Change or fix a dataset entry

Edit `register/<slug>.yaml` and open a `fix(register): ...` pull request. A field the publisher
renamed, a resource that moved or a header that changed row are the usual causes. The fix applies
from the next version. When a fault in our conversion or a wrong attribution has already reached
published versions, those versions are rebuilt as [docs/CORRECTIONS.md](docs/CORRECTIONS.md)
describes. An edit to `partition_by` changes the `by/` files of every version already published,
so it is a correction too, and the deploy's plan fails it until each of those versions' manifests
carries a note ([A change to `partition_by`](docs/CORRECTIONS.md#a-change-to-partition_by)).
When the publisher changes its licence, the fetch stops that dataset until a
person has read the new licence and updated `licence` and `licence.reviewed`.

## Manual sources

A few publishers' file hosts turn automated clients away. publicdata.au does not get around that.
Their entries set `manual: true`, and a person downloads the file instead.

- The weekly fetch still reads the portal's record for each manual source, which those hosts
  allow. When the record shows a new file, the fetch lists it in the open issue **Manual
  downloads due** and moves on.
- To clear one, a maintainer downloads the file from the URL in the issue, in an ordinary browser,
  and runs:

  ```
  python -m publicdata fetch <slug> --file <slug>=<path to the download>
  ```

  The fetch reads the portal's record and licence as usual and takes the bytes from the file. For
  a `ckan-stack`, download every file the portal lists into one folder, keeping the names the
  links give them, and pass the folder.
  Then push the raw bytes (`python -m publicdata store push`, maintainers only) and open a
  `data(<jurisdiction>): ...` pull request with the new manifest.
- We ask each of these publishers to allow the fetch's address. When one agrees, its entries drop
  `manual: true` and go back to the weekly run.

## Add a source adapter

An adapter reads one kind of portal or file host. Add a function to `pipeline/publicdata/fetch.py`
that takes the entry and a store directory and returns the bytes, the manifest and the licence it
read, and register it in `ADAPTERS`. It must:

- read the licence from the publisher on every run and record `read_from` and `read_at`;
- date the version by the publisher's own change date, never by the fetch time, except for a live
  feed;
- refuse an empty body or an HTML page where a data file should be (`expect_page`);
- identify itself with the fetch's user agent and never pose as a browser;
- come with tests in `pipeline/tests/test_fetch_adapters.py` against recorded responses, including
  the ways it fails.

Document the adapter's register fields in Reference below.

## Add a serialisation

Each output format is a pure writer: one normalised table and the provenance header in,
deterministic bytes out, with no network, clock or randomness.

1. Add the writer to `pipeline/publicdata/serialise/writers/` and register it in `WRITERS` in
   `serialise/__init__.py`, with its place in `FORMATS` (or the geo lists), its media type in
   `MEDIA` and its name in `FORMAT_LABEL`. The order of `FORMATS` is the order the site lists them.
   `LEGACY_FORMATS` is the set of versions whose store manifest has no `caps` stamp; add it there too
   only if those versions should carry it. A format whose file grows past what people can open
   takes a limit in `CAPS`, measured on the NDJSON, the CSV or its own file, and the version pages then say
   why it is missing.
2. Carry the provenance header into the file in whatever way the format allows, as the other
   writers do.
3. Describe the format in `pipeline/publicdata/api.json`, which the pages, the API and the MCP
   tools all read their text from, and run `python -m publicdata.api_text` to regenerate the tool
   file.
4. Add tests that the file reads back to the same rows and types, and that two builds of one
   snapshot give identical bytes.
5. A new format is added to every version at the next deploy, and the old versions' other files
   are left as they are. Open the pull request as `feat(serialise): ...`.

## Change the build code

A deploy reuses every version it has built before while the version's inputs are the same: its
source, its register entry with the wording of its licence grant, its manifest, the register
entries of the spine layers it joins, the JSON and GeoJSON writers, the Python and library
versions, and two rebuild numbers. A dataset with geometry or a spine join is also keyed on the
DuckDB spatial extension, and a database on `database.py`. The rest of the build code under
`pipeline/publicdata/` is not in that key, so an edit to `normalise.py`, `build.py` or any other
module the build imports leaves every published version as it was.

- When your change alters what a version's files hold, raise a rebuild number in the same pull
  request. Add or raise `rebuild:` in the register entry of each dataset it affects, which starts
  at 0, or raise `REBUILD` in `pipeline/publicdata/cache.py` when the change reaches datasets you
  cannot list. Say in the pull request which number you raised and why.
- A change to a format writer under `serialise/writers/` needs no number. Each writer has a key of
  its own, and the deploy writes that format again into every version.
- Changing the default of an existing register field needs `REBUILD`, since a version's key leaves
  out every field at its default. The deploy's plan fails such a change without it. Adding a new
  field with a default needs no number.
- The deploy checks every change to the build code against real data (`publicdata verify`). It
  builds a sample of stored datasets from their sources, at least one of each adapter and shape
  that fits its budget, every dataset whose `rebuild` the change raises, and one of the largest
  datasets in turn. Of a large dataset it builds the newest versions that fit in 60 MB of source.
  It compares each version, its query copy, diff and history archive a deploy would reuse with
  what was built before. A difference fails the deploy and names the dataset, the version and the
  file. When more than one sampled dataset differs, raise `REBUILD`: the sample is a part of the
  store, and raising only the entries it names leaves the rest reused with their old files. A
  push to main is checked against the last release, and the sample stays the same until the code
  changes or a release moves past it, so a change is checked again until a deploy passes. No
  version is built or pushed to R2 until the check passes.
- A pull request from a fork has no access to the store, so its change is first checked on the
  push to main. A failure there stops every deploy, the daily data updates included, until a
  maintainer raises the number or reverts the change. To check datasets beyond the sample,
  pull their sources and cache entries from R2 and run
  `python -m publicdata verify run <slug>... --cache <cache> --published r2://publicdata-dist`.
- A raised number builds the dataset's versions, diffs and history again, so the pages and the
  cache follow the new code. A dated file already in R2 is still never overwritten; replacing the
  published files is the `replace` dispatch under Withdraw a dataset or correct published files.

## Toolchain versions

Every tool and library CI, the deploy and the fetch runner use is pinned to an exact version, and
each version lives in one file that the workflows or the pipeline read:

| What | File |
|---|---|
| Python | `.python-version` |
| Node | `.node-version` |
| uv | `uv.toml` |
| The pipeline's Python packages | `pipeline/requirements.txt` (a constraint on every install) |
| The Python client's CI packages | `clients/python/requirements.txt` |
| npm packages, wrangler, and the Chrome build the accessibility check runs (from `puppeteer-core`) | `package-lock.json` |
| GitHub Actions | the commit SHA in each `uses:` |
| Runner image | `ubuntu-24.04` in each `runs-on:` |
| R, its CRAN snapshot date and the R client's lint tools | `.github/workflows/clients.yml` |
| DuckDB's spatial extension | `pipeline/publicdata/spatial-extension.json` |
| gh on the fetch runner, gitleaks, and mcp-publisher, each with its SHA-256 | `fetch.yml` and `catalogue.yml`, `secrets.yml`, `deploy.yml` |
| zizmor, actionlint and shellcheck | `.github/workflows/ci.yml` |

An upgrade is a pull request of its own. Renovate (`renovate.json`) raises every version in the
table but the last two rows on the first of each month, to exact versions only, with one pull request
per group and each major raise in a pull request of its own. The groups are the Actions, the
runner image, the pipeline's packages, the client's packages, the npm packages, R and its
snapshot, and duckdb with the spatial extension's pin; Python, Node and uv each get their own.
Security fixes come as soon as GitHub's advisories name them. The last two rows' tools are raised
by hand, since a raise needs the new hash. A runner image raise must also change the Ubuntu codename
in the R snapshot's URL in `clients.yml`. Check a change to `renovate.json` with
`npx --yes --package renovate@44.143.0 -- renovate-config-validator --strict`.

The Python version and the keyed libraries (pyarrow, duckdb, xlsxwriter, openpyxl, xlrd, pmtiles)
are in the build's cache key, so raising one rebuilds every version, about four hours on main.
Merge such a pull request on a day with no data pull request due.

DuckDB serves its spatial extension for each release and can replace it within one, so we keep a
copy of the build we use in R2, under `_toolchain/` in `publicdata-raw`, named by the DuckDB
release, the platform and the build. `spatial-extension.json` pins its URL and SHA-256.
`python -m publicdata spine install` takes our copy when it has the R2 credentials, as the deploy
does, and otherwise the same bytes from DuckDB, as CI and a working copy do. It stops when the hash
differs or when the pin is for another DuckDB than the one installed. The datasets that load the
extension are keyed on its build, and every job of a deploy checks that it has the same build as
the plan.

A raise of `duckdb` needs the extension for the new release. Renovate raises the release in the
pin with the package, and its checks fail, naming the workflow, until the rest of the pin follows:
a maintainer runs the Spatial extension workflow on main with that pull request's branch. It
checks that the build DuckDB serves is the one DuckDB's own install fetches, copies it to R2 and
commits the new pin to the branch. Renovate then leaves the branch alone, so merge it before the
month's pipeline pull request, and run the workflow again if Renovate is asked to rebase it. A new
build changes the key of every dataset that loads the extension, so those versions are built
again.

## Licences that are not Creative Commons

A publisher's own open grant is admitted as a file in `register/licences/` that quotes the publisher
on reproduction, adaptation, commercial use and attribution, as the README there describes.
`register validate` refuses a grant that is missing any of the four. A written permission is one
such file, `<AGENCY>-PERMISSION-<year>`, with the reply stored beside it.

## Withdraw a dataset or correct published files

[docs/CORRECTIONS.md](docs/CORRECTIONS.md) covers a correction from the report to the log, and a
withdrawal, withholding or removal, which change what a version's URL serves.

## Change the API or the MCP tools

The text of every endpoint, parameter and tool lives in `pipeline/publicdata/api.json`. Change it
there and run `python -m publicdata.api_text`, so the pages, the OpenAPI description and the MCP
server stay in step; the gate fails when they drift. A change a v1 caller would notice is a
breaking change (see Versioning). The MCP tools are held to a quality bar, described in Reference.

## Release the clients

1. Raise the version in `clients/python/pyproject.toml` and `clients/r/DESCRIPTION`, and add the
   changes to `clients/r/NEWS.md`.
2. Open a `feat(clients): ...` or `fix(clients): ...` pull request. The Clients workflow runs the
   Python tests and `R CMD check --as-cran`.
3. On merge, the Python client publishes to PyPI by trusted publishing when the version is new.
   A maintainer builds the R tarball and submits it to CRAN by hand, with `cran-comments.md`.

The R client is held to lintr and styler on every pull request. `clients/r/.lintr` turns on every
linter lintr has and names the few it turns off, each with its reason. The Clients workflow fails
on any finding and on any file styler would change. To check before pushing, install the package
and run both from `clients/r`:

```
R CMD INSTALL .
Rscript -e 'lintr::lint_package()'
Rscript -e 'styler::style_pkg()'
```

`style_pkg()` rewrites the files in place, so commit what it changes. A line that has to break a
rule carries `# nolint: <linter>. <reason>`.

## Reference

- Comments state constraints the code cannot show, in one or two lines of *why*.
- Before writing a helper, search for one that already exists. Follow the conventions of the
  neighbouring files.
- Every CI gate must be proven to fail on the defect it guards against. A gate without a
  failing-fixture test does not count.
- Every page meets WCAG 2.2 AAA, with dense data regions held to AA; [`docs/ACCESSIBILITY.md`](docs/ACCESSIBILITY.md)
  states the target, what `scripts/a11y.mjs` enforces and the regions excepted. Sizes are in rem,
  never px. An abbreviation the site's own prose uses goes in `pipeline/publicdata/glossary.json`,
  which the about page renders; a publisher's code or value quoted in register copy goes in
  backticks, and `register validate` fails an entry that uses an abbreviation the glossary lacks.
  A template that puts a publisher's value in the site's own sentence marks it `data-quoted`.
- New datasets enter through `register/<slug>.yaml` with licence id, evidence URL,
  attribution and column allow-list declared, or `register validate` stops the build.
  `publicdata register draft <portal dataset url>` writes a first entry from a CKAN portal: the
  publisher, licence and attribution from the portal, and each field typed from a sample of the
  file the way the build will type it. It leaves the search copy blank with a TODO and the status
  `building`, so the entry cannot go live until a person has written it and checked the rest.
- Fetch adapters: `file` (a fixed URL on the publisher's own site, dated by its Last-Modified header, read as
  `source.format` when the URL does not end in an extension, as a WFS request does;
  the licence is the register's, held while the evidence page still carries the words
  `licence.statement` quotes), `kiwis` (the Bureau of Meteorology's Water Data Online: `search` is the
  parameter, `package` the series name, `resource` `stations` or `values`), `aihw` (an AIHW report's data
  page, whose listed file `resource_match` names by title), `zenodo` (a concept record, `package`, followed
  to its newest version, the file named by `resource_match`), `ckan-stack` (every workbook of every package
  `package_match` names, read as one table, each header row found by `header_match`; with `section_match`,
  a file of several small titled tables is read one row per cell, the title's `section` group naming the
  table; `header_depth` reads a header over several rows, `group_match` the heading rows that group
  the rows below them, `footnote_marks` moves a note number off the first cell, and `file_match` takes
  the part of each file's name that the `(file)` column holds), `file-stack` (every
  workbook or CSV a publisher's own page at `url` links to whose address matches `resource_match`, read as
  one table the same way, with the licence quoted as for `file`), `ckan-resource` (a CKAN resource), `socrata` (package is the four-by-four),
  `opendatasoft` (package is the dataset id) and `arcgis-hub` (package is the item id, resource
  the layer number). Each reads the licence the portal states on every run and checks the id
  worked out from its words, or the portal's own licence code when the entry names it in
  `licence.portal_id`. A `ckan-resource` whose file host turns
  automated clients away sets `manual: true` (see "Manual sources" above).
- Each fetch records in the version's manifest where it read the licence (`licence.read_from`) and when
  (`licence.read_at`), and a fetch whose adapter records neither is refused. `licence.reviewed` in the
  register is the date the evidence page was read and the licence decided; a live entry needs it.
- A portal's generic licence code means what that portal's `license_list` says: `cc-by` is CC BY 4.0 on
  the WA, SA and Victorian portals and CC BY 3.0 AU on data.gov.au (`PORTAL_LICENCES` in fetch.py). NSW and
  NT name no version for `cc-by`, so an entry there sets `licence.portal_id` and the version from the
  dataset page, and an entry whose id disagrees with the portal's own definition is stopped.
- A source that is a live feed of what is current sets `feed: true`. The daily run fetches only feeds
  (`fetch --feeds`), and each day a feed changes is one version dated by that day.
- Geometry is declared on the entry. Points name their `lon` and `lat` fields and the publisher's `crs`;
  their coordinates are published as given. A `kind: polygon` or `kind: line` layer is read whole from a
  shapefile, GeoPackage or GeoJSON (`source.member` names it inside a zip), moved to GDA2020 (EPSG:7844), and
  served as GeoParquet, GeoJSON, a GeoPackage and PMTiles vector tiles to `geometry.maxzoom`.
- The place spine (`spine.py`) is six ABS boundary layers, each its own register entry: SA2, LGA, suburb and
  locality, postal area, and state and federal electorate. A point dataset that lists layers in `enrich` gains
  the code and name of the area each point falls in, joined by location against each layer's newest version.
  The columns are marked as joined in `schema.json` with the layer version, the ABS attribution is in every
  file's header, and the published coordinates are never changed. The joins and tiles need DuckDB's spatial
  extension, which `python -m publicdata spine install` fetches once, as pinned in
  `pipeline/publicdata/spatial-extension.json`, so the build stays offline.
- Workbooks may be `.xlsx`, `.xlsm` or legacy `.xls`, which is converted cell for cell before it is read.
  A header that repeats a name numbers each repeat, `Count (2)`. In a stack, a field whose source is
  `(file)` holds the name of the file each row came from, which is often its period; `ckan-stack` stacks
  CSV resources as well as workbooks.
- A point layer published as a shapefile or GeoPackage is read with its coordinates as `(longitude)` and
  `(latitude)`, as published. A WFS that caps its answers sets `source.page_size` and is read page by
  page into one GeoJSON file.
- An XML file is read one row per element named in `source.record`: its attributes as `@name`, each
  child's text by the child's name, and a child that repeats joined by ` | `.
- The MCP tool definitions in `pipeline/publicdata/api.json` must pass a static check. Each tool
  opens with a sentence saying what it does, names another tool to say when to use it, describes
  every parameter, states what a call costs against the rate limit and what comes back, carries
  annotations that agree with its description, and keeps its description between 200 and 1,000
  characters. Tool names are lowercase verb_object snake case, and two tools with similar
  purposes must name each other. Run `python -m publicdata.tdqs check` in `pipeline/` after
  changing a tool's text. It works offline, prints the same result every time, and names the
  tool and the quality a failure lacks. CI runs the same command.
- `example` sets the dataset page's first query, the one the query tile answers and the console
  starts from: `where` (`field: value` for an exact match, `field: {gte: 1, lt: 9}` for the API's
  other operators, `newest` for the field's newest value), `group`, `metric` (`count`, or `sum`,
  `avg`, `min` or `max` of a numeric field) and a `label` naming what it counts. Without one the
  build picks a query from the field statistics, and `publicdata gate` prints every page's query
  marked `register` or `rules`, so the picks can be read and the poor ones replaced. A register
  example that answers no rows fails the gate. `chart` sets what the yearly chart and the card's
  sparkline draw: `where` (the same form), `split` (a field, or `none`), `metric` and `label`,
  each falling back to the example's, and `year`, the field that dates a row. A text `year` is
  a financial year in any common form (`2018-19`, `2018–19`, `2018/19`, `2018-2019`, `FY201819`,
  `FY18-19`, or `FY2019` for the year that ends in June 2019), and the chart names each bar as the
  publisher wrote it. A value that is not a financial year is left out. `chart: none`
  draws no chart, for a table with no year worth drawing.
- `search_title` is the phrase a dataset's title tag targets and no two entries may share one;
  a collection's phrase goes in `collection_search_title` on the entry that carries the
  collection description. `place_field` names a `partition_by` field whose values are places,
  and the build writes a page per value under `/d/<slug>/in/<value>/`.
- `rebuild` is a whole number, 0 when left out, that a pull request raises when its change to the
  build code alters this dataset's published files. Raising it builds every version, diff and
  history archive of the dataset again (see Change the build code).
- `sort` lists the fields data.parquet is ordered by, for an entry whose queries mostly filter on
  fields the publisher's order does not group. The build breaks ties by the `key`, then by the
  row's place in the source, and writes a page index for the sorted file. Without `sort` the rows
  keep the publisher's order and the file has no page index, so set `sort` to the field the
  source is already ordered by when that order serves the main filter. Only data.parquet and the
  files made from it are sorted. A sort that suits one filter slows another, so give the
  benchmark or the queries that justify it in the PR. Changing `sort` never cuts a new version.
- `lookup` lists fields that get a bloom filter in data.parquet, for an equality lookup, such as
  an identifier, on an entry sorted for another filter. A boolean field cannot be a lookup.
- `int32` lists integer fields written as INT32 in data.parquet, for a field whose values always
  fit 32 bits, such as a year, a count or a short identifier. A new version holding a larger
  value is held at its fetch, so leave out anything that can grow past 2,147,483,647.
- A version keeps the `sort`, `lookup` and `int32` its fetch found, so an edit to them changes
  the files of later versions and never a published one. The query copy of every version
  follows the edit from the next deploy, which writes each copy again in R2. Each copy records
  its own order in its footer (`sorting_columns`), so a reader takes the order from the file.
  None of the three applies to a `kind: database` entry.
- The fetch checks every `int32` field against a new version before it stores it, and holds the
  dataset with an error naming the field when a value does not fit. Before declaring `int32` on
  a field, run `python -m publicdata register validate`, which checks the stored versions whose
  source or built Parquet (`--built <tree>`) is at hand.
- Each field carries a `label`, the name a reader sees in the explorer. `publicdata register
  labels <slug> --write` fills in any that are missing, reusing the label another entry gives a
  field of the same name and drafting the rest from the name. Review the drafts in the PR.
- Nothing that lands in `dist/` may read the clock, draw a random value or iterate an
  unordered collection.
- Raw snapshots live in object storage, never in git. Only manifests (URL, hash, fetched-at)
  are committed.
