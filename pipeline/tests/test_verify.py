from dataclasses import replace

import pytest

from publicdata import build, store
from publicdata import cache as cache_mod
from publicdata.build import build_dataset
from publicdata.cache import BuildCache, entry_key
from publicdata.register import Field
from publicdata.verify import check, sample, unkeyed

from .conftest import make_dataset, make_manifest

F = [Field("id", "Id", "integer"), Field("v", "V")]


def _store(root, slug="t"):
    for version, csv in (("2026-01-01", b"Id,V\n1,a\n2,b\n"), ("2026-02-01", b"Id,V\n2,B\n3,c\n")):
        store.write(root, make_manifest(csv, dataset=slug, version=version), csv)
    return root


@pytest.fixture
def two_datasets(tmp_path):
    s = tmp_path / "store"
    _store(s, "t")
    _store(s, "u")
    return s, make_dataset(F, key=("id",)), make_dataset(F, key=("id",), slug="u")


def _build(datasets, s, out, root):
    cache = BuildCache(root)
    for ds in datasets:
        build_dataset(ds, s, out, cache)
    return cache


def _changed_normalise(monkeypatch):
    """An edit to the build code that changes what every version holds."""
    real = build.normalise

    def normalise(ds, m, data):
        t = real(ds, m, data)
        return replace(t, table=t.table.slice(1))

    monkeypatch.setattr(build, "normalise", normalise)


def test_the_key_reads_no_build_module_but_the_partition_writers(monkeypatch):
    from pathlib import Path

    read, real = [], Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda p: read.append(p.name) or real(p))
    cache_mod.environment_key()
    assert sorted(read) == ["geojson.py", "json.py"]


def test_an_entry_key_ignores_defaults_and_what_shapes_no_version():
    ds = make_dataset(F, key=("id",))
    assert entry_key(ds) == entry_key(replace(ds, order=100, sample={"rows": 3}, query=False))
    assert entry_key(ds) != entry_key(replace(ds, title="Renamed"))
    assert entry_key(ds) != entry_key(replace(ds, rebuild=1))
    assert '"rebuild"' not in entry_key(ds)


def test_a_code_edit_reuses_every_version(two_datasets, tmp_path, monkeypatch):
    s, t, u = two_datasets
    _build([t, u], s, tmp_path / "a", tmp_path / "cache")
    _changed_normalise(monkeypatch)
    cache = _build([t, u], s, tmp_path / "b", tmp_path / "cache")
    assert (cache.hits, cache.misses) == (8, 0)


def test_a_dataset_rebuild_number_rebuilds_only_its_versions_diffs_and_history(
    two_datasets, tmp_path
):
    s, t, u = two_datasets
    _build([t, u], s, tmp_path / "a", tmp_path / "cache")
    cache = BuildCache(tmp_path / "cache")
    build_dataset(replace(t, rebuild=1), s, tmp_path / "b", cache)
    assert (cache.hits, cache.misses) == (0, 4)  # two versions, their diff, the history
    build_dataset(u, s, tmp_path / "b", cache)
    assert (cache.hits, cache.misses) == (4, 4)


def test_the_global_rebuild_number_rebuilds_every_version(two_datasets, tmp_path, monkeypatch):
    s, t, u = two_datasets
    _build([t, u], s, tmp_path / "a", tmp_path / "cache")
    monkeypatch.setattr(cache_mod, "REBUILD", cache_mod.REBUILD + 1)
    cache = _build([t, u], s, tmp_path / "b", tmp_path / "cache")
    assert (cache.hits, cache.misses) == (0, 8)


def test_the_check_passes_when_the_code_makes_what_is_published(two_datasets, tmp_path):
    s, t, _u = two_datasets
    cache = _build([t], s, tmp_path / "a", tmp_path / "cache")
    problems, compared, rebuilt = check(t, s, BuildCache(cache.root), tmp_path / "v")
    assert (problems, compared, rebuilt) == ([], 2, 0)


