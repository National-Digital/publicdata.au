"""The real-data check on a change to the build code. A version's key leaves the code out, so a
deploy reuses every published version until a rebuild number is raised. This builds a sample of
stored datasets from their sources with the code as it stands and compares each version, its
query copy, diff and history archive whose key a deploy would reuse with the build cache entry it
would reuse, file by file. A difference means the change alters published bytes without raising a
rebuild number. The diffs, the history archive and a format grown into a reused version are made
from the Parquet the site already serves, here as in a deploy, so old bytes in R2 are no
difference."""

from __future__ import annotations

import ast
import json
import random
import shutil
import tempfile
from pathlib import Path

from . import published, store
from .cache import (
    KIND_MODULES,
    PACKAGE,
    BuildCache,
    _is_writer,
    _writers_dir,
    code_files,
    digest,
    digests,
    shape_layer,
    writer_files,
    writer_keys,
)
from .register import Dataset

REPO = PACKAGE.parents[1]
# A pull request's check builds about this many source bytes, and of each dataset only the newest
# versions that fit in CAP, so it runs in minutes on one runner.
BUDGET = 300_000_000
CAP = 60_000_000


def unkeyed(changed: list[str]) -> list[str]:
    """The changed paths, relative to the repository, that shape versions without being in
    their key. A writer module in a format's writer key is keyed, so an edit to it writes that
    format again; one no format's key reads is checked like the rest of the build code."""
    from .serialise import WRITERS

    keyed = {p for f in WRITERS for p in writer_files(f)}
    keyed |= {PACKAGE / n for names in KIND_MODULES.values() for n in names}
    writers = {p for p in _writers_dir().glob("*.py") if _is_writer(p)}
    mods = {p for p in code_files() if not _is_writer(p) and p not in keyed}
    mods |= writers - keyed
    rel = {p.relative_to(REPO).as_posix() for p in mods}
    return sorted(c for c in changed if c in rel)


def _defaults(src: str) -> dict[str, str]:
    """Each register dataclass field's default, as source text, by class and field."""
    out = {}
    for node in ast.parse(src).body:
        if not isinstance(node, ast.ClassDef):
            continue
        for st in node.body:
            if not isinstance(st, ast.AnnAssign) or st.value is None:
                continue
            if not isinstance(st.target, ast.Name):
                continue
            v = st.value
            if isinstance(v, ast.Call) and getattr(v.func, "id", "") == "field":
                kw = {k.arg: k.value for k in v.keywords}
                if isinstance(kw.get("repr"), ast.Constant) and kw["repr"].value is False:
                    continue  # out of every key, as it shapes no version
                v = kw.get("default") or kw.get("default_factory")
                if v is None:
                    continue
            out[f"{node.name}.{st.target.id}"] = ast.unparse(v)
    return out


def _rebuild_number(src: str) -> str:
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "REBUILD" for t in node.targets
        ):
            return ast.unparse(node.value)
    return ""


def changed_defaults(before: Path) -> list[str]:
    """The register fields whose default the change edits while REBUILD stays as it was. A
    version's key leaves out every field at its default, so such an edit would reuse versions
    the new default reshapes. before holds register.py and cache.py as the base had them."""
    old_reg, old_cache = before / "register.py", before / "cache.py"
    if not old_reg.is_file() or not old_cache.is_file():
        return []
    now = (PACKAGE / "cache.py").read_text(encoding="utf-8")
    if _rebuild_number(old_cache.read_text(encoding="utf-8")) != _rebuild_number(now):
        return []
    old = _defaults(old_reg.read_text(encoding="utf-8"))
    new = _defaults((PACKAGE / "register.py").read_text(encoding="utf-8"))
    return sorted(k for k in old.keys() & new.keys() if old[k] != new[k])


def bumped(before: Path, datasets: list[Dataset]) -> list[str]:
    """The datasets whose rebuild number the change moves. before holds the base's copy of each
    changed register entry at its path in the repository. Two changes that raise the same number
    merge without a conflict, so the later one is checked against the entries the earlier built."""
    import yaml

    out = []
    for ds in datasets:
        if not ds.path or not Path(ds.path).is_relative_to(REPO):
            continue
        old = before / Path(ds.path).relative_to(REPO)
        if not old.is_file():
            continue
        raw = yaml.safe_load(old.read_text(encoding="utf-8")) or {}
        if raw.get("rebuild", 0) != ds.rebuild:
            out.append(ds.slug)
    return sorted(out)


