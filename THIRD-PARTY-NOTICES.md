# Third-party notices

Material in this repository, or used to build the site, that is **not** covered by the project's
own licences ([AGPL-3.0-or-later](LICENSE) for the code, [CC BY 4.0](LICENSE-DATA.md) for our own
register text). Each item stays under its owner's terms.

## Government data in the test fixtures

The tests build from small extracts of real government data, cut down from the published files
so the suite runs quickly. They are redistributed under each publisher's licence, with the
attribution the register records for that dataset. Every directory under
`pipeline/tests/fixtures/store/` must appear here; a test checks it.

| Fixture | Publisher | Licence | Attribution |
| --- | --- | --- | --- |
| `abs-federal-electoral-divisions-2025`, `abs-lga-2025`, `abs-postal-areas-2021`, `abs-sa2-2021`, `abs-state-electoral-divisions-2025`, `abs-suburbs-localities-2021` | Australian Bureau of Statistics | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | Australian Bureau of Statistics, Australian Statistical Geography Standard (ASGS) Edition 3, [digital boundary files](https://www.abs.gov.au/statistics/standards/australian-statistical-geography-standard-asgs/edition-3-july-2021-june-2026/access-and-downloads/digital-boundary-files). A subset of each layer's features. |
| `gnaf` | Geoscape Australia, licensed by the Commonwealth of Australia | [Open G-NAF End User Licence Agreement](https://data.gov.au/data/dataset/geocoded-national-address-file-g-naf) | Incorporates or developed using G-NAF © Geoscape Australia licensed by the Commonwealth of Australia under the Open Geo-coded National Address File (G-NAF) End User Licence Agreement. A small subset of the August 2026 release. |
| `qld-road-casualties`, `qld-road-crash-driver-demographics`, `qld-road-crash-factors`, `qld-road-crash-locations`, `qld-road-crash-restraint-helmet-use`, `qld-road-crash-vehicle-types` | Department of Transport and Main Roads, Queensland Government | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | Department of Transport and Main Roads, Queensland Government, [Crash data from Queensland roads](https://www.data.qld.gov.au/dataset/crash-data-from-queensland-roads). 300 rows of each file. |
| `catalogue` | The government portals the catalogue harvests | Each record's own licence field | Catalogue metadata (titles, publishers, licence ids) as the portals published it. |

`pipeline/tests/fixtures/catalogue/` holds catalogue API responses from the City of Sydney (ArcGIS
Hub) and the City of Ballarat (Opendatasoft) portals, used unchanged to test the harvesters. The
other fixtures (`draft/`, `legacy.xls`, `locate.json`) are made up for the tests.

The R client's vignette figure (`clients/r/vignettes/figures/map-1.png`) is drawn from ACT Road
Crash Data, Roads ACT, City and Environment Directorate, ACT Government, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## Government data the site serves

None of it is in this repository. Each dataset's licence, attribution and evidence are in its
`register/` entry and on its page, and the build refuses a dataset whose licence does not allow
republishing. Licences other than Creative Commons are recorded in `register/licences/`.

## Typeface

| Asset | Owner | Terms |
| --- | --- | --- |
| Random Grotesque (`RG-StandardBook`, `RG-StandardMedium`, `RG-StandardBold`) | RandomMaerks (Bao Nguyen) | Licensed to National Digital for its sites. **Not in this repository and not covered by its licences.** The deploy fetches the files from private storage; a build without them falls back to system fonts on the page and Pillow's bundled Aileron on social cards. |

## Libraries the site serves

The data explorer serves these npm packages from `/static/vendor/`. They are installed at build
time from `package-lock.json`, not committed.

| Package | Licence |
| --- | --- |
| `@duckdb/duckdb-wasm` | MIT |
| `@perspective-dev/client`, `server`, `viewer`, `viewer-charts`, `viewer-datagrid` | Apache-2.0 |
| `apache-arrow` | Apache-2.0 |

## The GitHub mark

The pages link to the repository with the GitHub mark (`GH_MARK` in
`pipeline/publicdata/site.py`), drawn from [Octicons](https://github.com/primer/octicons) under
the MIT licence. The mark is GitHub's, used as its
[logo guidelines](https://github.com/logos) allow for linking to a repository.

## This project's own marks

The **publicdata.au** and **National Digital** names, marks and brand assets are not licensed
for reuse by the AGPL grant. See [`BRAND.md`](BRAND.md).
