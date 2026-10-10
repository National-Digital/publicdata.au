# 0013: Two builds of one snapshot are byte-identical, and every tool is pinned

- Status: Accepted
- Date: 2026-10-10

## Context

A dated version keeps its content ([0002](0002-versions-are-kept.md)), and its files are written to
R2 once. The deploy reuses a version it has built before instead of building it again
([0014](0014-build-cache-in-r2.md)). Both rest on a build that gives the same bytes from the same
inputs. If it did not, a rebuild could change a cited file without anyone choosing to, and a reused
version could differ from the one a fresh build would make.

## Decision

- Serialisers are pure functions of the normalised model, and two builds of one snapshot are
  byte-identical. CI's Determinism job proves it on the fixtures: twice from scratch, through a cold
  cache and a warm one, and with a subset of formats then every format.
- A DuckDB file is the one exception. Its storage lays out and packs its blocks differently on each
  write, so CI compares every `data.duckdb` by what it holds, and nothing the site publishes states
  its size.
- A version's build key is made of its inputs: its source, register entry and manifest, the spine
  layers and spatial extension it uses, the modules its kind runs, each format's writer, the
  rebuild numbers and the runtime and library versions. The rest of the build code is left out of
  the key on purpose, so an edit to it reuses every version.
- A change to that code is checked against real versions instead. The plan draws a seeded sample of
  datasets covering the features that change how a version is built, builds them with the new code
  and compares them with the cache entries a deploy would reuse. A difference fails the deploy, and
  the fix is to raise a rebuild number.
- Every tool and library is pinned to an exact version, each in one file the workflows read.
  DuckDB's spatial extension, which DuckDB can replace within a release, is installed from a copy
  in R2 checked against a pinned hash. Renovate raises the pins once a month, and the few tools
  whose raise needs a new hash are raised by hand.

## Alternatives considered

- **Every line of build code in the key.** Any refactor would rebuild the whole archive, which takes
  hours. A seeded sample of real versions finds an edit that changes the files of the datasets it
  draws.
- **Open version ranges.** A runner's update could then change the key or the bytes with no change
  in the repository. With exact pins a raise is a pull request of its own, and a reviewer can see
  what it rebuilds.

## Consequences

- A byte comparison needs no tolerance and no judgement, and any contributor can run it on the
  fixtures.
- Raising Python or a keyed library moves every version's key and rebuilds every version, about four
  hours on main, so such a pull request is merged on a day with no data pull request due.
- A sample cannot cover every dataset, so the plan prints the strata no sampled dataset covers.
  Outside a `replace` dispatch, a dated file already in R2 keeps the bytes of its first build.

See `docs/ARCHITECTURE.md` ("Rules that decide the code", "Hosting") and `CONTRIBUTING.md`
("Change the build code", "Toolchain versions").
