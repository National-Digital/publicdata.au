"""The real-data check on a change to the build code. A version's key leaves the code out, so a
deploy reuses every published version until a rebuild number is raised. This builds a sample of
stored datasets from their sources with the code as it stands and compares each version, diff and
history archive whose key a deploy would reuse with the build cache entry it would reuse, file by
file. A difference means the change alters published bytes without raising a rebuild number."""

from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

from . import store
from .cache import PACKAGE, BuildCache, _is_writer, code_files, digests, writer_keys
from .register import Dataset

REPO = PACKAGE.parents[1]
# A pull request's check builds about this many source bytes, and leaves out any dataset larger
# than CAP, so it runs in minutes on one runner.
BUDGET = 300_000_000
CAP = 60_000_000


def unkeyed(changed: list[str]) -> list[str]:
    """The changed paths, relative to the repository, that shape versions without being in
    their key. The format writers are keyed one by one, so an edit to one rebuilds its files."""
    mods = {p.relative_to(REPO).as_posix() for p in code_files() if not _is_writer(p)}
    return sorted(c for c in changed if c in mods)


def _layers_bytes(ds: Dataset, store_dir: Path) -> int:
    if not ds.enrich:
        return 0
    from .spine import LAYERS

    return sum(
        ms[-1].bytes for k in ds.enrich if (ms := store.manifests(store_dir, LAYERS[k].slug))
    )


def source_bytes(ds: Dataset, store_dir: Path) -> int:
    """What building every version of a dataset reads, the spine layers it joins included."""
    ms = store.manifests(store_dir, ds.slug) if ds.publishable else []
    return sum(max(m.bytes, 1) for m in ms) + _layers_bytes(ds, store_dir) if ms else 0


def _stratum(ds: Dataset) -> tuple:
    """What picks the code paths a dataset's build runs through."""
    from .serialise.geo import geo_kind

    return (
        ds.kind,
        ds.source.adapter,
        ds.source.format,
        geo_kind(ds),
        bool(ds.enrich),
        bool(ds.sort),
        bool(ds.partition_by),
        bool(ds.wide),
        bool(ds.unpivot),
        bool(ds.suppression),
    )


def sample(
    datasets: list[Dataset],
    store_dir: Path,
    seed: str,
    budget: int = BUDGET,
    cap: int = CAP,
) -> list[str]:
    """The datasets the check builds: the cheapest of each stratum first, so every adapter and
    shape that fits is covered, then others in an order the seed picks, while the budget lasts.
    Every version of a chosen dataset is built, so its diffs and history are checked too."""
    cost = {d.slug: c for d in datasets if (c := source_bytes(d, store_dir))}
    by = {d.slug: d for d in datasets if d.slug in cost}
    strata: dict[tuple, list[str]] = {}
    for s in sorted(by):
        strata.setdefault(_stratum(by[s]), []).append(s)
    chosen, total = [], 0

    def take(s: str) -> None:
        nonlocal total
        if s not in chosen and cost[s] <= cap and total + cost[s] <= budget:
            chosen.append(s)
            total += cost[s]

    for key in sorted(strata, key=repr):
        take(min(strata[key], key=lambda s: (cost[s], s)))
    rest = sorted(set(by) - set(chosen))
    random.Random(seed).shuffle(rest)
    for s in rest:
        take(s)
    return sorted(chosen)


def _entry(cache: BuildCache, key: str) -> dict | None:
    p = cache.root / key / "meta.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _plain(v):
    return json.loads(json.dumps(v, ensure_ascii=False))


