# Decision records

Each record states a decision that shapes publicdata.au: the problem it answered, what was chosen,
what was weighed against it and what it costs. A change that would reverse one of them starts by
superseding its record.

A record does not restate the rules it led to. Each rule and the way the code meets it is written
once, in `docs/ARCHITECTURE.md`, `CONTRIBUTING.md` or `docs/CORRECTIONS.md`, and a record names
that section. Where a record and those documents disagree on how something works, the documents
and the code are right and the record is corrected. Measurements in a record are dated and stay as
they were when the decision was made.

| # | Decision | Status |
|---|----------|--------|
| [0001](0001-no-circumventing-bot-checks.md) | Publishers' bot checks are respected, and blocked files are fetched by hand | Accepted |
| [0002](0002-versions-are-kept.md) | A dated version is kept for good and keeps its content | Accepted |
| [0003](0003-fetch-runs-on-our-own-runner.md) | The fetch and the catalogue harvest run on our own runner in Australia | Accepted |
| [0004](0004-free-typeface-is-in-the-repository.md) | The typeface's free styles are in the repository under their own licence | Accepted |
| [0005](0005-update-classes.md) | A fetch is not always a version; each source declares how it updates | Accepted |
| [0006](0006-period-partitions.md) | Large dated tables are split into period parts | Accepted |
| [0007](0007-formats-each-version-carries.md) | The formats each version carries, and their size caps | Accepted |
| [0008](0008-parquet-is-the-base-format.md) | Parquet is the format everything reads | Accepted |
| [0009](0009-queries-read-parquet.md) | The MCP row tools reach every version through Parquet; D1 keeps the newest | Accepted, one part open |
| [0010](0010-mcp-answer-layer.md) | Counts are answered from per-version rollups first | Accepted |
| [0011](0011-text-stored-compressed.md) | URLs stay fixed while text is stored compressed; storing identical files once is deferred | Accepted |
| [0012](0012-storage-budget.md) | Each dataset has a storage budget, checked in CI | Accepted |
| [0013](0013-pages-under-the-file-cap.md) | Every page stays on Pages, and the deploy watches the account's file cap | Accepted |
| [0014](0014-build-cache-in-r2.md) | The build cache lives in its own R2 bucket and the publisher's files are served from the raw store | Accepted |
| [0015](0015-mcp-server-and-agent-surface.md) | Agents reach the site through one remote MCP server and the tools it shares with the pages | Accepted |
| [0016](0016-place-spine.md) | Points are joined by location to named ABS boundary layers, and the join is marked | Accepted, one part open |
| [0017](0017-hosting-on-cloudflare.md) | The site is hosted on Cloudflare, with its files kept as plain objects | Accepted |
| [0018](0018-deterministic-builds.md) | Two builds of one snapshot are byte-identical, and every tool is pinned | Accepted |
| [0019](0019-contract-only-grows.md) | The public URLs and the API only grow, and a change that breaks a caller is marked | Accepted |
| [0020](0020-licence-is-data.md) | A dataset is published only under a licence a person has checked, and the gate enforces it | Accepted |
| [0021](0021-accessibility-aaa.md) | Every page meets WCAG 2.2 AAA, and every preview meets the Lighthouse targets | Accepted |
| [0022](0022-copies-on-the-hubs.md) | The newest version of each dataset is copied to Hugging Face, Zenodo and Kaggle | Accepted |

## Statuses

- **Accepted**: decided, and the pull requests that carry it out have merged. The status line names
  them, or "the initial public release" for what the repository held when it was opened.
- **Accepted, one part open**: the decision is in force, and the record names the one question it
  leaves for a later record.
- **Superseded by NNNN**: replaced. The record stays, with a link to the one that replaced it.

A new record takes the next number, uses the same headings (Context, Decision, Alternatives
considered, Consequences) and is added to this table. A decision is recorded once its pull request
merges, so no record describes work that has not landed.