def test_the_check_names_each_version_an_unbumped_change_alters(
    two_datasets, tmp_path, monkeypatch
):
    s, t, _u = two_datasets
    root = _build([t], s, tmp_path / "a", tmp_path / "cache").root
    _changed_normalise(monkeypatch)
    problems, compared, _ = check(t, s, BuildCache(root), tmp_path / "v")
    assert compared == 2
    assert "d/t/v/2026-01-01/: rows 2 published, 1 built now" in problems
    assert "d/t/v/2026-02-01/: the first row of data.ndjson" in problems
    assert [p for p in problems if p.startswith("d/t/v/2026-02-01/: data.csv is ")]
    assert "d/t/diff/2026-01-01..2026-02-01.json: differs" in problems
    assert "d/t/history.tar.zst: differs" in problems
    # Raising the dataset's number rebuilds it, so nothing stale is reused.
    problems, compared, rebuilt = check(replace(t, rebuild=1), s, BuildCache(root), tmp_path / "w")
    assert (problems, compared, rebuilt) == ([], 0, 2)


def test_the_check_finds_a_change_that_keeps_every_size(two_datasets, tmp_path, monkeypatch):
    s, t, _u = two_datasets
    root = _build([t], s, tmp_path / "a", tmp_path / "cache").root
    real = build.normalise
    monkeypatch.setattr(build, "normalise", lambda ds, m, d: real(ds, m, d.replace(b",a", b",z")))
    problems, _, _ = check(t, s, BuildCache(root), tmp_path / "v")
    assert "d/t/v/2026-01-01/: data.csv differs" in problems
    assert not [p for p in problems if "2026-02-01/" in p]


def test_the_check_leaves_a_changed_writers_file_to_the_deploy(two_datasets, tmp_path, monkeypatch):
    s, t, _u = two_datasets
    root = _build([t], s, tmp_path / "a", tmp_path / "cache").root
    real = cache_mod.writer_key
    stale = ("csv", "csv.gz")  # the gzip is made from the CSV, so its key takes the CSV writer in
    monkeypatch.setattr(cache_mod, "writer_key", lambda f: "new" if f in stale else real(f))
    real_csv = build.WRITERS["csv"]

    def csv(tbl, header, path, vdir):
        real_csv(tbl, header, path, vdir)
        path.write_bytes(path.read_bytes() + b"\n")

    monkeypatch.setitem(build.WRITERS, "csv", csv)
    problems, compared, _ = check(t, s, BuildCache(root), tmp_path / "v")
    assert (problems, compared) == ([], 2)


def test_the_command_passes_on_the_fixture_store_built_by_the_deploy(
    fixture_store, fixture_builds, tmp_path, capsys
):
    from publicdata.__main__ import main

    _plain, cold, cache = fixture_builds
    run = ["verify", "run", "--store", str(fixture_store), "--cache", str(cache)]
    assert main([*run, "--published", str(cold), "--out", str(tmp_path / "v")]) == 0
    out = capsys.readouterr().out
    assert ", 0 to be built again" in out.splitlines()[-1]


def test_the_command_fails_and_names_the_entry_to_bump(
    fixture_store, fixture_builds, tmp_path, monkeypatch, capsys
):
    from publicdata.__main__ import main

    _plain, cold, cache = fixture_builds
    _changed_normalise(monkeypatch)
    slug = "qld-road-crash-factors"
    run = ["verify", "run", slug, "--store", str(fixture_store), "--cache", str(cache)]
    assert main([*run, "--published", str(cold), "--out", str(tmp_path / "v")]) == 1
    out = capsys.readouterr().out
    assert f"::error::d/{slug}/v/" in out
    assert f"  {slug} (register/{slug}.yaml, rebuild 0 now; built as table, " in out


def test_a_dataset_the_new_code_cannot_build_fails_the_check(two_datasets, tmp_path, monkeypatch):
    from publicdata.verify import run

    s, t, u = two_datasets
    root = _build([t, u], s, tmp_path / "a", tmp_path / "cache").root
    real = build.normalise

    def normalise(ds, m, data):
        if ds.slug == "t":
            raise ValueError("broken")
        return real(ds, m, data)

    monkeypatch.setattr(build, "normalise", normalise)
    assert run([t, u], s, root, tmp_path / "v") == 1


