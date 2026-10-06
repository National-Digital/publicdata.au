# Agents

Before opening, updating or reviewing a change, read [`README.md`](README.md) and every document
it links to, [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`SECURITY.md`](SECURITY.md) among them.
They hold the project's rules, which apply to agents as to everyone. This file adds only what is
particular to agents.

- An agent cannot certify the [Developer Certificate of Origin](CONTRIBUTING.md#commits-and-pull-requests).
  The person running it reviews the change and signs off each commit. Never add an agent as an
  author or co-author.
- An agent does not approve, request changes on or merge a pull request. It reports its findings
  to the person running it, who decides.
- Do not fetch from publishers while exploring or testing. Work from `--fixtures` and the
  recorded responses in `pipeline/tests/`. A person runs real fetches, and nothing gets around a
  publisher's bot check.
- Do not push `data/` branches or edit manifests in `store/`; the fetch app and maintainers
  change those.
- Check that every function, option and config key you call exists in the version the project
  pins, by reading the installed package or its source.
- Before calling a change done, run the checks in [Set up](CONTRIBUTING.md#set-up) and say which
  ran and which were skipped.
- When reviewing, read the change as code that can look right and still be wrong: a call to a
  function that does not exist, a helper that repeats one already in the repository, an idiom
  foreign to this codebase, or code left over from an earlier attempt. Question a new document
  nobody asked for.
