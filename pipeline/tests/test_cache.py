import filecmp
import re

import pytest

from publicdata import store
from publicdata.build import build_dataset
from publicdata.cache import PACKAGE, BuildCache, code_files
from publicdata.register import Field

from .conftest import make_dataset, make_manifest

F = [Field("id", "Id", "integer"), Field("v", "V")]


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _same(a, b):
    from publicdata.dbcheck import compare

    assert _tree(a) == _tree(b)
    # A DuckDB file's bytes are not reproducible, so those are compared by content.
    files = [f for f in _tree(a) if not f.endswith("data.duckdb")]
    _, mismatch, errors = filecmp.cmpfiles(a, b, files, shallow=False)
    assert not mismatch and not errors
    assert compare(a, b) == []


@pytest.fixture
def two_versions(tmp_path):
    s = tmp_path / "store"
    for version, csv in (("2026-01-01", b"Id,V\n1,a\n2,b\n"), ("2026-02-01", b"Id,V\n2,B\n3,c\n")):
        store.write(s, make_manifest(csv, version=version), csv)
    return s


def _without_sources(src, dst):
    import shutil

    shutil.copytree(src, dst)
    for p in dst.rglob("source.*"):
        p.unlink()
    return dst


def _reading(monkeypatch, out, published_tree):
    """A build into out reads a cached version's left-out files back from published_tree, as
    the deploy reads them from R2."""
    from publicdata import published

    monkeypatch.setattr(published, "current", published.Published(out, source=published_tree))


def _matches_except_absent(full, slim, vouts, slug="t"):
    """slim holds exactly full's files less each version's absent ones, byte for byte. A file
    read back after the cache left it out is in slim, as the build's absent list counts it."""
    absent = {f"d/{slug}/v/{v.manifest.version}/{rel}" for v in vouts for rel in v.absent}
    absent = {a for a in absent if not (slim / a).exists()}
    assert _tree(slim) == sorted(set(_tree(full)) - absent)
    _, mismatch, errors = filecmp.cmpfiles(full, slim, _tree(slim), shallow=False)
    assert not mismatch and not errors
    return absent


def test_a_warm_build_needs_no_source_bytes_and_matches_less_the_published_formats(
    two_versions, tmp_path
):
    ds = make_dataset(F, key=("id",))
    plain, cold, warm = (tmp_path / n for n in ("plain", "cold", "warm"))
    build_dataset(ds, two_versions, plain)
    cache = BuildCache(tmp_path / "cache")
    build_dataset(ds, two_versions, cold, cache)
    assert (cache.hits, cache.misses) == (0, 4)  # two versions, their diff, the history
    _same(plain, cold)
    cache = BuildCache(tmp_path / "cache")
    out = build_dataset(ds, _without_sources(two_versions, tmp_path / "bare"), warm, cache)
    assert (cache.hits, cache.misses) == (4, 0)
    assert out.changes[0]["changed"] == 1 and out.latest.rows == 2
    absent = _matches_except_absent(plain, warm, out.versions)
    names = {a.rsplit("/", 1)[1] for a in absent}
    assert {"data.json", "data.csv", "data.ndjson", "data.parquet", "data.sqlite"} <= names
    # The publisher's file is listed by every version and held by the raw store, never the tree.
    assert not names & {"manifest.json", "schema.json", "source.csv"}
    assert "source.csv" in out.latest.files
    assert out.latest.first == '{"id":2,"v":"B"}'


def test_a_new_version_diffs_against_the_cached_one_without_its_source(tmp_path, monkeypatch):
    ds = make_dataset(F, key=("id",))
    one, both = tmp_path / "one", tmp_path / "both"
    first = (b"Id,V\n1,a\n2,b\n", "2026-01-01")
    store.write(one, make_manifest(first[0], version=first[1]), first[0])
    build_dataset(ds, one, tmp_path / "a", BuildCache(tmp_path / "cache"))
    _without_sources(one, both)
    csv = b"Id,V\n2,B\n3,c\n"
    store.write(both, make_manifest(csv, version="2026-02-01"), csv)
    plain_store = tmp_path / "plain-store"
    store.write(plain_store, make_manifest(first[0], version=first[1]), first[0])
    store.write(plain_store, make_manifest(csv, version="2026-02-01"), csv)
    build_dataset(ds, plain_store, tmp_path / "plain")
    cache = BuildCache(tmp_path / "cache")
    _reading(monkeypatch, tmp_path / "warm", tmp_path / "a")
    out = build_dataset(ds, both, tmp_path / "warm", cache)
    assert cache.misses == 3  # the new version, its diff and the history
    # The diff and the history read the cached version's Parquet back, so that is in the tree.
    absent = _matches_except_absent(tmp_path / "plain", tmp_path / "warm", out.versions)
    assert (tmp_path / "warm" / "d/t/v/2026-01-01/data.parquet").exists()
    assert all("/2026-01-01/" in a for a in absent)