def test_only_an_edit_outside_the_keys_needs_the_check():
    assert unkeyed(["pipeline/publicdata/normalise.py", "README.md"]) == [
        "pipeline/publicdata/normalise.py"
    ]
    assert unkeyed(["pipeline/publicdata/build.py"]) == ["pipeline/publicdata/build.py"]
    # A writer is keyed on its own, and the site, the fetch and the register are not build code.
    assert not unkeyed(
        [
            "pipeline/publicdata/serialise/writers/csv.py",
            "pipeline/publicdata/serialise/writers/json.py",
            "pipeline/publicdata/site.py",
            "pipeline/publicdata/fetch.py",
            "register/qld-road-crash-factors.yaml",
        ]
    )


def test_the_sample_covers_each_stratum_within_its_budget(fixture_store, register_dir):
    from publicdata.register import load
    from publicdata.verify import _stratum, source_bytes

    datasets = [d for d in load(register_dir) if source_bytes(d, fixture_store)]
    cost = {d.slug: source_bytes(d, fixture_store) for d in datasets}
    assert sample(datasets, fixture_store, "a", 10**12, 10**12) == sorted(cost)
    assert sample(datasets, fixture_store, "a", 0, 10**12) == []
    cheapest: dict = {}
    for d in datasets:
        cheapest[_stratum(d)] = min(cheapest.get(_stratum(d), 10**18), cost[d.slug])
    budget = sum(cheapest.values())
    some = sample(datasets, fixture_store, "a", budget, 10**12)
    assert some == sample(datasets, fixture_store, "a", budget, 10**12)
    assert sum(cost[s] for s in some) <= budget
    by = {d.slug: d for d in datasets}
    assert {_stratum(by[s]) for s in some} == set(cheapest)
    # A dataset over the cap is never built, whatever the budget.
    big = max(cost, key=cost.get)
    assert big not in sample(datasets, fixture_store, "a", 10**12, cost[big] - 1)


def test_the_check_compares_what_the_duckdb_file_holds(two_datasets, tmp_path, monkeypatch):
    from publicdata import serialise

    s, t, _u = two_datasets
    root = _build([t], s, tmp_path / "a", tmp_path / "cache").root
    monkeypatch.setitem(serialise.DUCKDB_TYPES, "string", "BLOB")
    problems, compared, _ = check(t, s, BuildCache(root), tmp_path / "v")
    assert compared == 2
    assert "d/t/v/2026-01-01/: data.duckdb differs" in problems


def test_the_check_compares_the_query_copy(two_datasets, tmp_path, monkeypatch):
    s, t, _u = two_datasets
    root = _build([t], s, tmp_path / "a", tmp_path / "cache").root
    real = build._query_copy

    def query_copy(tbl, header, vdir, out):
        rel = real(tbl, header, vdir, out)
        data = (out / rel).read_bytes()
        (out / rel).unlink()  # it can be a link to the version's own Parquet
        (out / rel).write_bytes(data + b"x")
        return rel

    monkeypatch.setattr(build, "_query_copy", query_copy)
    problems, _, _ = check(t, s, BuildCache(root), tmp_path / "v")
    assert problems == [
        "d/t/v/2026-01-01/: its query copy differs",
        "d/t/v/2026-02-01/: its query copy differs",
    ]


def test_a_format_the_code_no_longer_makes_is_left_to_the_deploy(
    two_datasets, tmp_path, monkeypatch
):
    s, t, _u = two_datasets
    root = _build([t], s, tmp_path / "a", tmp_path / "cache").root
    real = build.formats_for
    monkeypatch.setattr(build, "formats_for", lambda *a: [f for f in real(*a) if f != "xlsx"])
    assert check(t, s, BuildCache(root), tmp_path / "v") == ([], 2, 0)


