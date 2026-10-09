# 0019: The public URLs and the API only grow, and a change that breaks a caller is marked

- Status: **Accepted** (the initial public release; download names: #47, #129; checksums: #73)
- Date: 2026-10-08
- Deciders: National Digital
- Relates to: [0002](0002-versions-are-kept.md), [0011](0011-text-stored-compressed.md),
  [0015](0015-mcp-server-and-agent-surface.md)

## Context

People cite version URLs, and the copies on the hubs link back to them. Scripts, the R and Python
clients and agents read the site by its paths. None of them can be told when a path moves, so a
path that stops answering breaks them without notice.

## Decision

- The URL contract in `docs/ARCHITECTURE.md` is public and only grows. A new format, file or path
  may be added. One already published keeps its name and its meaning, and its bytes change only as
  [0002](0002-versions-are-kept.md) allows, however it is stored
  ([0011](0011-text-stored-compressed.md)).
- File names in URLs are generic, such as `data.parquet` and `manifest.json`. A download saves under
  a name that carries the dataset and the version, given by `Content-Disposition`, so a saved file
  says what it holds while the URL stays the same.
- `latest/` names whatever is newest and changes, with a five-minute cache. A caller that needs a
  file to stay the same uses the dated URL. Each version's `SHA256SUMS` lets a reader check what
  they downloaded.
- A file the site may no longer serve answers 410, and its version's manifest stays on record in
  `store/` ([`docs/CORRECTIONS.md`](../CORRECTIONS.md)).
- The query API is versioned in its path, `/api/v1/`. A change that would break a v1 caller goes
  into `/api/v2/`, and v1 keeps answering for at least twelve months after v2 ships.
- A change that a caller of the published paths, the query API or the MCP tools would notice is
  breaking. Adding a path, a field, a format, a parameter or a tool is not. A breaking change is
  marked with `!` after the type in its pull request title, and raises the major number of the
  site's release.
- The R and Python clients take any slug, so a new dataset needs no client release. They follow
  semantic versioning of their own.

## Alternatives considered

- **Descriptive file names in URLs.** Downloads named `data.csv` overwrote each other, and a saved
  copy did not say what it was (#47). Renaming the files in their URLs would have broken every
  citation, so the descriptive name travels in `Content-Disposition` instead.

## Consequences

- A file name chosen badly stays. The fix is a new name beside it.
- The formats a version left out under its caps stay left out
  ([0007](0007-formats-each-version-carries.md)).
- An MCP tool's name and parameters are part of the contract
  ([0015](0015-mcp-server-and-agent-surface.md)), so renaming one is a breaking change.
- The clients also read `/places.json`, each dataset's `fields.json`, a database's `tables/` files
  and `/api/v1/catalogue`, and the URL contract does not list them yet. Until it does, a change to
  one of those can break a client without counting as breaking under this record.

See `docs/ARCHITECTURE.md` ("URL contract") and `CONTRIBUTING.md` ("Versioning").
