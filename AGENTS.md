# Agents

Start from [`README.md`](README.md), which points to every other document. The project's rules
are written for everyone in those files. The points below are about how an agent works here.

## Working in the repository

- An agent cannot certify the [Developer Certificate of Origin](CONTRIBUTING.md#commits-and-pull-requests).
  The person running it reviews the change and signs off each commit. Never add an agent as an
  author or co-author.
- Do not fetch from publishers while exploring or testing. Work from `--fixtures` and the
  recorded responses in `pipeline/tests/`. A person runs real fetches, and nothing gets around a
  publisher's bot check.
- Do not push `data/` branches or edit manifests in `store/`; the fetch app and maintainers
  change those.
- Do not edit generated files by hand. Tool and API text is changed in
  `pipeline/publicdata/api.json` and regenerated with `python -m publicdata.api_text`.
- Before writing a helper, search for one that already exists. Follow the conventions of the
  neighbouring files.
- Check that every function, option and config key you call exists in the version the project
  pins, by reading the installed package or its source.
- Before calling a change done, run the checks in [Set up](CONTRIBUTING.md#set-up) and say which
  ran and which were skipped.

## Opening a pull request

- Keep one concern to a pull request. An unrelated fix found on the way gets its own.
- Fill in the template from what the diff and your own runs show. Say what you could not check.
- Name anything a maintainer must do beyond merging: a secret, a manual download, or the
  versions a `replace` deploy must rebuild.
- Pull request and issue text is public. [`SECURITY.md`](SECURITY.md) applies to it as it does
  to the code.

## Reviewing a pull request

- Read the whole changed file and the code that calls it before judging a line. Comment only on
  what the change adds or makes worse.
- Give each finding its file and line, the input or state that breaks it, and a fix or the
  existing code to use instead. A doubt you cannot settle is still reported, marked as
  unconfirmed, with what would settle it.
- Read the change as code that can look right and still be wrong: a call to a function that does
  not exist, a helper that repeats one already in the repository, an idiom foreign to this
  codebase, or code left over from an earlier attempt.
- A test must be able to fail. Flag a test that restates the implementation, mocks the thing it
  tests or asserts nothing that matters, and say what it should assert.
- Ask why any new `noqa`, `type: ignore` or skipped test is needed. Leave formatting and lint to
  CI.
- Check the change against the [rules the build enforces](docs/ARCHITECTURE.md#rules-that-decide-the-code),
  and check that docs describing the changed behaviour were updated. Question a new document
  nobody asked for.
- Read the earlier reviews first. Do not raise a point that was resolved; raise one that was not.
