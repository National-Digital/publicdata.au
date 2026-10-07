<!-- The title becomes the squash-merge subject and decides the release number: use a Conventional
     Commit (feat:, fix:, data:, docs:, ...). See CONTRIBUTING.md. -->

## What and why

<!-- What this changes and why. Link any issue it closes (Closes #123). -->

## How I checked it

<!-- The commands you ran and what you saw. -->

- [ ] `python -m publicdata register validate`
- [ ] `pytest -n auto` in `pipeline/`
- [ ] A build and gate of any dataset this touches

## Checklist

- [ ] Every commit is signed off (`git commit -s`)
- [ ] A new or changed dataset meets "Licence is data" in `docs/ARCHITECTURE.md`
- [ ] Nothing derived, as `docs/ARCHITECTURE.md` defines it
- [ ] A change a v1 API caller would notice is marked breaking (`!`)