def checked_versions(ds: Dataset, store_dir: Path, cap: int = CAP) -> list:
    """The versions the check builds: the newest ones whose source bytes fit in cap, and always
    the newest. The spine layers a joined dataset reads are left out: every joined dataset shares
    them, and a sample without one never checks the join."""
    ms = store.manifests(store_dir, ds.slug) if ds.publishable else []
    out, total = [], 0
    for m in reversed(ms):
        total += max(m.bytes, 1)
        if out and total > cap:
            break
        out.append(m)
    return out[::-1]


def source_bytes(ds: Dataset, store_dir: Path, cap: int = CAP) -> int:
    """The source bytes of the versions the check builds."""
    return sum(max(m.bytes, 1) for m in checked_versions(ds, store_dir, cap))


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
    forced: list[str] | tuple[str, ...] = (),
) -> list[str]:
    """The datasets the check builds: those in forced, the cheapest of each stratum, so every
    adapter and shape that fits is covered, then others in an order the seed picks, while the
    budget lasts. Of each, the newest versions that fit in cap are built, with the diffs between
    them. One dataset whose newest version alone is over cap is added, also picked by the seed
    and from a stratum nothing else covers when there is one, so the largest datasets are
    checked in turn. A database is left out, since the module only it runs is in its key."""
    cost = {
        d.slug: c
        for d in datasets
        if d.kind != "database" and (c := source_bytes(d, store_dir, cap))
    }
    by = {d.slug: d for d in datasets if d.slug in cost}
    strata: dict[tuple, list[str]] = {}
    for s in sorted(by):
        strata.setdefault(_stratum(by[s]), []).append(s)
    chosen = [s for s in sorted(set(forced)) if s in cost]
    total = sum(cost[s] for s in chosen)

    def take(s: str) -> None:
        nonlocal total
        if s not in chosen and cost[s] <= cap and total + cost[s] <= budget:
            chosen.append(s)
            total += cost[s]

    for key in sorted(strata, key=repr):
        take(min(strata[key], key=lambda s: (cost[s], s)))
    rest = sorted(set(by) - set(chosen))
    rng = random.Random(seed)
    rng.shuffle(rest)
    for s in rest:
        take(s)
    large = sorted(s for s in by if cost[s] > cap)
    # A stratum no other pick covers comes first, so each is checked within a few runs.
    alone = [s for s in large if not set(strata[_stratum(by[s])]) & set(chosen)]
    if budget > 0 and large and not set(large) & set(chosen):
        chosen.append(rng.choice(alone or large))
    return sorted(chosen)


def uncovered(datasets: list[Dataset], store_dir: Path, chosen: list[str]) -> list[str]:
    """The strata with a stored dataset and none in the sample, each with its datasets."""
    by: dict[tuple, list[str]] = {}
    for d in datasets:
        if d.kind != "database" and source_bytes(d, store_dir):
            by.setdefault(_stratum(d), []).append(d.slug)
    picked = set(chosen)
    return [
        f"{', '.join(str(x) for x in key)}: {' '.join(sorted(slugs))}"
        for key, slugs in sorted(by.items(), key=lambda kv: repr(kv[0]))
        if not picked & set(slugs)
    ]


def _entry(cache: BuildCache, key: str) -> dict | None:
    p = cache.root / key / "meta.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _plain(v):
    return json.loads(json.dumps(v, ensure_ascii=False))


