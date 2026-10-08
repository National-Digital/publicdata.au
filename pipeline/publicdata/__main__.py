"""publicdata: register validate | draft | labels | fetch | catalogue fetch | build | gate | split | store pull/push | dist-push | checksums | r2 restore-gzip | hubs | contribute | cost."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

from . import REPO, SITE

ROOT = Path(__file__).resolve().parents[2]
REGISTER = ROOT / "register"
PUBLISHERS = REGISTER / "publishers"
STORE = ROOT / "store"
FIXTURES = ROOT / "pipeline" / "tests" / "fixtures" / "store"


def cmd_register(args) -> int:
    from .register import load

    ds = load(REGISTER)
    for d in ds:
        print(f"{d.status:9s} {d.slug:40s} {d.licence.id:14s} {d.publisher.short}")
    print(f"{len(ds)} entries valid")
    for d in ds:
        bare = [f.name for f in d.fields if not f.label]
        if d.status == "live" and bare:
            print(
                f"note: {d.slug} has {len(bare)} fields without a label; `publicdata register labels {d.slug}` drafts them"
            )
    from .store import manifests
    from .validate import int32_misfits

    store_dir = Path(getattr(args, "store", STORE))
    bad, unchecked = [], 0
    for d in ds:
        if not d.int32 or not d.publishable:
            continue
        for m in manifests(store_dir, d.slug):
            found = int32_misfits(d, m, store_dir, [Path(b) for b in getattr(args, "built", [])])
            if found is None:
                unchecked += 1
            bad += [f"{d.slug}/{m.version}: {x}" for x in found or []]
    for b in bad:
        print(f"int32: {b}")
    if unchecked:
        print(
            f"int32: {unchecked} stored version(s) not checked, since neither their Parquet nor their source is here"
        )
    return 1 if bad else 0


def cmd_labels(args) -> int:
    """Draft a label for every field that has none: the label another entry already gives a field
    of the same name, or else one worked out from the name. --write puts them in the YAML."""
    from .register import draft_label, load

    datasets = load(REGISTER)
    known = {}
    for d in datasets:
        for f in d.fields:
            if f.label:
                known.setdefault(f.name, f.label)
    for d in datasets:
        if args.slug and d.slug not in args.slug:
            continue
        names = [f.name for f in d.fields]
        drafts = {
            f.name: known.get(f.name) or draft_label(f.name, names) for f in d.fields if not f.label
        }
        for n, label in drafts.items():
            print(f"{d.slug}: {n} -> {label}{'' if n in known else '  (drafted)'}")
        if args.write and drafts:
            path = Path(d.path)
            lines = path.read_text(encoding="utf-8").split("\n")
            out, done = [], set()
            for ln in lines:
                out.append(ln)
                m = re.match(r"^- name: (\S+)$", ln)
                if m and m.group(1) in drafts:
                    out.append(f"  label: {drafts[m.group(1)]}")
                    done.add(m.group(1))
            if done != set(drafts):
                raise SystemExit(
                    f"{path.name}: could not place a label for {', '.join(sorted(set(drafts) - done))}"
                )
            path.write_text("\n".join(out), encoding="utf-8")
    return 0


def cmd_draft(args) -> int:
    """Write a first register entry for a CKAN portal dataset, for a person to review."""
    import requests

    from .catalogue import PortalError
    from .publishers import load_curated
    from .register_draft import DraftError, draft, to_yaml, write

    try:
        slug, entry, notes = draft(
            args.url,
            load_curated(PUBLISHERS),
            resource=args.resource,
            slug=args.slug,
            sheet=args.sheet,
            header_row=args.header_row,
        )
        text = to_yaml(entry)
        if args.stdout:
            print(text, end="")
        else:
            print(f"wrote {write(REGISTER, slug, text).relative_to(ROOT)}")
    except (DraftError, PortalError, requests.RequestException) as e:
        print(f"draft: {e}", file=sys.stderr)
        return 1
    for n in notes:
        print(f"note: {n}", file=sys.stderr)
    return 0


def cmd_fetch(args) -> int:
    import requests

    from .fetch import MANUAL, ManualDue, fetch
    from .register import load

    store_dir = Path(args.store)
    for pair in args.file:
        slug, _, path = pair.partition("=")
        if not path or not Path(path).exists():
            sys.exit(f"--file {pair}: give SLUG=PATH for a file, or a stack's folder, that exists")
        MANUAL[slug] = Path(path)
    datasets = load(REGISTER)
    manual = {d.slug for d in datasets if d.source.manual}
    if set(MANUAL) - manual:
        sys.exit(f"--file: {sorted(set(MANUAL) - manual)} not a manual source in the register")
    changed = failed = due = 0
    groups: dict[str, list[str]] = {}
    for d in datasets:
        if args.slug and d.slug not in args.slug:
            continue
        if d.status not in ("live", "building") or d.source.adapter == "none":
            continue
        if args.feeds and not d.source.feed:
            continue
        # One dataset that cannot be fetched is reported and the rest go ahead.
        try:
            m = fetch(d, store_dir)
        except ManualDue as e:
            due += 1
            print(f"{d.slug}: MANUAL {e}")
            continue
        except (RuntimeError, ValueError, OSError, requests.RequestException) as e:
            failed += 1
            print(f"{d.slug}: FAILED {e}")
            continue
        if m is None:
            print(f"{d.slug}: unchanged")
        else:
            changed += 1
            groups.setdefault(d.publisher.jurisdiction.lower(), []).append(
                (store_dir / d.slug / m.version / "manifest.json").as_posix()
            )
            print(
                f"{d.slug}: new version {m.version} ({m.bytes} bytes, {m.encoding}, sha256 {m.sha256[:12]})"
            )
    if args.groups:
        import json

        Path(args.groups).write_text(json.dumps(groups, indent=2, sort_keys=True) + "\n")
    print(f"{changed} new version(s), {failed} failed, {due} manual download(s) due")
    return 0


R2 = "r2://"


def cmd_build(args) -> int:
    from .register import load

    out = Path(args.out)
    store_dir = FIXTURES if args.fixtures else Path(args.store)
    from . import serialise

    serialise.LIMIT = None
    if args.formats:
        want = {f.strip() for f in args.formats.split(",") if f.strip()}
        unknown = want - set(serialise.WRITERS)
        if unknown:
            sys.exit(
                f"--formats: unknown {sorted(unknown)}; the formats are {list(serialise.WRITERS)}"
            )
        if "ndjson" not in want or "parquet" not in want:
            sys.exit("--formats must keep ndjson and parquet, which the build reads back")
        serialise.LIMIT = want
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    datasets = load(REGISTER)
    cache = None
    if args.cache and not args.absent:
        sys.exit(
            "--cache needs --absent: a cached version leaves out files that are already published"
        )
    if args.cache:
        from .cache import BuildCache

        cache = BuildCache(Path(args.cache))
        if not args.slug:
            n = _prune_unusable(cache, datasets, store_dir)
            print(f"cache: {n} entries this build cannot use removed first")
    from . import published

    download = None
    if args.published.startswith(R2):
        from .r2 import downloader

        download = downloader(args.published[len(R2) :])
    source = Path(args.published) if args.published and not download else None
    published.current = published.Published(out, args.built, source, download)
    try:
        return _build(args, out, store_dir, datasets, cache)
    finally:
        published.current = None


def _build(args, out: Path, store_dir: Path, datasets, cache) -> int:
    from . import published
    from .build import build_dataset
    from .site import render_site

    outs = []
    for d in datasets:
        if args.slug and d.slug not in args.slug:
            continue
        o = build_dataset(d, store_dir, out, cache)
        if o.versions:
            print(
                f"{d.slug}: {len(o.versions)} version(s), latest {o.latest.manifest.version}, {o.latest.rows} rows"
            )
        outs.append(o)
    if args.built:
        from .build import take_built

        n = sum(take_built(outs, out, Path(root)) for root in args.built)
        print(f"build: {n} file(s) linked in from the shard builds")
    from .catalogue import latest as catalogue_latest
    from .catalogue import load as load_catalogue
    from .publishers import load_curated

    cat = catalogue_latest(store_dir)
    if not args.no_site:
        # The pages draw their figures from every version's Parquet, so a cached version's comes
        # back first; a page drawn without it would only lose its figures.
        want = [
            f"d/{o.dataset.slug}/v/{v.manifest.version}/data.parquet"
            for o in outs
            for v in o.versions
            if "data.parquet" in v.absent
        ]
        missing = [
            r for r, p in zip(want, published.current.paths(want), strict=True) if not p.exists()
        ]
        if missing:
            sys.exit(
                f"build: {len(missing)} cached version(s) have no Parquet to draw from, e.g. "
                f"{missing[0]}; pass --published, or build without the cache"
            )
        render_site(
            outs,
            out,
            records=load_catalogue(store_dir),
            curated=load_curated(PUBLISHERS),
            catalogue_as_at=cat.version if cat else "",
            catalogue_stats=(cat.source or {}).get("stats", {}) if cat else {},
            search=Path(args.search) if args.search else None,
            cache=cache,
            hubs=_hubs_record(store_dir),
            tasks=_tasks(args.tasks),
        )
    if cache is not None:
        if not args.slug:
            cache.prune()
        print(
            f"cache: {cache.hits} reused, {cache.misses} built, {cache.grown} file(s) written into reused versions"
        )
        import json

        # A file read back from R2 or a shard's tree is in this tree now.
        absent = sorted(
            r
            for o in outs
            for v in o.versions
            for rel in v.absent
            if not (out / (r := f"d/{o.dataset.slug}/v/{v.manifest.version}/{rel}")).exists()
        )
        # A cached version's query copy was pushed by the build that made it.
        absent += sorted(
            v.query for o in outs for v in o.versions if v.query and not (out / v.query).exists()
        )
        absent.sort()
        Path(args.absent).write_text(json.dumps(absent, indent=0) + "\n", encoding="utf-8")
        print(f"cache: {len(absent)} published file(s) left out, listed in {args.absent}")
    print(f"built {sum(1 for p in out.rglob('*') if p.is_file())} files into {out}")
    return 0


def cmd_gate(args) -> int:
    from .gate import main

    return main(Path(args.out), REGISTER, _absent(args.absent), not args.versions_only)


def _absent(path: str | None) -> list[str]:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8")) if path else []


VERSIONED_FILE = re.compile(r"^d/[a-z0-9][a-z0-9-]*/v/\d{4}-\d{2}-\d{2}/")


def cmd_split(args) -> int:
    """Move files that R2 serves into a sibling tree: anything over the Pages per-file limit
    and, with --versioned, every file of a dated version, its page included. A file moved to R2
    is only reachable where _routes.json runs the function that reads it."""
    from .serialise.profile import QUERY_DIR
    from .site import ROUTES

    routed = [
        re.compile("^/" + re.escape(r.lstrip("/")).replace(r"\*", ".*") + "$") for r in ROUTES
    ]
    out, large = Path(args.out), Path(args.large)
    limit = int(args.limit_mib * 1024 * 1024)
    moved, stranded = 0, []
    for p in sorted(out.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(out).as_posix()
        if rel.startswith(f"{QUERY_DIR}/"):
            # A query copy lives in R2 alone, where only the query engine reads it.
            dest = large / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), dest)
            moved += 1
            continue
        versioned = args.versioned and VERSIONED_FILE.match(rel)
        if versioned or p.stat().st_size > limit:
            if not any(r.match("/" + rel) for r in routed):
                stranded.append(rel)
                continue
            dest = large / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), dest)
            moved += 1
    print(f"split: {moved} file(s) moved to {large}")
    for rel in stranded:
        print(
            f"split: {rel} is over the Pages limit and no route reaches R2 for it", file=sys.stderr
        )
    return 1 if stranded else 0


def cmd_catalogue(args) -> int:
    import requests

    from .catalogue import PortalError, fetch

    try:
        fetch(Path(args.store))
    except (PortalError, OSError, ValueError, requests.RequestException) as e:
        print(f"catalogue: FAILED {e}")
        return 1
    return 0


def cmd_catalogue_publishers(args) -> int:
    """Print proposed curation for data.gov.au organisations no curated publisher claims yet."""
    import requests
    import yaml

    from .catalogue import load
    from .publishers import load_curated, suggest

    codes = requests.get(
        "https://data.api.abs.gov.au/rest/codelist/ABS/CL_LGA_2024",
        headers={"Accept": "application/vnd.sdmx.structure+json"},
        timeout=120,
    ).json()["data"]["codelists"][0]["codes"]
    state = dict(
        zip("12345678", ("NSW", "Vic", "Qld", "SA", "WA", "Tas", "NT", "ACT"), strict=True)
    )
    lgas = {
        c["name"]: state[c["id"][0]] for c in codes if len(c["id"]) == 5 and c["id"][0] in state
    }
    rows = suggest(load(Path(args.store)), load_curated(PUBLISHERS), lgas)
    print(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True, width=100))
    print(f"# {len(rows)} organisation(s) to review", file=sys.stderr)
    return 0


def cmd_d1(args) -> int:
    """Write one SQL file per live dataset whose latest version the query API has not loaded."""
    import json

    from .d1 import CATALOGUE, SERVED, catalogue_loads, served_loads, write_loads
    from .register import load

    loaded: dict[str, list[str]] = {}
    loaded_fields: dict[tuple[str, str], str] = {}
    loaded_orders: dict[tuple[str, str], str] = {}
    if args.loaded and Path(args.loaded).exists():
        raw = json.loads(Path(args.loaded).read_text(encoding="utf-8") or "[]")
        rows = (
            [r for part in raw for r in part.get("results", [])]
            if isinstance(raw, list) and raw and "results" in raw[0]
            else raw
        )
        for r in rows or []:
            loaded.setdefault(r["slug"], []).append(r["version"])
            if "fields" in r:
                loaded_fields[(r["slug"], r["version"])] = r["fields"]
            if r.get("ord") is not None:
                loaded_orders[(r["slug"], r["version"])] = r["ord"]
    live = [d for d in load(REGISTER) if d.status in ("live", "building")]
    parts = write_loads(
        [Path(r) for r in args.root],
        live,
        loaded,
        Path(args.out),
        args.stamp,
        loaded_fields,
        loaded_orders,
    )
    if args.catalogue and Path(args.catalogue).exists():
        parts += catalogue_loads(
            Path(args.catalogue), loaded.get(CATALOGUE, []), Path(args.out), args.stamp
        )
        parts += served_loads(
            Path(args.catalogue), loaded.get(SERVED, []), Path(args.out), args.stamp
        )
    for p in parts:
        print(f"d1: {p.name} ({p.stat().st_size} bytes)")
    return 0


def _rows_written(text: str) -> int:
    import argparse

    from .d1 import rows_written

    try:
        return rows_written(text or "0")
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def cmd_d1_load(args) -> int:
    from .d1 import BUDGET, Wrangler, load

    failed = load(
        Path(args.dir),
        Wrangler(),
        budget=args.budget or BUDGET,
        retry=set(args.retry.split()),
        summary=Path(args.summary) if args.summary else None,
    )
    print(f"d1 load: {failed} version(s) did not load")
    return 1 if failed else 0


def cmd_store(args) -> int:
    from .r2 import pull_store, push

    store_dir = Path(args.store)
    if args.sub == "pull":
        cached = _cached_versions(store_dir, Path(args.cache)) if args.cache else set()
        n = pull_store(store_dir, only=_with_layers(args.only), skip=cached)
        print(f"store pull: {n} file(s), {len(cached)} version(s) already built in the cache")
    else:
        # A run that failed before its PR can leave bytes under a version main never took.
        committed = _committed_versions(store_dir)
        n = 0
        for src in sorted(store_dir.glob("*/*/source.*")):
            v = (src.parts[-3], src.parts[-2])
            n += push(
                src.parent,
                "publicdata-raw",
                f"{v[0]}/{v[1]}/",
                immutable=lambda key, v=v: v in committed,
            )
        print(f"store push: {n} file(s)")
    return 0


def _committed_versions(store_dir: Path) -> set[tuple[str, str]]:
    """The versions whose manifest git tracks; outside a checkout, every version on disk."""
    import subprocess

    found = [p.relative_to(store_dir) for p in store_dir.glob("*/*/manifest.json")]
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--", "*/*/manifest.json"],
            cwd=store_dir,
            capture_output=True,
            check=True,
        ).stdout
    except OSError, subprocess.CalledProcessError:
        return {(p.parts[0], p.parts[1]) for p in found}
    return {tuple(Path(f).parts[:2]) for f in out.decode().split("\0") if f}


def _with_layers(only: list[str]) -> tuple[str, ...]:
    """The slugs, and the spine layers any of them joins, whose sources a joined build reads."""
    from .register import load
    from .spine import LAYERS

    if not only:
        return ()
    joins = {LAYERS[k].slug for d in load(REGISTER) if d.slug in only for k in d.enrich}
    return tuple(sorted({*only, *joins}))


def _cached_versions(store_dir: Path, cache_dir: Path) -> set[tuple[str, str]]:
    """The versions the build will take from the cache, so their source bytes are not needed."""
    from . import store
    from .build import version_key
    from .cache import BuildCache
    from .register import load
    from .spine import LAYERS

    cache = BuildCache(cache_dir)
    datasets = [d for d in load(REGISTER) if d.publishable]
    cached = {
        (d.slug, m.version)
        for d in datasets
        for m in store.manifests(store_dir, d.slug)
        if cache.has(version_key(cache, d, m, store_dir))
    }
    # A spine-joined version the cache cannot serve reads each layer's newest source.
    needs = {
        k
        for d in datasets
        for m in store.manifests(store_dir, d.slug)
        if d.enrich and (d.slug, m.version) not in cached
        for k in d.enrich
    }
    for k in needs:
        ms = store.manifests(store_dir, LAYERS[k].slug)
        if ms:
            cached.discard((LAYERS[k].slug, ms[-1].version))
    return cached


VERSION_PREFIX = re.compile(r"^d/[a-z0-9][a-z0-9-]*/v/\d{4}-\d{2}-\d{2}/$")


def cmd_spine_install(args) -> int:
    from .extension import install

    install()
    print("spine: DuckDB spatial extension installed")
    return 0


def cmd_spine_mirror(args) -> int:
    from .extension import mirror

    pin = mirror(Path(args.pin))
    print(f"spine: pinned {pin['url']} ({pin['sha256']})")
    return 0


def cmd_dist_push(args) -> int:
    from .r2 import dated_file, push

    bad = [x for x in args.replace if not VERSION_PREFIX.match(x)]
    if bad:
        print(f"dist push: --replace takes d/<slug>/v/<date>/ prefixes only, not {bad}")
        return 2
    from .r2 import check_sources
    from .register import load
    from .serialise.profile import layout

    # Before anything goes up, so a version whose source the site cannot serve is never published.
    found = check_sources([Path(args.large)])
    expect = _absent(args.expect)
    queries = (Path(args.large) / "_q").is_dir() or any(k.startswith("_q/") for k in expect)
    n = push(
        Path(args.large),
        "publicdata-dist",
        replace=tuple(args.replace),
        immutable=dated_file,
        expect=expect,
        include=dated_file if args.dated_only else lambda key: True,
        layouts={ds.slug: layout(ds) for ds in load(REGISTER)} if queries else None,
    )
    print(
        f"dist push: {n} file(s){' (replacing under ' + ', '.join(args.replace) + ')' if args.replace else ''}"
    )
    print(f"dist push: {found} publisher's file(s) found in the raw store")
    return 0


def cmd_r2_restore_gzip(args) -> int:
    from .r2 import restore_gzip

    t = restore_gzip(
        prefix=args.prefix,
        apply=args.apply,
        workers=args.workers,
        dedupe_csv_gz=args.dedupe_csv_gz,
    )
    return 1 if t["failed"] else 0


def cmd_checksums(args) -> int:
    from .checksums import KEY, slugs_in, slugs_in_bucket, update, write_subjects
    from .r2 import client

    bad = [x for x in args.replace if not VERSION_PREFIX.match(x)]
    if bad:
        print(f"checksums: --replace takes d/<slug>/v/<date>/ prefixes only, not {bad}")
        return 2
    if not args.all and not args.root:
        print("checksums: name the built trees with --root, or pass --all")
        return 2
    s3 = client()
    slugs = slugs_in_bucket(s3) if args.all else slugs_in(Path(r) for r in args.root)
    lists: dict[str, str] = {}
    n, held, changed = update(
        slugs,
        replace=tuple(args.replace),
        download=args.download,
        s3=s3,
        lists=lists,
        every=args.resign,
    )
    print(f"checksums: {n} SHA256SUMS written over {len(slugs)} dataset(s)")
    if args.subjects:
        parts = write_subjects(lists, Path(args.subjects))
        print(f"checksums: {len(lists)} list(s) to attest in {len(parts)} part(s)")
    for prefix in held:
        print(f"::warning::{prefix}SHA256SUMS not written: a file has no stored SHA-256")
    # A dated file changes only under a replace, which writes the list again. Anything else
    # breaks that rule, so it is reported and the version's list keeps what it was first given.
    for key in changed:
        slug, version, _ = KEY.match(key).groups()
        prefix = f"d/{slug}/v/{version}/"
        print(
            f"::warning::{key} was written after {prefix}SHA256SUMS, outside a replace. "
            "A dated version's files never change, so its list is left as it is"
        )
    if held:
        print(
            f"checksums: {len(held)} version(s) left without one; "
            "run the Checksums workflow to hash their files from R2 and sign the lists"
        )
    return 0


def cmd_purge(args) -> int:
    import os

    from .edge import purge, with_answers

    bad = [x for x in args.prefix if not VERSION_PREFIX.match(x)]
    if bad:
        print(f"purge: takes d/<slug>/v/<date>/ prefixes only, not {bad}")
        return 2
    token = os.environ.get("CLOUDFLARE_PURGE_TOKEN", "")
    if not token:
        print("purge: CLOUDFLARE_PURGE_TOKEN is not set")
        return 2
    print(f"purge: {purge(with_answers(args.prefix), token)} prefix(es) purged from the edge")
    return 0


def _prune_unusable(cache, datasets, store_dir: Path) -> int:
    """Removes the entries no version in the store keys to, which reads the manifests only."""
    from .brand import CARD_PREFIX
    from .build import cache_keys

    usable = set().union(*(cache_keys(cache, d, store_dir) for d in datasets))
    if cache.root.is_dir():
        usable |= {p.name for p in cache.root.glob(CARD_PREFIX + "*")}
    return cache.prune(usable)


def cmd_cache_prune(args) -> int:
    from .cache import BuildCache
    from .register import load

    n = _prune_unusable(BuildCache(Path(args.cache)), load(REGISTER), Path(args.store))
    print(f"cache: {n} entries no version in the store can use removed")
    return 0


def cmd_cache(args) -> int:
    from .r2 import cache_pull, cache_push

    root = Path(args.cache)
    if args.sub == "pull":
        entries = None
        if args.only:
            from .build import cache_keys
            from .cache import BuildCache
            from .register import load

            if not args.store:
                print("cache pull: --only needs --store")
                return 2
            cache = BuildCache(root)
            entries = set().union(
                *(
                    cache_keys(cache, d, Path(args.store))
                    for d in load(REGISTER)
                    if d.slug in args.only
                )
            )
        n = cache_pull(root, meta_only=args.meta_only, entries=entries)
        print(f"cache pull: {n} entries")
    else:
        up, gone = cache_push(root, prune=args.prune)
        print(
            f"cache push: {up} file(s) uploaded, {gone} entries unused past the grace period deleted"
        )
    return 0


def cmd_verify(args) -> int:
    """plan prints the datasets the real-data check builds, or nothing when no changed path
    shapes versions outside their key; run builds them and compares."""
    from . import verify
    from .register import load

    datasets = load(REGISTER)
    store_dir = Path(args.store)
    mb = 1_000_000
    cap = args.cap_mb * mb if args.cap_mb is not None else verify.CAP
    if args.sub == "plan":
        changed = []
        if args.changed:
            changed = Path(args.changed).read_text(encoding="utf-8").splitlines()
        if args.before and (moved := verify.changed_defaults(Path(args.before))):
            for k in moved:
                print(
                    f"::error::the default of {k} changed. A version's key leaves out every "
                    "field at its default, so raise REBUILD in pipeline/publicdata/cache.py "
                    "in the same change.",
                    file=sys.stderr,
                )
            return 1
        if args.before and (
            bare := verify.unnoted_partitions(Path(args.before), datasets, store_dir, changed)
        ):
            for slug, prefixes in bare.items():
                print(
                    f"::error::{slug}: partition_by changed, which adds or drops by/ files in "
                    f"{len(prefixes)} published version(s). That is a correction "
                    "(docs/CORRECTIONS.md): add a dated note to each version's manifest in "
                    "store/ in the same change, and once it is merged run the Deploy workflow "
                    f"with replace set to {' '.join(prefixes)}",
                    file=sys.stderr,
                )
            return 1
        mods = verify.unkeyed(changed)
        raised = verify.bumped(Path(args.before), datasets) if args.before else []
        if not mods and not raised:
            print("verify: no change to the build code outside the keys", file=sys.stderr)
            return 0
        budget = args.budget_mb * mb if args.budget_mb is not None else verify.BUDGET
        if raised:
            print(f"verify: rebuild raised for {' '.join(raised)}", file=sys.stderr)
        if mods:
            print(f"verify: {', '.join(mods)} changed", file=sys.stderr)
        else:
            budget = 0  # only the raised entries are checked
        slugs = verify.sample(datasets, store_dir, args.seed, budget, cap, raised)
        if mods:
            for line in verify.uncovered(datasets, store_dir, slugs):
                print(f"verify: no dataset in the sample is built as {line}", file=sys.stderr)
        if not slugs and not mods:
            return 0
        if not slugs:
            print(
                "verify: no stored dataset fits the budget, so nothing can be checked",
                file=sys.stderr,
            )
            return 1
        print(" ".join(slugs))
        return 0
    if not args.cache:
        print("verify run: --cache is the build cache a deploy would reuse")
        return 2
    chosen = [d for d in datasets if d.publishable and (not args.slug or d.slug in args.slug)]
    unknown = set(args.slug) - {d.slug for d in chosen}
    if unknown:
        print(f"verify run: not a publishable dataset: {sorted(unknown)}")
        return 2
    from . import published, serialise

    serialise.LIMIT = None
    import tempfile

    out = Path(args.out or tempfile.mkdtemp(prefix="publicdata-verify-"))
    out.mkdir(parents=True, exist_ok=True)
    download = None
    if args.published.startswith(R2):
        from .r2 import downloader

        download = downloader(args.published[len(R2) :])
    source = Path(args.published) if args.published and not download else None
    # A capped version keeps the format set its published manifest records.
    published.current = published.Published(out, [], source, download)
    try:
        return verify.run(chosen, store_dir, Path(args.cache), out, cap)
    finally:
        published.current = None


def cmd_shards(args) -> int:
    """Prints a JSON list of build jobs, each a space-separated list of dataset slugs."""
    import json

    from .register import load
    from .shards import plan, weights

    w = weights(load(REGISTER), Path(args.store), Path(args.cache) if args.cache else None)
    jobs = plan(w, args.count)
    for i, slugs in enumerate(jobs):
        mb = sum(w[s] for s in slugs) / 1e6
        print(f"shard {i}: {len(slugs)} dataset(s), {mb:.0f} MB of source", file=sys.stderr)
    print(json.dumps([" ".join(slugs) for slugs in jobs]))
    return 0


def _hubs_record(store_dir: Path) -> dict:
    """Where the Hubs job found each copy, committed beside the manifests."""
    import json

    path = store_dir / "hubs.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def _tasks(path: str | None) -> dict[str, int]:
    """The open contributor issues by dataset key; a build without the file shows none."""
    import json

    return json.loads(Path(path).read_text("utf-8")) if path else {}


def cmd_hubs(args) -> int:
    from . import hubs
    from .register import load

    only = set(args.only) if args.only else None
    registered = {d.slug: d for d in load(REGISTER)}
    entries = list(hubs.read_site(args.site, only, register=registered))
    if args.render:
        hubs.render(entries, Path(args.render))
        print(f"hubs: rendered {len(entries)} dataset(s) into {args.render}")
        return 0
    available, skipped = hubs.configured()
    for line in skipped:
        print(line)
    chosen = {n: make() for n, make in available.items() if n in args.hub}
    import json

    path = Path(args.record) if args.record else None
    old = json.loads(path.read_text("utf-8")) if path and path.exists() else {}

    def save(found: dict) -> dict:
        # Written after every dataset, so a run stopped part way keeps what it recorded, and
        # swapped into place whole, so a stop mid-write leaves the last good record.
        merged = hubs.merge_record(old, found)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", "utf-8")
        os.replace(tmp, path)
        return merged

    found: dict = {}
    failures = hubs.run(
        chosen, entries, refresh=args.refresh, record=found, on_record=save if path else None
    )
    if path:
        merged = save(found)
        print(f"hubs: recorded {len(merged['datasets'])} dataset(s) in {path}")
    print(f"hubs: {len(entries)} dataset(s), {len(chosen)} hub(s), {failures} failure(s)")
    return 1 if failures else 0


def cmd_contribute(args) -> int:
    """Keep one issue open for each of the most-wanted datasets, or list the open ones."""
    import json

    from . import contribute
    from .register import load

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    gh = contribute.GitHub(args.repo, contribute.session(token))
    if args.sub == "issues":
        tasks = contribute.open_tasks(gh.issues("open"))
        Path(args.out).write_text(json.dumps(tasks, indent=1) + "\n", encoding="utf-8")
        print(f"contribute: {len(tasks)} open task(s) written to {args.out}")
        return 0
    if args.votes < 1 or args.cap < 0:
        sys.exit("contribute: --votes is at least 1 and --cap at least 0")
    # Issues opened under any other account are invisible to the next scheduled run.
    if not args.dry_run and os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit("contribute: only the Contribute workflow changes issues; pass --dry-run")
    try:
        acts = contribute.sync(
            load(REGISTER),
            gh,
            contribute.session(),
            threshold=args.votes,
            cap=args.cap,
            dry_run=args.dry_run,
            site=args.site,
        )
    except contribute.SyncError as e:
        print(f"contribute: {e}", file=sys.stderr)
        return 1
    print(f"contribute: {len(acts)} change(s){' planned' if args.dry_run else ''}")
    return 0


def cmd_cost(args) -> int:
    import datetime as dt

    from . import cost
    from .register import load

    root = Path(args.root).resolve() if args.root else ROOT
    register = root / "register"
    changed, fresh, reshaped = set(args.slug), set(), {}
    if args.base:
        if links := cost.symlinks(root):
            print(f"cost: the register may not hold symbolic links: {', '.join(links)}")
            return 2
        base, paths = cost.changed_paths(args.base, root)
        entries = cost.changed_entries(register, paths, root)
        priced = cost.costed(root, base, entries)
        if same := sorted(set(entries) - priced):
            print(f"cost: {', '.join(same)}: edited, but nothing the projection reads changed")
        changed |= priced
        fresh, reshaped = cost.entry_changes(root, base, {s: entries[s] for s in priced})
    approve = None
    if args.github_pr:
        repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]
        approve = lambda: cost.approval(repo, args.github_pr, lambda p: cost._github(p, token))  # noqa: E731
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    return cost.run(
        load(register),
        Path(args.store),
        args.catalog or cost.CATALOG,
        changed,
        today,
        approved=args.approved,
        probing=args.probe,
        fresh=fresh,
        summary=args.summary,
        reshaped=reshaped,
        approve=approve,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="publicdata")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register").add_subparsers(dest="sub", required=True)
    rv = r.add_parser("validate")
    rv.add_argument(
        "--store", default=str(STORE), help="the store whose versions int32 is checked against"
    )
    rv.add_argument(
        "--built",
        action="append",
        default=[],
        help="a built tree whose Parquet int32 is checked against, before the store's sources",
    )
    rv.set_defaults(fn=cmd_register)
    lb = r.add_parser("labels", help="draft labels for fields that have none")
    lb.add_argument("slug", nargs="*")
    lb.add_argument("--write", action="store_true", help="write the drafts into the register YAML")
    lb.set_defaults(fn=cmd_labels)
    dr = r.add_parser("draft", help="draft an entry from a CKAN portal dataset URL")
    dr.add_argument("url")
    dr.add_argument("--resource", default="", help="the resource id, when not the first CSV")
    dr.add_argument("--slug", default="", help="the entry's slug, when not drawn from the title")
    dr.add_argument("--sheet", default="", help="the workbook sheet, for an Excel resource")
    dr.add_argument("--header-row", type=int, default=1, help="the workbook row holding headers")
    dr.add_argument("--stdout", action="store_true", help="print the entry instead of writing it")
    dr.set_defaults(fn=cmd_draft)
    f = sub.add_parser("fetch")
    f.add_argument("slug", nargs="*")
    f.add_argument("--store", default=str(STORE))
    f.add_argument(
        "--groups", help="write the new manifests to this JSON file, grouped by jurisdiction"
    )
    f.add_argument(
        "--feeds", action="store_true", help="fetch only the live feeds, as the daily run does"
    )
    f.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="SLUG=PATH",
        help="a manual source's file, or a stack's folder of files, downloaded by a person from the URL the fetch reported",
    )
    f.set_defaults(fn=cmd_fetch)
    b = sub.add_parser("build")
    b.add_argument("slug", nargs="*")
    b.add_argument("--store", default=str(STORE))
    b.add_argument("--out", default=str(ROOT / "dist"))
    b.add_argument("--fixtures", action="store_true")
    b.add_argument("--cache", help="reuse built versions from this directory and keep them there")
    b.add_argument("--search", help="also write the catalogue search index for D1 to this file")
    b.add_argument(
        "--absent", help="with --cache, list here the published files a cached version leaves out"
    )
    b.add_argument(
        "--formats",
        help="write only these formats, comma separated, with ndjson and parquet always; "
        "CI uses it to prove a cached version grows into the full set",
    )
    b.add_argument(
        "--no-site",
        action="store_true",
        help="build the datasets' files and no pages, as a deploy shard does",
    )
    b.add_argument(
        "--built",
        action="append",
        default=[],
        metavar="DIR",
        help="a shard's built tree; a file the cache leaves out is linked in from it when there",
    )
    b.add_argument(
        "--tasks",
        metavar="FILE",
        help="the open contributor issues by dataset key, as `contribute issues` writes them",
    )
    b.add_argument(
        "--published",
        default="",
        metavar="DIR|r2://BUCKET",
        help="where a cached version's Parquet is read back from, as the site lays it out",
    )
    b.set_defaults(fn=cmd_build)
    ck = sub.add_parser("checksums", help="write SHA256SUMS beside each dated version in R2")
    ck.add_argument(
        "--root", action="append", default=[], help="a built tree whose datasets to cover; repeat"
    )
    ck.add_argument("--all", action="store_true", help="every dataset R2 holds, as a backfill")
    ck.add_argument(
        "--download",
        action="store_true",
        help="read and hash a file R2 stored without its SHA-256, instead of skipping its version",
    )
    ck.add_argument(
        "--replace",
        nargs="*",
        default=[],
        metavar="PREFIX",
        help="versions a replace deploy rewrote, whose lists are made again from scratch",
    )
    ck.add_argument(
        "--subjects", metavar="DIR", help="write the lists to attest here, 1.sha256 and on"
    )
    ck.add_argument(
        "--resign",
        action="store_true",
        help="add every list left as it is to --subjects, to attest it again; no list is rewritten",
    )
    ck.set_defaults(fn=cmd_checksums)
    pg = sub.add_parser(
        "purge", help="purge replaced versions and their query API answers from the edge cache"
    )
    pg.add_argument("prefix", nargs="+", help="d/<slug>/v/<date>/ prefixes")
    pg.set_defaults(fn=cmd_purge)
    sh = sub.add_parser("shards", help="split the versions the cache cannot serve over build jobs")
    sh.add_argument("--store", default=str(STORE))
    sh.add_argument("--cache", help="the build cache; without it every version is built")
    sh.add_argument("--count", type=int, default=4)
    sh.set_defaults(fn=cmd_shards)
    vf = sub.add_parser(
        "verify", help="build stored versions with this code and compare what a deploy reuses"
    )
    vf.add_argument("sub", choices=["plan", "run"])
    vf.add_argument("slug", nargs="*", help="run: the datasets to check; every one without")
    vf.add_argument("--store", default=str(STORE))
    vf.add_argument("--cache", help="run: the build cache a deploy would reuse")
    vf.add_argument("--out", default="", help="run: where each build goes; a temporary folder")
    vf.add_argument("--published", default="", help="run: the published tree, or r2://<bucket>")
    vf.add_argument("--changed", help="plan: a file listing the changed paths, one per line")
    vf.add_argument("--seed", default="", help="plan: picks the datasets beyond one per stratum")
    vf.add_argument(
        "--before",
        help="plan: a folder with the base's register.py and cache.py, and its copy of each "
        "changed register entry at its path, to compare",
    )
    vf.add_argument("--budget-mb", type=int, help="plan: source megabytes to build (300)")
    vf.add_argument(
        "--cap-mb", type=int, help="the source MB of a dataset's newest versions checked (60)"
    )
    vf.set_defaults(fn=cmd_verify)
    cc = sub.add_parser("cache", help="copy the build cache between this disk and R2")
    cc.add_argument("sub", choices=["pull", "push"])
    cc.add_argument("--cache", required=True)
    cc.add_argument("--meta-only", action="store_true", help="pull each entry's record alone")
    cc.add_argument("--store", help="the store whose manifests --only reads")
    cc.add_argument(
        "--only", nargs="+", metavar="SLUG", help="pull only the entries these datasets can use"
    )
    cc.add_argument(
        "--prune",
        action="store_true",
        help="push, and delete the entries this disk has dropped once they stay unused a day",
    )
    cc.set_defaults(fn=cmd_cache)
    cpr = sub.add_parser("cache-prune", help="drop build cache entries no stored version uses")
    cpr.add_argument("--store", default=str(STORE))
    cpr.add_argument("--cache", required=True)
    cpr.set_defaults(fn=cmd_cache_prune)
    g = sub.add_parser("gate")
    g.add_argument("out", nargs="?", default=str(ROOT / "dist"))
    g.add_argument("--absent", help="the build's list of published files it left out")
    g.add_argument(
        "--versions-only", action="store_true", help="check the dated versions, not the pages"
    )
    g.set_defaults(fn=cmd_gate)
    s = sub.add_parser("split")
    s.add_argument("--out", default=str(ROOT / "dist"))
    s.add_argument("--large", default=str(ROOT / "dist-large"))
    s.add_argument("--limit-mib", type=float, default=24)
    s.add_argument("--versioned", action="store_true", help="also move every dated version file")
    s.set_defaults(fn=cmd_split)
    ca = sub.add_parser("catalogue").add_subparsers(dest="sub", required=True)
    cf = ca.add_parser("fetch", help="harvest every portal's dataset list into the store")
    cf.add_argument("--store", default=str(STORE))
    cf.set_defaults(fn=cmd_catalogue)
    cp = ca.add_parser(
        "publishers", help="propose curation for uncurated data.gov.au organisations"
    )
    cp.add_argument("--store", default=str(STORE))
    cp.set_defaults(fn=cmd_catalogue_publishers)
    d1 = sub.add_parser("d1").add_subparsers(dest="sub", required=True)
    d1s = d1.add_parser("sql", help="write D1 load files for versions the query API lacks")
    d1s.add_argument("--root", action="append", required=True, help="a built tree; repeat for each")
    d1s.add_argument("--loaded", help="JSON of loaded slug and version rows, as wrangler prints it")
    d1s.add_argument("--out", required=True)
    d1s.add_argument("--stamp", default="", help="a per-run id written into every part")
    d1s.add_argument("--catalogue", help="the catalogue search index the build wrote")
    d1s.set_defaults(fn=cmd_d1)
    d1l = d1.add_parser("load", help="run the load files against D1, verify and retry")
    d1l.add_argument("--dir", required=True)
    d1l.add_argument(
        "--budget",
        type=_rows_written,
        default=0,
        help="rows written this deploy may plan, such as 10M (0: the default)",
    )
    d1l.add_argument(
        "--retry", default="", help="dataset slugs, or all, to load again past their failures"
    )
    d1l.add_argument("--summary", help="a Markdown file to append the load plan to")
    d1l.set_defaults(fn=cmd_d1_load)
    st = sub.add_parser("store")
    st.add_argument("sub", choices=["pull", "push"])
    st.add_argument("--store", default=str(STORE))
    st.add_argument(
        "--only", nargs="*", default=[], help="dataset slugs to pull, such as catalogue"
    )
    st.add_argument("--cache", help="skip versions this build cache already holds")
    st.set_defaults(fn=cmd_store)
    dp = sub.add_parser("dist-push")
    dp.add_argument("--large", default=str(ROOT / "dist-large"))
    dp.add_argument(
        "--replace",
        nargs="*",
        default=[],
        metavar="PREFIX",
        help="overwrite existing keys under these prefixes, e.g. d/<slug>/v/<date>/",
    )
    dp.add_argument(
        "--expect", help="the build's list of files it left out, each of which R2 must already hold"
    )
    dp.add_argument(
        "--dated-only",
        action="store_true",
        help="push only dated version files, leaving pages to the deploy that builds them all",
    )
    dp.set_defaults(fn=cmd_dist_push)
    rr = sub.add_parser("r2").add_subparsers(dest="sub", required=True)
    rg = rr.add_parser(
        "restore-gzip",
        help="store the dated text files in publicdata-dist gzipped, in place; a dry run unless --apply",
    )
    rg.add_argument("--prefix", default="d/", help="only keys under this prefix, e.g. d/<slug>/")
    rg.add_argument("--apply", action="store_true", help="rewrite the objects, not just count them")
    rg.add_argument("--workers", type=int, default=4)
    rg.add_argument(
        "--dedupe-csv-gz",
        action="store_true",
        help="delete a version's data.csv.gz once the gzipped data.csv beside it holds its bytes",
    )
    rg.set_defaults(fn=cmd_r2_restore_gzip)
    sp = sub.add_parser("spine").add_subparsers(dest="sub", required=True)
    sp.add_parser(
        "install", help="fetch DuckDB's spatial extension so builds stay offline"
    ).set_defaults(fn=cmd_spine_install)
    sm = sp.add_parser(
        "mirror", help="copy the spatial extension for the installed DuckDB to R2 and pin it"
    )
    sm.add_argument("--pin", required=True, help="the spatial-extension.json to write")
    sm.set_defaults(fn=cmd_spine_mirror)
    hb = sub.add_parser("hubs", help="copy each dataset's newest version to the data hubs")
    hb.add_argument("--site", default="https://publicdata.au")
    hb.add_argument("--hub", nargs="*", default=["huggingface", "zenodo", "kaggle"])
    hb.add_argument("--only", nargs="*", metavar="SLUG")
    hb.add_argument("--render", metavar="DIR", help="write what each hub would receive, and stop")
    hb.add_argument(
        "--record", metavar="FILE", help="merge where each copy lives into this JSON file"
    )
    hb.add_argument(
        "--refresh",
        action="store_true",
        help="re-apply cards, page settings and notebooks to versions a hub already holds",
    )
    hb.set_defaults(fn=cmd_hubs)

    cb = sub.add_parser("contribute", help="the most-wanted datasets as contributor issues")
    cb.add_argument("sub", choices=["sync", "issues"])
    cb.add_argument("--repo", default=REPO.removeprefix("https://github.com/"))
    cb.add_argument("--site", default=SITE)
    cb.add_argument("--votes", type=int, default=1, help="sync: votes a catalogue record needs")
    cb.add_argument("--cap", type=int, default=10, help="sync: the most issues open at once")
    cb.add_argument("--dry-run", action="store_true", help="sync: print the changes, make none")
    cb.add_argument("--out", default="contribute.json", help="issues: where the open ones go")
    cb.set_defaults(fn=cmd_contribute)
    co = sub.add_parser(
        "cost", help="project each entry's storage growth and D1 writes and gate changed ones"
    )
    co.add_argument("slug", nargs="*", help="entries to gate, as well as those --base finds")
    co.add_argument("--base", help="gate the register entries changed since this ref")
    co.add_argument("--store", default=str(STORE))
    co.add_argument(
        "--root", help="the checkout whose register and history are read (default this one)"
    )
    co.add_argument("--catalog", help="a catalog.json path or URL (default the live site's)")
    co.add_argument("--today", help="the date versions are counted back from (YYYY-MM-DD)")
    co.add_argument(
        "--approved", action="store_true", help="treat an over-budget entry as approved"
    )
    co.add_argument(
        "--github-pr", type=int, help="read this pull request's cost-approved label from GitHub"
    )
    co.add_argument(
        "--probe", action="store_true", help="size a new or moved source from its portal or host"
    )
    co.add_argument(
        "--summary", help="append the Markdown table here (default GITHUB_STEP_SUMMARY)"
    )
    co.set_defaults(fn=cmd_cost)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
