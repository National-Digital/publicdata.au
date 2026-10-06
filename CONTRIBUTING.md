# Contributing

publicdata.au republishes Australian government open data as dated versions that never change.
Contributions are welcome: a new dataset, a fix to an entry, a new output format, an adapter for a
portal we cannot read yet, or a bug fix. This guide covers the routine changes step by step, then
the rules every change is held to.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Report security issues
privately as [SECURITY.md](SECURITY.md) describes.

Questions and ideas that are not a fault go in
[Discussions](https://github.com/National-Digital/publicdata.au/discussions).

## Ground rules

The build enforces these, so a change that breaks one fails its checks. `CLAUDE.md` states them in
full and `docs/ARCHITECTURE.md` explains the code they shape.

- Nothing derived: data is re-keyed, re-typed and joined on a declared key, and never turned into
  rates, rankings or estimates.
- Licence is data. A dataset is published only under an open licence a person has reviewed, with
  the publisher's own statement as evidence.
- Versions are immutable and dated by the source's change. A version is never deleted or rewritten.
- Every file carries its provenance: publisher, licence, attribution, source URL, fetch time and
  source hash.
- Two builds of one snapshot are byte-identical.
- Requesters are recorded by organisation, never by name.

## Set up

You need Python 3.14, Node 24 and `uv`. No credentials are needed to build, test or fetch.

```
git config core.hooksPath .githooks   # signs off every commit for the DCO
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

The site's typeface is licensed to National Digital and is not in the repository. Without it,
pages use system fonts and the social cards use Pillow's bundled Aileron; nothing else changes.
`store pull` fetches it from the same private bucket for the deploy.

## Private copies

Workflows with side effects (deploy, fetch, catalogue, hubs, publishing the clients, approving
data PRs) run only in `National-Digital/publicdata.au`. In a fork or a private copy, only the
checks run: tests, sign-off, PR title and secret scanning. `pipeline/tests/test_repo.py` fails if a
new job with side effects lacks that condition. Dependabot reads its config wherever it is, so
delete `.github/dependabot.yml` in a private copy if you do not want its pull requests.

## Commits and pull requests

- Sign off every commit (`git commit -s`). This is the
  [Developer Certificate of Origin](https://developercertificate.org/): you certify that you wrote
  the change or have the right to submit it under the project's licence. The DCO check fails a pull
  request with an unsigned commit; `git rebase --signoff main` fixes it.
- Title the pull request as a Conventional Commit, `<type>(<scope>): <subject>`. The types are
  `feat fix docs chore perf refactor test build ci style revert data`; `data` is for register
  entries and store manifests. The squash merge makes the title the commit subject on `main`, and
  the title decides the release number (see Versioning).
- Say in the description what changed and why, and what you ran to check it. The template asks.
- A pull request merges when a maintainer has approved it and every required check is green:
  tests, the register, the build and gate, the clients, secret scanning, CodeQL, dependency review,
  the DCO and the title. Pull requests are squash-merged.
- The fetch's own data pull requests are approved by `.github/workflows/data-pr.yml`, only when
  the fetch app opened them from a run on `main`, with one signed-off commit of store manifests.
  Only the app may push `data/` branches. They merge themselves when their checks are green.
  Every other pull request, a person's change to `store/` included, needs a maintainer.

## Versioning

publicdata.au has four kinds of version, and each has its own rule.

- **Datasets** are versioned by date, not by number. A version is named for the day the publisher
  changed the source, it never changes once published, and its URL never moves. An unchanged
  source makes no version.
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

## Add a dataset

1. Find the dataset on its publisher's portal and read the licence. It must be open (CC BY, CC0 or
   an open grant the publisher states), and the evidence must be the publisher's own words. A
   non-commercial or no-derivatives licence, or none, means the dataset cannot be published.
2. Draft the entry: `python -m publicdata register draft <portal dataset url>`. It writes
   `register/<slug>.yaml` with the publisher, licence and attribution from the portal and every
   field typed from a sample of the file, at status `building`.
3. Finish the entry by hand. Write the title, `search_title`, description and the TODOs the draft
   left. Keep only the fields that should be published (the allow-list), set `key` and
   `partition_by` where they apply, and give geometry as Reference below describes. Set
   `licence.reviewed` to the day you read the licence.
4. Label the fields: `python -m publicdata register labels <slug> --write`, then read the drafts.
5. Fetch and build it locally as Set up shows, and run the gate. The gate prints the page's
   example query; if it reads poorly, set `example` and `chart` in the entry.
6. Set `status: live` and open a pull request titled `data(register): add <what it is>`. Say where
   the licence evidence is and what you checked.
7. The Storage cost check projects how much the entry adds to storage in a year: the bytes one
   version publishes times the versions its cadence implies. An entry over 5 GB a year fails it
   until a maintainer who accepts the cost adds the `cost-approved` label. Run
   `python -m publicdata cost <slug>` to see the figure first.

A dataset that cannot be published yet keeps its entry at `backlog`, `assessing` or `blocked`, with
the reason, so the site can say why.

## Change or fix a dataset entry

Edit `register/<slug>.yaml` and open a `fix(register): ...` pull request. A field the publisher
renamed, a resource that moved or a header that changed row are the usual causes. A change that
would alter a published version's bytes is not possible: versions are immutable, so the fix applies
from the next version. When the publisher changes its licence, the fetch stops that dataset until a
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
2. Carry the provenance header into the file in whatever way the format allows, as the other
   writers do.
3. Describe the format in `pipeline/publicdata/api.json`, which the pages, the API and the MCP
   tools all read their text from, and run `python -m publicdata.api_text` to regenerate the tool
   file.
4. Add tests that the file reads back to the same rows and types, and that two builds of one
   snapshot give identical bytes.
5. A new format is added to every version at the next deploy, and the old versions' other files
   are left as they are. Open the pull request as `feat(serialise): ...`.

## Licences that are not Creative Commons

A publisher's own open grant is admitted as a file in `register/licences/` that quotes the publisher
on reproduction, adaptation, commercial use and attribution. `register validate` refuses a grant
that is missing any of the four. A written permission is one such file,
`<AGENCY>-PERMISSION-<year>`, with the reply stored beside it.

## Withdraw a dataset or correct published files

- When a publisher withdraws a source, the entry stays and the versions already published stay
  where they are. Set the entry's status and the reason, and no new versions are made.
- When a licence turns out not to allow publication, a maintainer withholds the dataset or the
  affected columns, as `source_withheld` and the omitted fields do. The build stops making those
  files and the site says why.
- A legal takedown is the only time a published file is removed. It is recorded in `changes.json`
  as a tombstone that keeps the manifest and hash.
- To rebuild files a bug wrote wrongly, a maintainer runs the Deploy workflow with `replace` set
  to the version prefixes, which also purges them from the edge cache. The pull request that fixed
  the bug says which versions it affects.

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

## Reference

- Comments state constraints the code cannot show, in one or two lines of *why*.
- Every CI gate must be proven to fail on the defect it guards against. A gate without a
  failing-fixture test does not count.
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
- A licence that is not Creative Commons is admitted as a file in `register/licences/` that quotes the
  publisher on reproduction, adaptation, commercial use and attribution (see the README there). A
  written permission is one such file, `<AGENCY>-PERMISSION-<year>`, with the reply stored beside it.
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
  extension, which `python -m publicdata spine install` fetches once so the build stays offline.
- Workbooks may be `.xlsx`, `.xlsm` or legacy `.xls`, which is converted cell for cell before it is read.
  A header that repeats a name numbers each repeat, `Count (2)`. In a stack, a field whose source is
  `(file)` holds the name of the file each row came from, which is often its period; `ckan-stack` stacks
  CSV resources as well as workbooks.
- A point layer published as a shapefile or GeoPackage is read with its coordinates as `(longitude)` and
  `(latitude)`, as published. A WFS that caps its answers sets `source.page_size` and is read page by
  page into one GeoJSON file.
- An XML file is read one row per element named in `source.record`: its attributes as `@name`, each
  child's text by the child's name, and a child that repeats joined by ` | `.
- The MCP tool definitions are held to a Tool Definition Quality Score of 4.8 per tool and for
  the server, judged with the rubric the directories publish. After changing a tool's text in
  `pipeline/publicdata/api.json`, a maintainer runs `python -m publicdata.tdqs score` (it needs a
  signed-in `claude` CLI, and asks only about tools whose definition changed) and commits
  `tdqs.json`. CI reads the committed scores and fails a tool that is unscored or under the bar.
- `example` sets the dataset page's first query, the one the query tile answers and the console
  starts from: `where` (`field: value` for an exact match, `field: {gte: 1, lt: 9}` for the API's
  other operators, `newest` for the field's newest value), `group`, `metric` (`count`, or `sum`,
  `avg`, `min` or `max` of a numeric field) and a `label` naming what it counts. Without one the
  build picks a query from the field statistics, and `publicdata gate` prints every page's query
  marked `register` or `rules`, so the picks can be read and the poor ones replaced. A register
  example that answers no rows fails the gate. `chart` sets what the yearly chart and the card's
  sparkline draw: `where` (the same form), `split` (a field, or `none`), `metric` and `label`,
  each falling back to the example's.
- `search_title` is the phrase a dataset's title tag targets and no two entries may share one;
  a collection's phrase goes in `collection_search_title` on the entry that carries the
  collection description. `place_field` names a `partition_by` field whose values are places,
  and the build writes a page per value under `/d/<slug>/in/<value>/`.
- Each field carries a `label`, the name a reader sees in the explorer. `publicdata register
  labels <slug> --write` fills in any that are missing, reusing the label another entry gives a
  field of the same name and drafting the rest from the name. Review the drafts in the PR.
- Nothing that lands in `dist/` may read the clock, draw a random value or iterate an
  unordered collection.
- Raw snapshots live in object storage, never in git. Only manifests (URL, hash, fetched-at)
  are committed.