def test_a_shape_layers_parquet_is_keyed_on_the_geoparquet_writer(monkeypatch):
    from pathlib import Path

    from publicdata.cache import writer_files, writer_key
    from publicdata.serialise import WRITERS

    assert "geo_parquet.py" in {p.name for p in writer_files("parquet")}
    assert {p.name for p in writer_files("csv.gz")} == {"csv.py", "csv_gz.py"}
    before = writer_key("parquet")
    real = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda p: real(p) + (b"#" if p.name == "geo_parquet.py" else b"")
    )
    assert writer_key("parquet") != before
    # Every writer module is in some format's key, so none is left to the sample.
    writers = sorted({p for f in WRITERS for p in writer_files(f)})
    assert not unkeyed([p.relative_to(Path(__file__).parents[2]).as_posix() for p in writers])


def test_the_environment_key_takes_in_the_spatial_extension(monkeypatch):
    before = cache_mod.environment_key()
    monkeypatch.setattr(cache_mod, "spatial_version", lambda: "another build")
    assert cache_mod.environment_key() != before
    assert {"xlrd", "pmtiles"} <= set(cache_mod.LIBRARIES)


def test_a_joined_dataset_is_keyed_on_its_layers_register_entry(register_dir, tmp_path):
    import shutil

    from publicdata.spine import spine_versions

    reg = tmp_path / "register"
    reg.mkdir()
    shutil.copy(register_dir / "abs-lga-2025.yaml", reg / "abs-lga-2025.yaml")
    before = spine_versions(("lga",), tmp_path / "store", reg)
    entry = reg / "abs-lga-2025.yaml"
    entry.write_text(entry.read_text(encoding="utf-8") + "rebuild: 1\n", encoding="utf-8")
    assert spine_versions(("lga",), tmp_path / "store", reg) != before


def test_a_changed_register_default_needs_the_global_number(tmp_path):
    from publicdata.cache import PACKAGE
    from publicdata.verify import changed_defaults

    reg = (PACKAGE / "register.py").read_text(encoding="utf-8")
    cache_src = (PACKAGE / "cache.py").read_text(encoding="utf-8")
    before = tmp_path / "before"
    before.mkdir()
    (before / "cache.py").write_text(cache_src, encoding="utf-8")
    (before / "register.py").write_text(reg, encoding="utf-8")
    assert changed_defaults(before) == []
    # A new field with a default is no change to an existing one.
    (before / "register.py").write_text(reg.replace("    rebuild: int = 0\n", ""), encoding="utf-8")
    assert changed_defaults(before) == []
    (before / "register.py").write_text(
        reg.replace("    header_row: int = 1\n", "    header_row: int = 2\n"), encoding="utf-8"
    )
    assert changed_defaults(before) == ["Source.header_row"]
    # A field kept out of every key shapes no version, so its default is free to change.
    (before / "register.py").write_text(
        reg.replace("query: bool = field(default=True,", "query: bool = field(default=False,"),
        encoding="utf-8",
    )
    assert changed_defaults(before) == []
    (before / "register.py").write_text(
        reg.replace("    header_row: int = 1\n", "    header_row: int = 2\n"), encoding="utf-8"
    )
    (before / "cache.py").write_text(
        cache_src.replace("\nREBUILD = ", "\nREBUILD = 100 + "), encoding="utf-8"
    )
    assert changed_defaults(before) == []


def _old_r2(t, s, tmp_path, monkeypatch):
    """The published tree as an older build left it: rows another normalise made, which a
    version built again without a replace leaves in R2."""
    with monkeypatch.context() as mp:
        _changed_normalise(mp)
        build_dataset(t, s, tmp_path / "r2")
    return tmp_path / "r2"


def _publishing(out, r2):
    from publicdata import published

    published.current = published.Published(out, [], r2)


