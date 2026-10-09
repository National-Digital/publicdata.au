# 0015: Agents reach the site through one remote MCP server and the tools it shares with the pages

- Status: **Accepted** (server, spec and tools: the initial public release; tool lint: #72; every
  version reachable: #52, #108; discovery files, dataset bundles and the Agent Skill: #100)
- Date: 2026-10-08
- Deciders: National Digital
- Relates to: [0009](0009-queries-read-parquet.md), [0010](0010-mcp-answer-layer.md),
  [0019](0019-contract-only-grows.md)

## Context

An agent that can only download files has to fetch a whole table to count a few rows, and nothing
makes it say which release its answer came from. National Digital treats the MCP tools as the
feature the rest of the site serves: the files, the query API and the archive are what the tools
answer from.

The site describes its query API and its tools in several places: the OpenAPI description, the
agents page, `llms.txt`, each dataset page, the WebMCP tools the pages register, the MCP server,
its server card and its MCP Registry entry. Each copy written by hand drifts from the others.

## Decision

- The site runs a remote MCP server at `/mcp` over Streamable HTTP, with no session. Each POST is
  one JSON-RPC message or a batch, answered with one JSON body. It needs no key and no account.
- One spec, `pipeline/publicdata/api.json`, holds every sentence about the query API and the tools.
  The server's tool list, the WebMCP tools, the OpenAPI description, the server card and the
  Registry entry are all written from it, and a test fails when a written copy drifts.
- Every tool is read-only except `upvote_dataset`, which casts an anonymous vote for a dataset to be
  built. Every tool declares an output schema and returns structured content beside its text.
- A row answer names the version it came from and the publisher's attribution. The server asks the
  agent to link the version, `/d/<slug>/v/<version>/`, whenever it shows the data to a person. A
  query too large to answer is answered with the DuckDB SQL that returns the same rows from the
  version's dated Parquet.
- Tool definitions are held to a static lint, `python -m publicdata.tdqs check`, which CI runs and
  any contributor can run offline.
- The site lists its agent resources for discovery: an ARD manifest, an Agent Skill, the server card
  and a bundle per live dataset linking its newest files.

## Alternatives considered

- **A server with sessions or an event stream.** A stateless server runs as one Pages Function,
  scales with requests and needs no store of its own, and no tool needs to push to the agent.
- **Descriptions kept by hand in each place.** That is the drift the single spec exists to stop. A
  tool added for the pages is added to the server, the OpenAPI description and the listings in the
  same change.

## Consequences

- An agent with code execution can carry on from any answer itself. The dated URL and the SQL give
  it the rows the server read, and the URL keeps working because a version keeps its content.
- The server keeps no record of what is asked. The per-address rate limit holds a count in the edge
  cache for ten seconds, a vote stores a salted hash of the voter for that day, and a failed call
  goes to the Workers log with the dataset and version. A rate-limited call gets a tool error with
  the seconds to wait.
- Each tool has a fixed cost against the per-address limit, set in the spec. The server keeps that
  limit itself (`functions/_limit.js`), since the zone's rule covers `/api/v1/datasets/*` only.
- The tools are part of the public contract ([0019](0019-contract-only-grows.md)). Renaming a tool
  or a parameter breaks callers.
- Which engine answers, and in what order, is set by [0009](0009-queries-read-parquet.md) and
  [0010](0010-mcp-answer-layer.md).

See `CONTRIBUTING.md` ("Change the API or the MCP tools") and the agents page (`/agents/`).