def test_a_register_change_rebuilds_and_prune_drops_the_old_entry(two_versions, tmp_path):
    root = tmp_path / "cache"
    build_dataset(make_dataset(F, key=("id",)), two_versions, tmp_path / "a", BuildCache(root))
    cache = BuildCache(root)
    build_dataset(
        make_dataset(F, key=("id",), title="Renamed"), two_versions, tmp_path / "b", cache
    )
    assert cache.hits == 0
    assert cache.prune() == 4 and len(list(root.iterdir())) == 4


def test_cached_files_are_read_only(two_versions, tmp_path):
    build_dataset(
        make_dataset(F, key=("id",)), two_versions, tmp_path / "a", BuildCache(tmp_path / "c")
    )
    with pytest.raises(PermissionError):
        (tmp_path / "a" / "d" / "t" / "v" / "2026-01-01" / "manifest.json").write_bytes(b"x")


def test_the_key_covers_every_module_the_version_build_imports():
    names = {str(p.relative_to(PACKAGE)) for p in code_files()}
    assert {"build.py", "normalise.py", "serialise/__init__.py", "provenance.py"} <= names
    assert "site.py" not in names


def test_absolute_package_imports_are_followed(tmp_path, monkeypatch):
    from publicdata import cache

    (tmp_path / "__init__.py").write_text("")
    (tmp_path / "build.py").write_text("import publicdata.a\nfrom publicdata.b import x\n")
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("from .c import y\n")
    (tmp_path / "c.py").write_text("")
    monkeypatch.setattr(cache, "PACKAGE", tmp_path / "publicdata")
    (tmp_path / "publicdata").symlink_to(tmp_path)
    names = {p.name for p in cache.code_files()}
    assert names == {"__init__.py", "build.py", "a.py", "b.py", "c.py"}


def test_a_warm_site_build_from_manifests_alone_matches_and_passes_the_gate(
    fixture_store, fixture_builds, register_dir, tmp_path
):
    import json
    import shutil

    from publicdata.__main__ import main
    from publicdata.gate import check

    s = tmp_path / "store"
    shutil.copytree(fixture_store, s)
    plain = fixture_builds[0]
    cold, warm = (tmp_path / n for n in ("cold", "warm"))
    cache = ["--cache", str(tmp_path / "cache")]
    assert main(["build", "--store", str(s), "--out", str(cold), *cache, "--absent", str(tmp_path / "a.json")]) == 0  # fmt: skip
    assert json.loads((tmp_path / "a.json").read_text()) == []
    for p in s.glob("*/*/source.*"):
        if p.parts[-3] != "catalogue":
            p.unlink()
    assert main(["build", "--store", str(s), "--out", str(warm), *cache, "--absent", str(tmp_path / "b.json"), "--published", str(cold)]) == 0  # fmt: skip
    absent = json.loads((tmp_path / "b.json").read_text())
    _same(plain, cold)
    assert absent and _tree(warm) == sorted(set(_tree(plain)) - set(absent))
    _, mismatch, errors = filecmp.cmpfiles(plain, warm, _tree(warm), shallow=False)
    assert not mismatch and not errors
    assert check(warm, register_dir, absent) == []
    assert any("missing data.json" in e for e in check(warm, register_dir))


def test_the_push_refuses_when_a_left_out_file_is_not_in_r2():
    from publicdata import r2

    r2.check_expected(["d/x/v/2026-01-01/data.json"], {"d/x/v/2026-01-01/data.json"})
    with pytest.raises(SystemExit, match="not in R2"):
        r2.check_expected(["d/x/v/2026-01-01/data.json"], set())
    with pytest.raises(SystemExit, match="without the cache"):
        r2.check_expected(
            ["d/x/v/2026-01-01/data.json"],
            {"d/x/v/2026-01-01/data.json"},
            ("d/x/v/2026-01-01/",),
        )


def test_a_code_change_prunes_the_old_entries_before_it_builds(two_versions, tmp_path):
    from publicdata.build import cache_keys

    ds = make_dataset(F, key=("id",))
    root = tmp_path / "cache"
    build_dataset(ds, two_versions, tmp_path / "a", BuildCache(root))
    cache = BuildCache(root)
    assert cache_keys(cache, ds, two_versions) == {p.name for p in root.iterdir()}
    cache.env = "other code"
    assert cache.prune(cache_keys(cache, ds, two_versions)) == 4
    assert not list(root.iterdir())