def test_old_bytes_in_r2_are_no_difference(two_datasets, tmp_path, monkeypatch):
    from publicdata import published

    s, t, _u = two_datasets
    r2 = _old_r2(t, s, tmp_path, monkeypatch)
    t = replace(t, rebuild=1)
    cache = BuildCache(tmp_path / "cache")
    try:
        _publishing(tmp_path / "a", r2)
        build_dataset(t, s, tmp_path / "a", cache)
        csv = b"Id,V\n3,c\n4,d\n"
        store.write(s, make_manifest(csv, dataset="t", version="2026-03-01"), csv)
        _publishing(tmp_path / "b", r2)
        cache = BuildCache(cache.root)
        build_dataset(t, s, tmp_path / "b", cache)
        assert (cache.hits, cache.misses) == (3, 3)  # the new version, its diff, the history
        _publishing(tmp_path / "v", r2)
        assert check(t, s, BuildCache(cache.root), tmp_path / "v") == ([], 3, 0)
    finally:
        published.current = None


def test_a_format_grown_from_old_bytes_in_r2_is_made_again_from_them(
    two_datasets, tmp_path, monkeypatch
):
    from publicdata import published, serialise

    s, t, _u = two_datasets
    r2 = _old_r2(t, s, tmp_path, monkeypatch)
    t = replace(t, rebuild=1)
    cache = BuildCache(tmp_path / "cache")
    try:
        _publishing(tmp_path / "a", r2)
        monkeypatch.setattr(serialise, "LIMIT", {"ndjson", "csv", "parquet"})
        build_dataset(t, s, tmp_path / "a", cache)
        monkeypatch.setattr(serialise, "LIMIT", None)
        _publishing(tmp_path / "b", r2)
        cache = BuildCache(cache.root)
        build_dataset(t, s, tmp_path / "b", cache)
        assert cache.grown
        _publishing(tmp_path / "v", r2)
        assert check(t, s, BuildCache(cache.root), tmp_path / "v") == ([], 2, 0)
        # A change to what the grown files hold, outside the writer's key, is still found.
        from publicdata import verify

        keys = cache_mod.writer_keys()
        monkeypatch.setattr(cache_mod, "writer_keys", lambda: keys)
        monkeypatch.setattr(verify, "writer_keys", lambda: keys)
        real = build.WRITERS["xlsx"]

        def xlsx(tbl, header, path, vdir):
            real(tbl, {**header, "note": "changed"}, path, vdir)

        monkeypatch.setitem(build.WRITERS, "xlsx", xlsx)
        _publishing(tmp_path / "w", r2)
        problems, _, _ = check(t, s, BuildCache(cache.root), tmp_path / "w")
        assert "d/t/v/2026-01-01/: data.xlsx differs" in problems
    finally:
        published.current = None


def test_the_published_copy_is_read_beside_what_the_build_made(tmp_path):
    from publicdata.published import Published

    out = tmp_path / "out"
    (out / "d").mkdir(parents=True)
    (out / "d" / "x").write_bytes(b"new")
    calls = []

    def download(rel, dest):
        calls.append(rel)
        if rel == "d/missing":
            return False
        dest.write_bytes(b"old")
        return True

    p = Published(out, [], None, download)
    old = p.copy("d/x")
    assert old.read_bytes() == b"old" and (out / "d" / "x").read_bytes() == b"new"
    assert p.copy("d/x") == old and p.copy("d/missing") is None and p.copy("d/missing") is None
    assert calls == ["d/x", "d/missing"]
    assert p.copy("d/y").read_bytes() == b"old" and (out / "d" / "y").read_bytes() == b"old"


def test_a_change_across_datasets_asks_for_the_global_number(
    two_datasets, tmp_path, monkeypatch, capsys
):
    from publicdata.verify import run

    s, t, u = two_datasets
    root = _build([t, u], s, tmp_path / "a", tmp_path / "cache").root
    _changed_normalise(monkeypatch)
    assert run([t, u], s, root, tmp_path / "v") == 1
    out = capsys.readouterr().out
    assert "for 2 of the 2 sampled dataset(s)" in out
    assert "Raise REBUILD in pipeline/publicdata/cache.py." in out