def _version(ds: Dataset, meta: dict, vout, vdir: Path, entry: Path) -> list[str]:
    """How a version built now differs from the cache entry a deploy would reuse for it."""
    from .build import _want

    now = writer_keys()
    seen = meta.get("writers", {})
    # A format whose writer changed is written again into the reused version, so it is not
    # compared; a changed Parquet writer rebuilds the version, which is never reused.
    stale = (
        set()
        if ds.kind == "database"
        else {f for f in _want(ds, meta["rows"], meta.get("left_out")) if seen.get(f) != now[f]}
    )
    if "parquet" in stale:
        return []
    skip = {f"data.{f}" for f in stale}
    if stale and meta.get("left_out") is not None:
        skip.add("manifest.json")  # a rewritten file it was measured on is measured again
    out = []
    for name in ("rows", "unknown_columns", "suppressed_cells", "partitions", "tables"):
        old, new = meta.get(name), _plain(getattr(vout, name))
        if name == "tables" and old is None:
            old = {}
        if old != new:
            out.append(f"{name} {json.dumps(old)} published, {json.dumps(new)} built now")
    if "data.ndjson" not in skip and meta.get("first", "") != vout.first:
        out.append("the first row of data.ndjson")
    if _plain(vout.left_out) != meta.get("left_out"):
        out.append("formats_left_out")
    old_files, new_files = meta.get("files", {}), vout.files
    for rel in sorted(set(old_files) | set(new_files)):
        if rel in skip:
            continue
        if rel not in new_files:
            out.append(f"{rel} is published and no longer built")
        elif rel not in old_files:
            out.append(f"{rel} is built now and was never published")
        elif old_files[rel] != new_files[rel]:
            out.append(f"{rel} is {new_files[rel]} bytes, {old_files[rel]} published")
    sums = meta.get("sha256")
    if sums is None:
        # An entry from before the record kept digests holds the small files alone.
        sums = digests(entry / "files")
    now_sums = digests(vdir, [r for r in sums if r not in skip and r in new_files])
    for rel, h in sorted(now_sums.items()):
        if sums[rel] != h and not any(line.startswith(f"{rel} ") for line in out):
            out.append(f"{rel} differs")
    return out


def check(ds: Dataset, store_dir: Path, cache: BuildCache, out: Path) -> tuple[list[str], int, int]:
    """One dataset built from its sources and compared with the entries a deploy would reuse.
    Returns the differences, each naming its file, and how many versions were compared and how
    many a deploy would build again anyway."""
    from .build import build_dataset, version_key

    fresh = build_dataset(ds, store_dir, out)
    problems: list[str] = []
    compared = rebuilt = 0
    keys = []
    for v in fresh.versions:
        m = v.manifest
        key = version_key(cache, ds, m, store_dir)
        keys.append(key)
        where = f"d/{ds.slug}/v/{m.version}/"
        meta = _entry(cache, key)
        if meta is None:
            rebuilt += 1
            continue
        compared += 1
        diffs = _version(ds, meta, v, out / where, cache.root / key)
        problems += [f"{where}: {d}" for d in diffs]
    pairs = list(zip(fresh.versions, keys, strict=True))
    for (a, ka), (b, kb) in zip(pairs, pairs[1:], strict=False):
        rel = f"d/{ds.slug}/diff/{a.manifest.version}..{b.manifest.version}.json"
        meta = _entry(cache, cache.key(ka, kb, "diff"))
        if meta is not None and meta != json.loads((out / rel).read_text(encoding="utf-8")):
            problems.append(f"{rel}: differs")
    if keys:
        hkey = cache.key(*keys, "history")
        rel = f"d/{ds.slug}/history.tar.zst"
        old = cache.root / hkey / "files" / "history.tar.zst"
        if old.is_file() and old.read_bytes() != (out / rel).read_bytes():
            problems.append(f"{rel}: differs")
    return problems, compared, rebuilt


def run(datasets: list[Dataset], store_dir: Path, cache_dir: Path, out: Path) -> int:
    from .serialise.profile import QUERY_DIR

    cache = BuildCache(cache_dir)
    problems, compared, rebuilt = [], 0, 0
    for ds in datasets:
        p, c, r = check(ds, store_dir, cache, out)
        print(f"verify: {ds.slug}: {c} version(s) compared, {r} to be built again")
        problems += [(ds, x) for x in p]
        compared += c
        rebuilt += r
        # Each dataset's tree goes once it is compared, so the sample never fills the disk.
        for d in (out / "d" / ds.slug, out / QUERY_DIR / ds.slug):
            shutil.rmtree(d, ignore_errors=True)
    print(
        f"verify: {len(datasets)} dataset(s), {compared} version(s) compared with what a deploy "
        f"would reuse, {rebuilt} to be built again"
    )
    if not problems:
        return 0
    for _ds, line in problems:
        print(f"::error::{line}")
    named = sorted({ds.slug: ds for ds, _ in problems}.items())
    print(
        f"verify: this change alters the published files of {len(named)} dataset(s) whose "
        "versions a deploy would reuse unchanged. Raise `rebuild` in each one's register entry, "
        "or REBUILD in pipeline/publicdata/cache.py when the change reaches across datasets:"
    )
    for slug, ds in named:
        where = Path(ds.path or f"{slug}.yaml")
        where = where.relative_to(REPO) if where.is_relative_to(REPO) else where.name
        print(f"  {slug} ({where}, rebuild {ds.rebuild} now)")
    return 1