def _plain_and_cached(fixture_store, fixture_builds, tmp_path, formats=None):
    import json
    import shutil

    from publicdata.__main__ import main

    plain, cold, shared = fixture_builds
    cache = tmp_path / "cache"
    if formats:
        cold = tmp_path / "cold"
        assert main(["build", "--store", str(fixture_store), "--out", str(cold), "--cache", str(cache), "--absent", str(tmp_path / "cold.json"), "--formats", formats]) == 0  # fmt: skip
    else:
        # A test grows its cache, so it takes a copy of the shared one.
        shutil.copytree(shared, cache)
    return plain, cold, cache, json.loads


def test_a_cached_version_grows_into_new_formats_from_its_parquet(
    fixture_store, fixture_builds, tmp_path, monkeypatch, capsys
):
    import json

    from publicdata import build, serialise
    from publicdata.__main__ import main
    from publicdata.gate import check

    from .conftest import ROOT

    plain, _cold, cache, _ = _plain_and_cached(
        fixture_store, fixture_builds, tmp_path, "ndjson,csv,parquet,sqlite,arrow,geojson"
    )
    assert serialise.LIMIT is not None  # until the next build resets it
    # The source bytes are never read again: the missing files come from the cached Parquet.
    monkeypatch.setattr(
        build, "normalise", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rebuilt"))
    )
    grown = tmp_path / "grown"
    assert main(["build", "--store", str(fixture_store), "--out", str(grown), "--cache", str(cache), "--absent", str(tmp_path / "grown.json"), "--published", str(_cold)]) == 0  # fmt: skip
    out = re.split(r"built \d+ files", capsys.readouterr().out, maxsplit=1)[
        1
    ]  # the grown build's lines
    assert "40 reused, 0 built, 74 file(s) written into reused versions" in out
    absent = set(json.loads((tmp_path / "grown.json").read_text()))
    # The grown tree is the plain tree, less the files the cache had already published.
    for rel in _tree(plain):
        if rel in absent:
            assert not (grown / rel).exists(), rel
        elif not rel.endswith("data.duckdb"):
            assert (grown / rel).read_bytes() == (plain / rel).read_bytes(), rel
    # The files written now are in the tree; those the cache had published are absent.
    assert absent and not [
        r
        for r in absent
        if r.endswith(("data.duckdb", "data.xlsx")) and "/v/" in r and "gnaf" not in r
    ]
    vdir = grown / "d" / "qld-road-crash-locations" / "v" / "2026-04-24"
    for new in ("data.duckdb", "data.xlsx", "data.json", "data.gpkg", "data.csv.gz"):
        assert (vdir / new).exists(), new
    assert not (vdir / "data.csv").exists()  # written to make the gzip, already published
    assert check(grown, ROOT / "register", sorted(absent)) == []
    # The entry now knows every writer, so a third build reuses it whole.
    again = tmp_path / "again"
    assert main(["build", "--store", str(fixture_store), "--out", str(again), "--cache", str(cache), "--absent", str(tmp_path / "again.json"), "--published", str(_cold)]) == 0  # fmt: skip
    assert "0 built, 0 file(s) written" in capsys.readouterr().out


def test_a_changed_writer_rewrites_only_its_own_file(
    fixture_store, fixture_builds, tmp_path, monkeypatch, capsys
):
    from publicdata import build
    from publicdata import cache as cache_mod
    from publicdata.__main__ import main

    plain, cold, cache, _ = _plain_and_cached(fixture_store, fixture_builds, tmp_path)
    real = cache_mod.writer_key
    monkeypatch.setattr(
        cache_mod,
        "writer_key",
        lambda fmt: "changed" if fmt in ("csv", "csv.gz", "ndjson") else real(fmt),
    )
    monkeypatch.setattr(
        build, "normalise", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rebuilt"))
    )
    grown = tmp_path / "grown"
    assert main(["build", "--store", str(fixture_store), "--out", str(grown), "--cache", str(cache), "--absent", str(tmp_path / "grown.json"), "--published", str(cold)]) == 0  # fmt: skip
    out = capsys.readouterr().out
    # A changed CSV writer also changes the key of the gzip derived from it: two files each.
    n = sum(3 for o in plain.glob("d/*/v/*/data.csv"))
    assert f"0 built, {n} file(s) written into reused versions" in out
    vdir = grown / "d" / "qld-road-crash-factors" / "v" / "2026-04-24"
    pv = plain / "d/qld-road-crash-factors/v/2026-04-24"
    assert (vdir / "data.csv").read_bytes() == (pv / "data.csv").read_bytes()
    assert (vdir / "data.csv.gz").read_bytes() == (pv / "data.csv.gz").read_bytes()
    assert (vdir / "data.ndjson").read_bytes() == (pv / "data.ndjson").read_bytes()
    assert not (vdir / "data.xlsx").exists()
    # The page's sample row is read again from the rewritten NDJSON, so the pages agree.
    page = "d/qld-road-crash-factors/index.html"
    assert (grown / page).read_bytes() == (plain / page).read_bytes()


