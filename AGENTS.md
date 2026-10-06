# Agents

Start from [`README.md`](README.md), which points to every other document. The project's rules
are written for everyone in those files. The points below are about how an agent works here.

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
- Before calling a change done, run the checks in [Set up](CONTRIBUTING.md#set-up) and say which
  ran and which were skipped.
