import json
import shutil

from publicdata import shards
from publicdata.__main__ import main


def test_small_changes_build_in_one_job():
    assert shards.plan({"a": 30, "b": 20, "c": 0}, 4, floor=100) == [["a", "b"]]
    assert shards.plan({"a": 0}, 4) == []


def test_large_changes_spread_and_a_dataset_larger_than_a_share_builds_alone():
    w = {"gnaf": 1850, "eucalypts": 580, "water": 360, "tas": 240, "qld": 210, "abs": 100}
    jobs = shards.plan(w, 4, floor=0)
    assert ["gnaf"] in jobs
    assert sorted(s for j in jobs for s in j) == sorted(w)
    assert len(jobs) <= 4


def test_more_jobs_than_allowed_fall_back_to_the_lightest_job():
    w = {"a": 7, "b": 7, "c": 7, "d": 3, "e": 3, "f": 3}
    # Two jobs of 15 cannot hold them (7+7, 7+3+3, 3), so each goes to the lighter job.
    jobs = shards.plan(w, 2, floor=0)
    assert len(jobs) == 2 and sorted(s for j in jobs for s in j) == sorted(w)
    assert sorted(sum(w[s] for s in j) for j in jobs) == [14, 16]


def _build(store, out, cache, *slugs, built=(), published=None):
    args = ["build", *slugs, *(["--no-site"] if slugs else []), "--store", str(store)]
    args += ["--out", str(out)]
    if published:
        args += ["--published", str(published)]
    args += ["--cache", str(cache), "--absent", str(out) + ".absent.json"]
    for b in built:
        args += ["--built", str(b)]
    assert main(args) == 0
    return json.loads((out.parent / (out.name + ".absent.json")).read_text())


def test_shards_fill_the_cache_the_deploy_builds_from(fixture_store, tmp_path, register_dir):
    from publicdata.register import load

    every = [d.slug for d in load(register_dir) if d.publishable]
    assert sorted(shards.weights(load(register_dir), fixture_store, tmp_path / "none")) == sorted(
        d.slug for d in load(register_dir)
    )
    # A database alone in a shard, as G-NAF is.
    one, two = ["gnaf"], [s for s in every if s != "gnaf"]
    for name, slugs in (("a", one), ("b", two)):
        _build(fixture_store, tmp_path / name, tmp_path / f"c{name}", *slugs)
        absent = str(tmp_path / f"{name}.absent.json")
        assert main(["gate", str(tmp_path / name), "--absent", absent, "--versions-only"]) == 0
    cache = tmp_path / "cache"
    shutil.copytree(tmp_path / "ca", cache)
    shutil.copytree(tmp_path / "cb", cache, dirs_exist_ok=True)
    w = shards.weights(load(register_dir), fixture_store, cache)
    assert not any(w.values())
    # Without the shard trees the deploy leaves their files out, reading back from R2 only the
    # SQLite its pages draw from; with them a preview has them all.
    r2 = tmp_path / "r2"
    shutil.copytree(tmp_path / "a", r2)
    shutil.copytree(tmp_path / "b", r2, dirs_exist_ok=True)
    assert _build(fixture_store, tmp_path / "o1", cache, published=r2)
    assert (
        _build(fixture_store, tmp_path / "o2", cache, built=(tmp_path / "a", tmp_path / "b")) == []
    )
    assert main(["gate", str(tmp_path / "o2")]) == 0


def test_a_version_whose_writer_changed_is_still_to_build(fixture_store, tmp_path, register_dir):
    from publicdata.register import load

    cache = tmp_path / "cache"
    _build(fixture_store, tmp_path / "o", cache)
    metas = [p for p in cache.glob("*/meta.json") if "writers" in json.loads(p.read_text())]
    meta = next(p for p in metas if json.loads(p.read_text())["writers"])
    m = json.loads(meta.read_text())
    m["writers"] = {f: "old" for f in m["writers"]}
    meta.chmod(0o644)
    meta.write_text(json.dumps(m))
    w = shards.weights(load(register_dir), fixture_store, cache)
    assert sum(1 for v in w.values() if v) == 1