def test_a_changed_parquet_writer_rebuilds_the_version(
    fixture_store, fixture_builds, register_dir, tmp_path, monkeypatch
):
    from publicdata import build
    from publicdata import cache as cache_mod
    from publicdata.__main__ import main
    from publicdata.register import load

    _plain, _cold, cache, _ = _plain_and_cached(fixture_store, fixture_builds, tmp_path)
    real = cache_mod.writer_key
    monkeypatch.setattr(
        cache_mod, "writer_key", lambda fmt: "changed" if fmt == "parquet" else real(fmt)
    )
    # The summary counts a hit before the entry is found stale, so the rebuilds are counted here.
    rebuilt, real_build = [], build.build_version
    monkeypatch.setattr(
        build,
        "build_version",
        lambda ds, *a, **k: rebuilt.append(ds.slug) or real_build(ds, *a, **k),
    )
    grown = tmp_path / "grown"
    assert main(["build", "--store", str(fixture_store), "--out", str(grown), "--cache", str(cache), "--absent", str(tmp_path / "grown.json")]) == 0  # fmt: skip
    kinds = {d.slug: d.kind for d in load(register_dir)}
    tables = {p.parts[-3] for p in fixture_store.glob("*/*/manifest.json")} - {"catalogue"}
    assert set(rebuilt) == {s for s in tables if kinds[s] != "database"}


def test_the_rows_key_leaves_the_writers_out_but_keeps_the_partition_writers():
    from publicdata import cache as cache_mod

    files = {p.relative_to(cache_mod.PACKAGE).as_posix() for p in cache_mod.code_files()}
    assert "serialise/writers/json.py" in files and "serialise/writers/geojson.py" in files
    assert not [
        f
        for f in files
        if f.startswith("serialise/writers/")
        and f not in ("serialise/writers/json.py", "serialise/writers/geojson.py")
    ]
    keys = cache_mod.writer_keys()
    assert set(keys) == {
        "json",
        "ndjson",
        "csv",
        "parquet",
        "sqlite",
        "duckdb",
        "xlsx",
        "arrow",
        "csv.gz",
        "geojson",
        "gpkg",
        "geo.parquet",
        "pmtiles",
    }
    assert len(set(keys.values())) == len(keys)


def test_a_derived_formats_key_takes_in_the_writer_it_reads(tmp_path, monkeypatch):
    import shutil

    from publicdata import cache as cache_mod

    before = cache_mod.writer_key("csv.gz")
    shadow = tmp_path / "writers"
    shutil.copytree(cache_mod._writers_dir(), shadow)
    (shadow / "csv.py").write_text((shadow / "csv.py").read_text() + "\n# changed\n")
    monkeypatch.setattr(cache_mod, "_writers_dir", lambda: shadow)
    assert cache_mod.writer_key("csv.gz") != before


def test_formats_must_be_known_and_keep_what_the_build_reads_back(fixture_store, tmp_path):
    from publicdata.__main__ import main

    run = ["build", "--store", str(fixture_store), "--out", str(tmp_path / "o")]
    with pytest.raises(SystemExit, match="unknown"):
        main([*run, "--formats", "ndjson,parquet,docx"])
    with pytest.raises(SystemExit, match="ndjson and parquet"):
        main([*run, "--formats", "csv,parquet"])


def test_a_limited_build_never_shrinks_a_full_entry(
    fixture_store, fixture_builds, tmp_path, capsys
):
    import json

    from publicdata.__main__ import main

    _plain, cold, cache, _ = _plain_and_cached(fixture_store, fixture_builds, tmp_path)
    run = ["build", "--store", str(fixture_store), "--cache", str(cache), "--published", str(cold)]
    assert main([*run, "--out", str(tmp_path / "few"), "--absent", str(tmp_path / "few.json"), "--formats", "ndjson,parquet"]) == 0  # fmt: skip
    assert main([*run, "--out", str(tmp_path / "full"), "--absent", str(tmp_path / "full.json")]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert out.count("40 reused, 0 built, 0 file(s) written") == 2
    # The entry still lists the SQLite, which stays published and is not read back.
    assert "d/qld-road-crash-factors/v/2026-04-24/data.sqlite" in json.loads(
        (tmp_path / "full.json").read_text()
    )


def test_cache_prune_drops_only_what_no_stored_version_uses(
    fixture_store, fixture_builds, tmp_path
):
    import shutil

    from publicdata.__main__ import main

    cache = tmp_path / "cache"
    shutil.copytree(fixture_builds[2], cache)
    kept = sorted(p.name for p in cache.iterdir())
    stale = cache / ("0" * 64)  # an entry from older code or a superseded version
    stale.mkdir()
    (stale / "meta.json").write_text("{}")
    assert main(["cache-prune", "--store", str(fixture_store), "--cache", str(cache)]) == 0
    assert sorted(p.name for p in cache.iterdir()) == kept