def _regrown(ds: Dataset, m, cache: BuildCache, key: str, out: Path, rels: list[str]):
    """The files a deploy grew into a reused version from the Parquet the site serves, made again
    the same way with the code as it stands, by name, or None when that Parquet is gone."""
    from .build import _built_table, _source_order, prov_header, version_url, write_formats
    from .serialise import WRITERS

    vrel = f"d/{ds.slug}/v/{m.version}"
    parquet, _ = published.served(out, f"{vrel}/data.parquet")
    if not parquet.is_file():
        return None
    tbl = _built_table(ds, m, out, parquet)
    if m.parquet.get("sort"):
        tbl = _source_order(tbl, cache, key, parquet)
        if tbl is None:
            return None
    base = version_url(ds.slug, m.version)
    names = {r[5:] for r in rels}
    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp)
        fmts = [f for f in WRITERS if f in names]
        write_formats(tbl, fmts, lambda n, r: prov_header(ds, m, n, base + r), vdir)
        return digests(vdir, rels)


def _unmeasured(p: Path) -> dict:
    man = json.loads(p.read_text(encoding="utf-8"))
    man.pop("measured_bytes", None)
    return man


def _version(
    ds: Dataset, meta: dict, vout, out: Path, entry: Path, cache: BuildCache, key: str
) -> list[str]:
    """How a version built now differs from the cache entry a deploy would reuse for it."""
    from .build import _want
    from .serialise import WRITERS

    vdir = out / "d" / ds.slug / "v" / vout.manifest.version
    now = writer_keys(shape_layer(ds))
    seen = meta.get("writers", {})
    want = set() if ds.kind == "database" else set(_want(ds, meta["rows"], meta.get("left_out")))
    # A format whose writer changed is written again into the reused version, so it is not
    # compared; a changed Parquet writer rebuilds the version, which is never reused.
    stale = {f for f in want if seen.get(f) != now[f]}
    if "parquet" in stale:
        return []
    skip = {f"data.{f}" for f in stale}
    # A rewritten or grown file the manifest measured is measured again, so its sizes are left
    # out of the comparison and the rest of it is compared.
    masked = bool(stale and meta.get("left_out") is not None)
    if ds.kind != "database":
        # A format the code no longer makes is dropped from the reused record by the deploy.
        skip |= {
            r
            for r in meta.get("files", {})
            if r.startswith("data.") and r[5:] in WRITERS and r[5:] not in want
        }
    sums = meta.get("sha256")
    if sums is None:
        # An entry from before the record kept digests holds the small files alone.
        sums = digests(entry / "files")
    # A file grown from a published Parquet other than this entry's is made again from it.
    regrow = {
        r
        for r, h in meta.get("grown", {}).items()
        if h != sums.get("data.parquet") and r not in skip and r in sums
    }
    if "manifest.json" in regrow:
        masked = True
        regrow.discard("manifest.json")
    if masked:
        skip.add("manifest.json")
    out_lines = []
    for name in ("rows", "unknown_columns", "suppressed_cells", "partitions", "tables"):
        old, new = meta.get(name), _plain(getattr(vout, name))
        if name == "tables" and old is None:
            old = {}
        if old != new:
            out_lines.append(f"{name} {json.dumps(old)} published, {json.dumps(new)} built now")
    if not ({"data.ndjson"} & (skip | regrow)) and meta.get("first", "") != vout.first:
        out_lines.append("the first row of data.ndjson")
    if _plain(vout.left_out) != meta.get("left_out"):
        out_lines.append("formats_left_out")
    old_files, new_files = meta.get("files", {}), vout.files
    for rel in sorted(set(old_files) | set(new_files)):
        if rel in skip or rel in regrow:
            continue
        if rel not in new_files:
            out_lines.append(f"{rel} is published and no longer built")
        elif rel not in old_files:
            out_lines.append(f"{rel} is built now and was never published")
        elif old_files[rel] != new_files[rel] and Path(rel).name != "data.duckdb":
            out_lines.append(f"{rel} is {new_files[rel]} bytes, {old_files[rel]} published")
    now_sums = digests(
        vdir,
        [r for r in sums if r not in skip and r not in regrow and r in new_files],
        databases=ds.kind != "database",
    )
    if regrow:
        again = _regrown(ds, vout.manifest, cache, key, out, sorted(regrow))
        if again is None:
            out_lines.append("the published Parquet its grown files were made from is gone")
        else:
            now_sums |= again
    for rel, h in sorted(now_sums.items()):
        if sums[rel] != h and not any(line.startswith(f"{rel} ") for line in out_lines):
            out_lines.append(f"{rel} differs")
    if masked and (old_man := entry / "files" / "manifest.json").is_file():
        new_man = vdir / "manifest.json"
        if not new_man.is_file() or _unmeasured(old_man) != _unmeasured(new_man):
            out_lines.append("manifest.json differs beyond the sizes it measures")
    if vout.query and "query_sha256" in meta:
        q = out / vout.query
        if not q.is_file() or digest(q) != meta["query_sha256"]:
            out_lines.append("its query copy differs")
    return out_lines


def check(
    ds: Dataset, store_dir: Path, cache: BuildCache, out: Path, cap: int = CAP
) -> tuple[list[str], int, int]:
    """One dataset's newest versions that fit in cap built from their sources and compared with
    the entries a deploy would reuse, with the diffs between them, and the history archive when
    every version was built. Returns the differences, each naming its file, and how many
    versions were compared and how many a deploy would build again anyway."""
    from .build import build_dataset, version_key

    ms = checked_versions(ds, store_dir, cap)
    whole = len(ms) == len(store.manifests(store_dir, ds.slug))
    fresh = build_dataset(ds, store_dir, out, newest=len(ms))
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
        diffs = _version(ds, meta, v, out, cache.root / key, cache, key)
        problems += [f"{where}: {d}" for d in diffs]
    pairs = list(zip(fresh.versions, keys, strict=True))
    for (a, ka), (b, kb) in zip(pairs, pairs[1:], strict=False):
        rel = f"d/{ds.slug}/diff/{a.manifest.version}..{b.manifest.version}.json"
        meta = _entry(cache, cache.key(ka, kb, "diff"))
        if meta is not None and meta != json.loads((out / rel).read_text(encoding="utf-8")):
            problems.append(f"{rel}: differs")
    if keys and whole:
        hkey = cache.key(*keys, "history")
        rel = f"d/{ds.slug}/history.tar.zst"
        old = cache.root / hkey / "files" / "history.tar.zst"
        if old.is_file() and old.read_bytes() != (out / rel).read_bytes():
            problems.append(f"{rel}: differs")
    return problems, compared, rebuilt


def run(
    datasets: list[Dataset], store_dir: Path, cache_dir: Path, out: Path, cap: int = CAP
) -> int:
    from .serialise.profile import QUERY_DIR

    cache = BuildCache(cache_dir)
    problems, failed, compared, rebuilt = [], [], 0, 0
    for ds in datasets:
        try:
            p, c, r = check(ds, store_dir, cache, out, cap)
        # Each dataset the new code cannot build is named, and the rest are still checked.
        except Exception as e:
            failed.append(f"d/{ds.slug}/: the build failed: {type(e).__name__}: {e}")
            print(f"verify: {ds.slug}: the build failed")
            p, c, r = [], 0, 0
        else:
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
    for line in failed:
        print(f"::error::{line}")
    if not problems:
        return 1 if failed else 0
    for _ds, line in problems:
        print(f"::error::{line}")
    named = sorted({ds.slug: ds for ds, _ in problems}.items())
    print(
        f"verify: the code as it stands makes other files for {len(named)} of the "
        f"{len(datasets)} sampled dataset(s) than the versions a deploy would reuse unchanged."
    )
    if len(named) > 1:
        print(
            "verify: the change reaches more than one dataset, so it very likely reaches stored "
            "datasets this sample did not build. Raise REBUILD in pipeline/publicdata/cache.py. "
            "Raise `rebuild` in single entries only when you can name every dataset the change "
            "reaches, since the check passes once the sampled ones are raised and the rest would "
            "then be reused with their old files."
        )
    else:
        print(
            "verify: raise `rebuild` in its register entry once you have checked that no other "
            "stored dataset built the same way (shown below) is affected, or raise REBUILD in "
            "pipeline/publicdata/cache.py. An earlier change can be the cause, and the same "
            "number fixes it."
        )
    for slug, ds in named:
        where = Path(ds.path or f"{slug}.yaml")
        where = where.relative_to(REPO) if where.is_relative_to(REPO) else where.name
        how = ", ".join(str(x) for x in _stratum(ds))
        print(f"  {slug} ({where}, rebuild {ds.rebuild} now; built as {how})")
    return 1
