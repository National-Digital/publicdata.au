import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from publicdata import r2


class FakeS3:
    def __init__(self, existing, meta=None, etags=None):
        self.existing = set(existing)
        self.meta = meta or {}
        self.etags = etags or {}
        self.puts = []
        self.heads = []
        self.listed = []

    def get_paginator(self, name):
        fake = self

        class P:
            def paginate(self, Bucket, Prefix):
                fake.listed.append(Prefix)
                yield {
                    "Contents": [
                        {"Key": k, "ETag": f'"{fake.etags.get(k, "")}"'}
                        for k in sorted(fake.existing)
                        if k.startswith(Prefix)
                    ]
                }

        return P()

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        return {"Metadata": self.meta.get(Key, {})}

    def upload_file(self, path, bucket, key, ExtraArgs):
        self.puts.append((key, ExtraArgs["ContentType"]))


def test_push_skips_existing_keys_unless_replace(tmp_path, monkeypatch):
    (tmp_path / "d" / "x" / "v" / "2026-04-24").mkdir(parents=True)
    (tmp_path / "d" / "x" / "v" / "2026-04-24" / "data.csv").write_text("a\n1\n")
    (tmp_path / "d" / "x" / "v" / "2026-04-24" / "data.parquet").write_bytes(b"PAR1")
    fake = FakeS3({"d/x/v/2026-04-24/data.csv"})
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path, "b") == 1
    assert [k for k, _ in fake.puts] == ["d/x/v/2026-04-24/data.parquet"]
    assert fake.heads == []
    fake.puts.clear()
    assert r2.push(tmp_path, "b", replace=("d/y/",)) == 1  # a prefix that matches nothing
    fake.puts.clear()
    assert r2.push(tmp_path, "b", replace=("d/x/v/2026-04-24/",)) == 2
    assert sorted(k for k, _ in fake.puts) == [
        "d/x/v/2026-04-24/data.csv",
        "d/x/v/2026-04-24/data.parquet",
    ]


def test_a_published_version_gains_partition_files_only_under_replace(tmp_path, monkeypatch):
    import pytest

    v = tmp_path / "d" / "x" / "v" / "2026-04-24"
    (v / "by" / "lga").mkdir(parents=True)
    (v / "by" / "lga" / "index.json").write_text("{}")
    (v / "manifest.json").write_text("{}")
    fake = FakeS3({"d/x/v/2026-04-24/manifest.json"})
    monkeypatch.setattr(r2, "client", lambda: fake)
    with pytest.raises(SystemExit, match="replace set to d/x/v/2026-04-24/"):
        r2.push(tmp_path, "b")
    assert fake.puts == []
    assert r2.push(tmp_path, "b", replace=("d/x/v/2026-04-24/",)) == 2
    # A version R2 holds no manifest of is new, or was cut short, and its push goes on.
    fake = FakeS3({"d/x/v/2026-04-24/by/lga/a.json"})
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path, "b") == 2
    assert [k for k, _ in fake.puts] == [
        "d/x/v/2026-04-24/by/lga/index.json",
        "d/x/v/2026-04-24/manifest.json",
    ]


def test_a_partition_by_edit_cannot_reach_r2_without_a_replace(tmp_path, monkeypatch):
    from dataclasses import replace

    import pytest

    from publicdata import store
    from publicdata.build import build_dataset
    from publicdata.cache import BuildCache
    from publicdata.register import Field

    from .conftest import make_dataset, make_manifest

    csv = b"Id,V\n1,a\n2,b\n"
    store.write(tmp_path / "store", make_manifest(csv, dataset="t", version="2026-01-01"), csv)
    ds = make_dataset([Field("id", "Id", "integer"), Field("v", "V")], key=("id",))
    cache = BuildCache(tmp_path / "cache")
    build_dataset(ds, tmp_path / "store", tmp_path / "a", cache)
    held = {p.relative_to(tmp_path / "a").as_posix() for p in (tmp_path / "a").rglob("*")}
    # The edit changes the version's key, so the cache rebuilds it with the new partitions.
    build_dataset(replace(ds, partition_by=("v",)), tmp_path / "store", tmp_path / "b", cache)
    assert cache.hits == 0
    fake = FakeS3({k for k in held if r2.dated_file(k)})
    monkeypatch.setattr(r2, "client", lambda: fake)
    with pytest.raises(SystemExit, match="replace set to d/t/v/2026-01-01/$"):
        r2.push(tmp_path / "b", "b", immutable=r2.dated_file)
    assert fake.puts == []
    r2.push(tmp_path / "b", "b", replace=("d/t/v/2026-01-01/",), immutable=r2.dated_file)
    assert "d/t/v/2026-01-01/by/v/a.json" in {k for k, _ in fake.puts}


def test_immutable_existing_keys_are_not_hashed(tmp_path, monkeypatch):
    v = tmp_path / "d" / "x" / "v" / "2026-04-24"
    v.mkdir(parents=True)
    (v / "data.csv").write_text("a\n1\n")
    monkeypatch.setattr(r2, "client", lambda: FakeS3({"d/x/v/2026-04-24/data.csv"}))
    hashed = []
    monkeypatch.setattr(r2, "_sha256", lambda p: hashed.append(p) or "h")
    assert r2.push(tmp_path, "b", immutable=r2.versioned) == 0
    assert hashed == []


def test_mutable_key_is_replaced_only_when_its_hash_changes(tmp_path, monkeypatch):
    (tmp_path / "d" / "x" / "v" / "2026-04-24").mkdir(parents=True)
    (tmp_path / "d" / "x" / "v" / "2026-04-24" / "data.csv").write_text("a\n1\n")
    (tmp_path / "d" / "x" / "history.tar.zst").write_bytes(b"new")
    keys = {"d/x/v/2026-04-24/data.csv", "d/x/history.tar.zst"}
    stale = FakeS3(keys, {"d/x/history.tar.zst": {"sha256": "old"}})
    monkeypatch.setattr(r2, "client", lambda: stale)
    assert r2.push(tmp_path, "b", immutable=r2.versioned) == 1
    assert [k for k, _ in stale.puts] == ["d/x/history.tar.zst"]
    same = FakeS3(keys, {"d/x/history.tar.zst": {"sha256": hashlib.sha256(b"new").hexdigest()}})
    monkeypatch.setattr(r2, "client", lambda: same)
    assert r2.push(tmp_path, "b", immutable=r2.versioned) == 0


def test_split_moves_every_version_file_and_its_page(tmp_path, capsys):
    from publicdata.__main__ import main

    out, large = tmp_path / "dist", tmp_path / "large"
    v = out / "d" / "x" / "v" / "2026-04-24"
    (v / "by" / "year").mkdir(parents=True)
    for name in ("index.html", "index.md", "data.csv", "manifest.json"):
        (v / name).write_text("x")
    (v / "by" / "year" / "2020.json").write_text("{}")
    (out / "d" / "x" / "index.html").write_text("x")
    (out / "d" / "x" / "versions.json").write_text("[]")
    assert main(["split", "--out", str(out), "--large", str(large)]) == 0
    assert not large.exists()
    assert main(["split", "--out", str(out), "--large", str(large), "--versioned"]) == 0
    moved = sorted(p.relative_to(large).as_posix() for p in large.rglob("*") if p.is_file())
    assert moved == [
        "d/x/v/2026-04-24/by/year/2020.json",
        "d/x/v/2026-04-24/data.csv",
        "d/x/v/2026-04-24/index.html",
        "d/x/v/2026-04-24/index.md",
        "d/x/v/2026-04-24/manifest.json",
    ]
    assert (out / "d" / "x" / "index.html").exists()
    # A large file where no route runs the R2 function would be unreachable, so split refuses it.
    (out / "d" / "x" / "history.tar.zst").write_bytes(b"x" * 2048)
    (out / "d" / "x" / "changes.json").write_bytes(b"x" * 2048)
    args = ["split", "--out", str(out), "--large", str(large), "--limit-mib", "0.001"]
    assert main(args) == 1
    assert "d/x/changes.json is over the Pages limit" in capsys.readouterr().err
    assert (large / "d" / "x" / "history.tar.zst").exists()


def test_dist_push_rejects_a_prefix_that_is_not_a_version(tmp_path, monkeypatch, capsys):
    from publicdata.__main__ import main

    monkeypatch.setattr(r2, "client", lambda: FakeS3(set()))
    assert main(["dist-push", "--large", str(tmp_path), "--replace", "d/"]) == 2
    assert "prefixes only" in capsys.readouterr().out
    assert main(["dist-push", "--large", str(tmp_path), "--replace", "d/x/v/2026-04-24/"]) == 0


def test_pull_skips_the_versions_the_build_cache_holds(fixture_store, tmp_path, monkeypatch):
    import shutil

    from publicdata.__main__ import _cached_versions, main

    s = tmp_path / "store"
    shutil.copytree(fixture_store, s)
    cache = tmp_path / "cache"
    assert main(["build", "--store", str(s), "--out", str(tmp_path / "o"), "--cache", str(cache), "--absent", str(tmp_path / "a.json")]) == 0  # fmt: skip
    held = _cached_versions(s, cache)
    datasets = {p.parts[-3] for p in s.glob("*/*/manifest.json")} - {"catalogue"}
    assert {slug for slug, _ in held} == datasets
    for p in s.glob("*/*/source.*"):
        p.unlink()
    got = []

    class Fake(FakeS3):
        def download_file(self, bucket, key, dest):
            got.append(key)
            shutil.copyfile(fixture_store / key, dest)

    monkeypatch.setattr(r2, "client", lambda: Fake(set()))
    r2.pull_store(s, skip=held)
    assert got == [k for k in got if k.startswith("catalogue/")] and got


def test_a_version_page_is_uploaded_again_only_when_it_changes(tmp_path, monkeypatch):
    v = tmp_path / "d" / "x" / "v" / "2026-04-24"
    v.mkdir(parents=True)
    (v / "index.html").write_text("newest")
    (v / "data.csv").write_text("a\n1\n")
    keys = {"d/x/v/2026-04-24/index.html", "d/x/v/2026-04-24/data.csv"}
    md5 = hashlib.md5(b"newest").hexdigest()
    same = FakeS3(keys, etags={"d/x/v/2026-04-24/index.html": md5})
    monkeypatch.setattr(r2, "client", lambda: same)
    assert r2.push(tmp_path, "b", immutable=r2.dated_file) == 0
    assert same.heads == []
    old = FakeS3(keys, etags={"d/x/v/2026-04-24/index.html": hashlib.md5(b"old").hexdigest()})
    monkeypatch.setattr(r2, "client", lambda: old)
    assert r2.push(tmp_path, "b", immutable=r2.dated_file) == 1
    assert [k for k, _ in old.puts] == ["d/x/v/2026-04-24/index.html"] and old.heads == []
    assert r2.dated_file("d/x/v/2026-04-24/data.csv") and not r2.dated_file("d/x/history.tar.zst")


def test_a_dated_only_push_leaves_the_pages(tmp_path, monkeypatch):
    v = tmp_path / "d" / "x" / "v" / "2026-04-24"
    v.mkdir(parents=True)
    (v / "data.csv").write_text("a\n1\n")
    (v / "index.html").write_text("<p>newest</p>")
    (tmp_path / "d" / "x" / "history.tar.zst").write_bytes(b"z")
    fake = FakeS3(set())
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path, "b", include=r2.dated_file) == 1
    assert [k for k, _ in fake.puts] == ["d/x/v/2026-04-24/data.csv"]


def test_store_push_replaces_raw_bytes_only_for_versions_main_never_took(tmp_path, monkeypatch):
    import subprocess

    from publicdata.__main__ import main

    for v in ("2026-10-03", "2026-10-04"):
        (tmp_path / "x" / v).mkdir(parents=True)
        (tmp_path / "x" / v / "manifest.json").write_text("{}")
        (tmp_path / "x" / v / "source.csv").write_text(f"a\n{v}\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "x/2026-10-03/manifest.json"], check=True)
    # Both keys hold other bytes: one from the committed version, one from a run that failed.
    fake = FakeS3(
        {
            f"x/{v}/{f}"
            for v in ("2026-10-03", "2026-10-04")
            for f in ("manifest.json", "source.csv")
        }
    )
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert main(["store", "push", "--store", str(tmp_path)]) == 0
    assert sorted(k for k, _ in fake.puts) == [
        "x/2026-10-04/manifest.json",
        "x/2026-10-04/source.csv",
    ]


class Bucket(FakeS3):
    """FakeS3 holding bytes, so the cache can go up and come back down."""

    def __init__(self):
        super().__init__(set())
        self.bytes = {}
        self.deleted = []
        self.batches = []
        self.buckets = set()

    def get_paginator(self, name):
        pages = super().get_paginator(name)
        fake = self

        class P:
            def paginate(self, Bucket, Prefix):
                fake.buckets.add(Bucket)
                return pages.paginate(Bucket=Bucket, Prefix=Prefix)

        return P()

    def head_object(self, Bucket, Key):
        self.buckets.add(Bucket)
        return super().head_object(Bucket, Key)

    def upload_file(self, path, bucket, key, ExtraArgs):
        self.buckets.add(bucket)
        super().upload_file(path, bucket, key, ExtraArgs)
        data = open(path, "rb").read()
        self.bytes[key] = data
        self.existing.add(key)
        self.etags[key] = hashlib.md5(data).hexdigest()

    def download_file(self, bucket, key, dest):
        self.buckets.add(bucket)
        if key not in self.bytes:
            raise _missing()
        open(dest, "wb").write(self.bytes[key])

    def get_object(self, Bucket, Key):
        import io

        self.buckets.add(Bucket)
        if Key not in self.bytes:
            raise _missing()
        return {"Body": io.BytesIO(self.bytes[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.buckets.add(Bucket)
        self.bytes[Key] = Body
        self.existing.add(Key)

    def delete_objects(self, Bucket, Delete):
        self.buckets.add(Bucket)
        self.batches.append([o["Key"] for o in Delete["Objects"]])
        for o in Delete["Objects"]:
            self.deleted.append(o["Key"])
            self.existing.discard(o["Key"])
            self.bytes.pop(o["Key"])
        return {}


def _missing():
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "404"}}, "GetObject")


def _entry(root, key, meta="{}", files=()):
    (root / key / "files").mkdir(parents=True)
    for name in files:
        (root / key / "files" / name).write_text(name)
    (root / key / "meta.json").write_text(meta)


def test_the_cache_goes_up_once_comes_back_whole_and_prunes_what_the_disk_dropped(
    tmp_path, monkeypatch
):
    bucket = Bucket()
    monkeypatch.setattr(r2, "client", lambda: bucket)
    up = tmp_path / "up"
    _entry(up, "a" * 64, files=("manifest.json",))
    _entry(up, "b" * 64)
    (up / ".c.tmp").mkdir()  # an entry a failed put left half written
    assert r2.cache_push(up) == (3, 0)
    # meta.json goes last, so an interrupted push never leaves a record without its files.
    puts = [k for k, _ in bucket.puts]
    assert sorted(puts[-2:]) == [f"{'a' * 64}/meta.json", f"{'b' * 64}/meta.json"]
    assert r2.cache_push(up) == (0, 0)
    (up / ("a" * 64) / "meta.json").write_text('{"grown": 1}')
    assert r2.cache_push(up) == (1, 0)
    assert r2.cache_pull(tmp_path / "meta", meta_only=True) == 2
    assert (
        sorted(p.name for p in (tmp_path / "meta").rglob("*") if p.is_file()) == ["meta.json"] * 2
    )
    down = tmp_path / "down"
    assert r2.cache_pull(down) == 2
    assert (down / ("a" * 64) / "files" / "manifest.json").read_text() == "manifest.json"
    assert (down / ("a" * 64) / "meta.json").read_text() == '{"grown": 1}'
    import shutil

    shutil.rmtree(up / ("b" * 64))
    assert r2.cache_push(up) == (0, 0)  # without prune nothing goes
    t0 = datetime(2026, 10, 6, tzinfo=UTC)
    # A preview may have listed the entry, so the first push to find it unused only notes it.
    assert r2.cache_push(up, prune=True, now=t0) == (0, 0)
    assert bucket.deleted == []
    assert r2.cache_pull(tmp_path / "again") == 2
    assert r2.cache_push(up, prune=True, now=t0 + timedelta(hours=23)) == (0, 0)
    assert r2.cache_push(up, prune=True, now=t0 + timedelta(hours=24)) == (0, 1)
    assert bucket.deleted == [f"{'b' * 64}/meta.json"]
    assert json.loads(bucket.bytes[r2.UNUSED]) == {}


def test_the_cache_lives_in_its_own_bucket_unless_another_is_named(tmp_path, monkeypatch):
    up = tmp_path / "up"
    _entry(up, "a" * 64, files=("x",))
    _entry(up, "b" * 64)
    t0 = datetime(2026, 10, 6, tzinfo=UTC)

    def run(**name):
        bucket = Bucket()
        monkeypatch.setattr(r2, "client", lambda: bucket)
        r2.cache_push(up, **name)
        r2.cache_pull(tmp_path / "down", meta_only=True, **name)
        shutil.rmtree(up / ("b" * 64), ignore_errors=True)
        r2.cache_push(up, prune=True, now=t0, **name)
        r2.cache_push(up, prune=True, now=t0 + timedelta(days=2), **name)
        assert bucket.deleted, "the prune reached no delete"
        _entry(up, "b" * 64)
        return bucket.buckets

    assert run() == {"publicdata-build-cache"}
    assert run(bucket="elsewhere") == {"elsewhere"}


def test_the_cache_reads_and_prunes_only_entry_keys(tmp_path, monkeypatch):
    bucket = Bucket()
    monkeypatch.setattr(r2, "client", lambda: bucket)
    stray = [
        f"_build/{'c' * 64}/meta.json",
        "d/x/v/2026-04-24/data.csv",
        f"{'c' * 64}/../../escape/meta.json",
    ]
    for k in stray:
        bucket.put_object(Bucket="b", Key=k, Body=b"{}", ContentType="application/json")
    up = tmp_path / "up"
    _entry(up, "card-" + "a" * 64)
    r2.cache_push(up)
    down = tmp_path / "down"
    assert r2.cache_pull(down) == 1
    assert sorted(p.name for p in down.iterdir()) == ["card-" + "a" * 64]
    assert not (tmp_path / "escape").exists()
    shutil.rmtree(up / ("card-" + "a" * 64))
    t0 = datetime(2026, 10, 6, tzinfo=UTC)
    r2.cache_push(up, prune=True, now=t0)
    assert r2.cache_push(up, prune=True, now=t0 + timedelta(days=2)) == (0, 1)
    assert all(k in bucket.existing for k in stray)


def test_an_entry_used_again_is_no_longer_due_and_pruning_takes_its_record_first(
    tmp_path, monkeypatch
):
    import shutil

    bucket = Bucket()
    monkeypatch.setattr(r2, "client", lambda: bucket)
    up = tmp_path / "up"
    a, b = "a" * 64, "b" * 64
    _entry(up, a, files=("x", "y"))
    _entry(up, b)
    r2.cache_push(up)
    t0 = datetime(2026, 10, 6, tzinfo=UTC)
    shutil.move(up / a, tmp_path / "aside")
    r2.cache_push(up, prune=True, now=t0)
    assert json.loads(bucket.bytes[r2.UNUSED]) == {a: t0.isoformat()}
    shutil.move(tmp_path / "aside", up / a)
    r2.cache_push(up, prune=True, now=t0 + timedelta(hours=1))
    assert json.loads(bucket.bytes[r2.UNUSED]) == {}
    shutil.rmtree(up / a)
    r2.cache_push(up, prune=True, now=t0 + timedelta(hours=2))
    assert r2.cache_push(up, prune=True, now=t0 + timedelta(hours=27)) == (0, 1)
    assert bucket.batches == [
        [f"{a}/meta.json"],
        [f"{a}/files/x", f"{a}/files/y"],
    ]


def test_a_delete_r2_refuses_fails_the_push(tmp_path, monkeypatch):
    import pytest

    class Refusing(Bucket):
        def delete_objects(self, Bucket, Delete):
            return {"Errors": [{"Key": Delete["Objects"][0]["Key"], "Code": "AccessDenied"}]}

    bucket = Refusing()
    monkeypatch.setattr(r2, "client", lambda: bucket)
    up = tmp_path / "up"
    _entry(up, "a" * 64)
    r2.cache_push(up)
    (up / ("a" * 64) / "meta.json").unlink()
    t0 = datetime(2026, 10, 6, tzinfo=UTC)
    r2.cache_push(up, prune=True, now=t0)
    with pytest.raises(SystemExit, match="AccessDenied"):
        r2.cache_push(up, prune=True, now=t0 + timedelta(days=2))


def test_a_pull_leaves_out_an_entry_deleted_under_it_and_can_keep_to_some(tmp_path, monkeypatch):
    bucket = Bucket()
    monkeypatch.setattr(r2, "client", lambda: bucket)
    up = tmp_path / "up"
    a, b = "a" * 64, "b" * 64
    _entry(up, a, files=("x",))
    _entry(up, b, files=("x",))
    r2.cache_push(up)
    down = tmp_path / "down"
    assert r2.cache_pull(down, entries={b}) == 1
    assert sorted(p.name for p in down.iterdir()) == [b]

    class Racing(Bucket):
        def download_file(self, bucket_, key, dest):
            if key == f"{a}/files/x":
                bucket.bytes.pop(f"{a}/meta.json")
                raise _missing()
            Bucket.download_file(bucket, bucket_, key, dest)

    racing = Racing()
    racing.existing, racing.etags = bucket.existing, bucket.etags
    monkeypatch.setattr(r2, "client", lambda: racing)
    assert r2.cache_pull(tmp_path / "raced") == 1
    assert not (tmp_path / "raced" / a / "meta.json").exists()
    assert (tmp_path / "raced" / b / "meta.json").exists()


def test_a_shard_pulls_only_the_entries_its_datasets_key_to(fixture_store, tmp_path, monkeypatch):
    from publicdata.__main__ import REGISTER, main
    from publicdata.build import cache_keys
    from publicdata.cache import BuildCache
    from publicdata.register import load

    seen = {}
    monkeypatch.setattr(
        r2, "cache_pull", lambda root, meta_only=False, entries=None: seen.update(e=entries) or 0
    )
    slug = next(
        d.slug for d in load(REGISTER) if cache_keys(BuildCache(tmp_path), d, fixture_store)
    )
    args = [
        "cache",
        "pull",
        "--cache",
        str(tmp_path),
        "--store",
        str(fixture_store),
        "--only",
        slug,
    ]
    assert main(args) == 0
    want = next(
        cache_keys(BuildCache(tmp_path), d, fixture_store) for d in load(REGISTER) if d.slug == slug
    )
    assert seen["e"] == want
    assert main(["cache", "pull", "--cache", str(tmp_path), "--only", slug]) == 2


def test_every_listed_source_must_be_in_the_raw_store(tmp_path, monkeypatch):
    import json

    import pytest

    for slug, version, man in (
        ("x", "2026-10-01", {"filename": "Crashes.CSV"}),
        ("y", "2026-10-02", {"filename": "a.zip", "source_withheld": "Its terms are unclear."}),
    ):
        v = tmp_path / "d" / slug / "v" / version
        v.mkdir(parents=True)
        (v / "manifest.json").write_text(json.dumps(man))
    assert r2.source_keys(tmp_path) == {"d/x/v/2026-10-01/source.csv": "x/2026-10-01/source.csv"}
    held = FakeS3({"x/2026-10-01/source.csv"})
    monkeypatch.setattr(r2, "client", lambda: held)
    assert r2.check_sources([tmp_path]) == 1
    # Only the published dataset's keys are listed, never the cache or the rest of the store.
    assert held.listed == ["x/"]
    other = {"x/2026-09-01/source.csv", "x-y/2026-10-01/source.csv"}
    monkeypatch.setattr(r2, "client", lambda: FakeS3(other))
    with pytest.raises(SystemExit, match="d/x/v/2026-10-01/source.csv"):
        r2.check_sources([tmp_path])


def test_pull_takes_snapshots_and_only_the_newest_fetch_of_a_rolling_source(tmp_path, monkeypatch):
    import shutil

    from publicdata import store

    def put(version, snapshot, history=False):
        d = tmp_path / "s" / "r" / version
        d.mkdir(parents=True)
        body = version.encode()
        m = store.Manifest("r", version, "", "", hashlib.sha256(body).hexdigest(), len(body),
                           "r.csv", "utf-8", {}, {}, snapshot=snapshot)  # fmt: skip
        if history:
            m.history = {"sha256": hashlib.sha256(b"h" + body).hexdigest(), "bytes": 1, "rows": 1}
        (d / "manifest.json").write_text(m.to_json())
        src = tmp_path / "raw" / "r" / version
        src.mkdir(parents=True)
        (src / "source.csv").write_bytes(body)
        (src / "history.parquet").write_bytes(b"h" + body)

    put("2026-08-04", True, history=True)
    put("2026-08-11", False, history=True)
    put("2026-09-01", True, history=True)
    put("2026-09-08", False, history=True)
    got = []

    class Fake(FakeS3):
        def download_file(self, bucket, key, dest):
            got.append(key)
            shutil.copyfile(tmp_path / "raw" / key, dest)

    monkeypatch.setattr(r2, "client", lambda: Fake(set()))
    r2.pull_store(tmp_path / "s")
    # The feed's read record is asked for too; this store has none.
    assert "r/read.json" in got
    got.remove("r/read.json")
    assert sorted(got) == [
        f"r/{v}/{f}"
        for v in ("2026-08-04", "2026-09-01", "2026-09-08")
        for f in ("history.parquet", "source.csv")
    ]
    for p in (tmp_path / "s").rglob("source.csv"):
        p.unlink()
    got.clear()
    r2.pull_store(tmp_path / "s", only=("r",), newest=True)
    assert got == ["r/2026-09-08/source.csv"]


def test_a_missing_newest_fetch_is_reported_and_the_other_datasets_still_pull(
    tmp_path, monkeypatch, capsys
):
    import shutil

    from publicdata import store

    for slug in ("a", "b"):
        d = tmp_path / "s" / slug / "2026-10-01"
        d.mkdir(parents=True)
        body = slug.encode()
        m = store.Manifest(slug, "2026-10-01", "", "", hashlib.sha256(body).hexdigest(), 1,
                           f"{slug}.csv", "utf-8", {}, {})  # fmt: skip
        (d / "manifest.json").write_text(m.to_json())
        (tmp_path / "raw" / slug / "2026-10-01").mkdir(parents=True)
        (tmp_path / "raw" / slug / "2026-10-01" / "source.csv").write_bytes(body)
    (tmp_path / "raw" / "a" / "2026-10-01" / "source.csv").unlink()

    class Fake(FakeS3):
        def download_file(self, bucket, key, dest):
            shutil.copyfile(tmp_path / "raw" / key, dest)

    monkeypatch.setattr(r2, "client", lambda: Fake(set()))
    assert r2.pull_store(tmp_path / "s", only=("a", "b"), newest=True) == 1
    assert "WARNING a: a/2026-10-01/source.csv not pulled" in capsys.readouterr().out
    assert (tmp_path / "s" / "b" / "2026-10-01" / "source.csv").is_file()
    assert not (tmp_path / "s" / "a" / "2026-10-01" / "source.csv").exists()


def test_a_feed_s_read_record_goes_to_the_raw_store_and_comes_back(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    from publicdata import store
    from publicdata.__main__ import main

    s = tmp_path / "s"
    d = s / "f" / "2026-10-05"
    d.mkdir(parents=True)
    m = store.Manifest("f", "2026-10-05", "", "", hashlib.sha256(b"x").hexdigest(), 1, "f.csv",
                       "utf-8", {}, {}, history={"sha256": hashlib.sha256(b"h").hexdigest(), "bytes": 1, "rows": 1})  # fmt: skip
    (d / "manifest.json").write_text(m.to_json())
    store.write_read(s, "f", "2026-10-06", "2026-10-05")
    held = {}

    class Fake(FakeS3):
        def upload_file(self, path, bucket, key, ExtraArgs):
            held[key] = Path(path).read_bytes()

        def download_file(self, bucket, key, dest):
            if key not in held:
                raise FileNotFoundError(key)
            Path(dest).write_bytes(held[key])

    monkeypatch.setattr(r2, "client", lambda: Fake(set()))
    monkeypatch.setattr("publicdata.__main__._committed_versions", lambda store_dir: set())
    assert main(["store", "push", "--store", str(s)]) == 0
    assert json.loads(held["f/read.json"]) == {"read": "2026-10-06", "fetch": "2026-10-05"}
    store.read_path(s, "f").unlink()
    held["f/2026-10-05/source.csv"] = b"x"
    held["f/2026-10-05/history.parquet"] = b"h"
    r2.pull_store(s)
    assert store.last_read(s, "f") == {"read": "2026-10-06", "fetch": "2026-10-05"}
    del held["f/read.json"]
    store.read_path(s, "f").unlink()
    r2.pull_store(s)
    assert not store.read_path(s, "f").exists()
    shutil.rmtree(s)


class ByteBucket:
    """An S3 bucket that keeps bytes, headers and metadata, enough for push, the downloader and
    the gzip restore. A put with IfMatch fails as R2 does when the object has changed."""

    def __init__(self, objects=None):
        self.objects = {}
        for k, body in (objects or {}).items():
            self.put(
                k,
                body,
                {
                    "ContentType": "text/csv; charset=utf-8",
                    "Metadata": {"sha256": hashlib.sha256(body).hexdigest()},
                },
            )
        self.puts = []
        self.heads = []
        self.deleted = []
        self.corrupt = set()
        self.before_put = {}

    def put(self, key, body, args):
        self.objects[key] = {"body": body, **args}

    def etag(self, key):
        return f'"{hashlib.md5(self.objects[key]["body"]).hexdigest()}"'

    def get_paginator(self, name):
        fake = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {
                    "Contents": [
                        {"Key": k, "Size": len(o["body"]), "ETag": fake.etag(k)}
                        for k, o in sorted(fake.objects.items())
                        if k.startswith(Prefix)
                    ]
                }

        return P()

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        o = self.objects[Key]
        head = {
            "ContentLength": len(o["body"]),
            "ContentType": o.get("ContentType"),
            "Metadata": dict(o.get("Metadata", {})),
            "ETag": self.etag(Key),
        }
        if o.get("ContentEncoding"):
            head["ContentEncoding"] = o["ContentEncoding"]
        return head

    def upload_file(self, path, bucket, key, ExtraArgs):
        self.put(key, Path(path).read_bytes(), ExtraArgs)
        self.puts.append(key)

    def put_object(self, Bucket, Key, Body, IfMatch=None, **args):
        from botocore.exceptions import ClientError

        if Key in self.before_put:
            self.before_put.pop(Key)(self)
        if IfMatch is not None and (Key not in self.objects or self.etag(Key) != IfMatch):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        body = Body.read()
        if Key in self.corrupt:
            self.corrupt.discard(Key)
            body = b"\x1f\x8bcorrupt"
        self.put(Key, body, args)
        self.puts.append(Key)
        return {"ETag": self.etag(Key)}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        del self.objects[Key]

    def download_file(self, bucket, key, path):
        from botocore.exceptions import ClientError

        if key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "GetObject")
        Path(path).write_bytes(self.objects[key]["body"])


CSV = b"a,b\n" + b"1,2\n" * 1000
V = "d/x/v/2026-04-24/"


def _version(tmp_path):
    from publicdata.serialise.writers.csv_gz import write_csv_gz

    v = tmp_path / "d" / "x" / "v" / "2026-04-24"
    v.mkdir(parents=True)
    (v / "data.csv").write_bytes(CSV)
    write_csv_gz(None, v / "data.csv.gz", v)
    return v


def test_the_client_sends_no_checksum_encoding(monkeypatch):
    import boto3

    seen = {}
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN_ID", "i")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: seen.update(k) or object())
    r2.client()
    assert seen["config"].request_checksum_calculation == "when_required"


def test_content_encoding_is_read_as_a_token_list():
    assert r2.is_gzip("gzip") and r2.is_gzip("gzip,aws-chunked") and r2.is_gzip(" GZIP ")
    assert not r2.is_gzip(None) and not r2.is_gzip("aws-chunked") and not r2.is_gzip("x-gzipped")


def test_what_is_stored_gzipped():
    big = 4096
    for name in (
        "data.csv",
        "data.ndjson",
        "data.json",
        "data.geojson",
        "schema.sql",
        "by/a/b.json",
    ):
        assert r2.stored_gzipped(V + name, big), name
    for name in ("data.sqlite", "data.duckdb", "data.parquet", "data.arrow", "data.xlsx", "data.gpkg", "data.pmtiles", "data.csv.gz", "source.csv", "index.html", "index.md"):  # fmt: skip
        assert not r2.stored_gzipped(V + name, big), name
    assert not r2.stored_gzipped(V + "manifest.json", 100)
    assert not r2.stored_gzipped("d/x/history.tar.zst", big)
    # The query layer's own files are read by range, whatever they are named.
    for key in ("_q/x/2026-04-24.parquet", "_q/x/v/2026-04-24/data.csv", "_q/x/2026-04-24.json"):
        assert not r2.stored_gzipped(key, big), key


def test_push_stores_text_gzipped_and_aliases_the_csv_gz(tmp_path, monkeypatch, capsys):
    import gzip

    v = _version(tmp_path)
    (v / "data.parquet").write_bytes(b"PAR1" * 1000)
    (v / "data.sqlite").write_bytes(b"SQLite format 3\x00" + bytes(4096))
    (v / "manifest.json").write_bytes(b"{}")
    (v / "index.html").write_bytes(b"<html>" * 1000)
    q = tmp_path / "_q" / "x"
    q.mkdir(parents=True)
    (q / "2026-04-24.parquet").write_bytes(b"PAR1" * 1000)
    b = ByteBucket()
    monkeypatch.setattr(r2, "client", lambda: b)
    r2.push(tmp_path, "publicdata-dist", immutable=r2.dated_file)
    o = b.objects
    assert V + "data.csv.gz" not in o
    assert o[V + "data.csv"]["ContentEncoding"] == "gzip"
    assert o[V + "data.csv"]["ContentType"] == "text/csv; charset=utf-8"
    assert o[V + "data.csv"]["body"] == (v / "data.csv.gz").read_bytes()
    assert gzip.decompress(o[V + "data.csv"]["body"]) == CSV
    assert o[V + "data.csv"]["Metadata"] == {
        "sha256": hashlib.sha256(CSV).hexdigest(),
        "size": str(len(CSV)),
    }
    for key in [V + n for n in ("data.parquet", "data.sqlite", "manifest.json", "index.html")] + [
        "_q/x/2026-04-24.parquet"
    ]:
        assert "ContentEncoding" not in o[key], key
    assert o["_q/x/2026-04-24.parquet"]["body"] == b"PAR1" * 1000
    assert f"alias publicdata-dist/{V}data.csv.gz" in capsys.readouterr().out
    # The next cached build leaves both out; the stored CSV answers for the gzip.
    r2.push(tmp_path / "empty", "publicdata-dist", expect=[V + "data.csv", V + "data.csv.gz"])
    with pytest.raises(SystemExit, match="not in R2"):
        r2.push(tmp_path / "empty", "publicdata-dist", expect=[V + "data.json.gz"])


def test_a_push_that_stopped_after_the_csv_aliases_its_gzip_on_the_next_run(tmp_path, monkeypatch):
    v = _version(tmp_path)
    b = ByteBucket()
    monkeypatch.setattr(r2, "client", lambda: b)
    r2.push(tmp_path, "publicdata-dist", immutable=r2.dated_file)
    b.puts.clear()
    r2.push(tmp_path, "publicdata-dist", immutable=r2.dated_file)
    assert b.puts == [] and V + "data.csv.gz" not in b.objects
    # A CSV an older push stored plain keeps a csv.gz of its own.
    old = ByteBucket({V + "data.csv": CSV})
    monkeypatch.setattr(r2, "client", lambda: old)
    r2.push(tmp_path, "publicdata-dist", immutable=r2.dated_file)
    assert old.puts == [V + "data.csv.gz"]
    assert old.objects[V + "data.csv.gz"]["body"] == (v / "data.csv.gz").read_bytes()


def test_a_replace_writes_the_old_csv_gz_again(tmp_path, monkeypatch):
    v = _version(tmp_path)
    stale = b"\x1f\x8bstale"
    b = ByteBucket({V + "data.csv": b"old", V + "data.csv.gz": stale})
    monkeypatch.setattr(r2, "client", lambda: b)
    r2.push(tmp_path, "publicdata-dist", replace=(V,), immutable=r2.dated_file)
    assert sorted(b.puts) == [V + "data.csv", V + "data.csv.gz"]
    assert b.objects[V + "data.csv.gz"]["body"] == (v / "data.csv.gz").read_bytes()
    assert b.objects[V + "data.csv"]["body"] == (v / "data.csv.gz").read_bytes()


def test_the_downloader_hands_back_the_file_a_gzipped_object_was(tmp_path, monkeypatch):
    v = tmp_path / "d" / "x" / "v" / "2026-04-24"
    v.mkdir(parents=True)
    js = b"[" + b"1," * 2000 + b"1]"
    (v / "data.json").write_bytes(js)
    (v / "data.parquet").write_bytes(b"PAR1" * 1000)
    b = ByteBucket()
    monkeypatch.setattr(r2, "client", lambda: b)
    r2.push(tmp_path, "publicdata-dist", immutable=r2.dated_file)
    b.objects[V + "data.json"]["ContentEncoding"] = "gzip,aws-chunked"
    get = r2.downloader("publicdata-dist")
    for name, want in (("data.json", js), ("data.parquet", b"PAR1" * 1000)):
        dest = tmp_path / name
        assert get(V + name, dest)
        assert dest.read_bytes() == want
    assert not get(V + "nothing.json", tmp_path / "n")
    b.objects[V + "data.json"]["Metadata"]["sha256"] = "0" * 64
    with pytest.raises(SystemExit, match="does not decode"):
        get(V + "data.json", tmp_path / "bad")


PLAIN = {
    V + "data.csv": CSV,
    V + "data.ndjson": b'{"a":1}\n' * 500,
    V + "data.parquet": b"PAR1" * 1000,
    V + "data.sqlite": b"SQLite format 3\x00" + bytes(4096),
    V + "source.csv": CSV,
    V + "index.html": b"<html>" * 1000,
    V + "schema.sql": b"create table t (a int);",
    "d/x/history.tar.zst": b"z" * 4096,
    "_q/x/2026-04-24.parquet": b"PAR1" * 1000,
}


def test_restore_gzip_is_a_dry_run_from_the_listing_unless_applied(monkeypatch, capsys):
    import gzip

    b = ByteBucket(PLAIN)
    monkeypatch.setattr(r2, "client", lambda: b)
    t = r2.restore_gzip()
    assert b.puts == [] and b.heads == []
    assert (t["objects"], t["before"]) == (2, len(CSV) + 4000)
    assert "dry run, 2 object(s)" in capsys.readouterr().out
    t = r2.restore_gzip(apply=True)
    assert sorted(b.puts) == [V + "data.csv", V + "data.ndjson"]
    assert t["done"] == 2 and t["saved"] > 0
    for k in (V + "data.csv", V + "data.ndjson"):
        o = b.objects[k]
        assert o["ContentEncoding"] == "gzip"
        assert gzip.decompress(o["body"]) == PLAIN[k]
        assert o["Metadata"] == {
            "sha256": hashlib.sha256(PLAIN[k]).hexdigest(),
            "size": str(len(PLAIN[k])),
        }
    for k in (V + "data.sqlite", V + "data.parquet", "_q/x/2026-04-24.parquet"):
        assert b.objects[k]["body"] == PLAIN[k] and "ContentEncoding" not in b.objects[k]
    assert b.objects[V + "data.csv"]["ContentType"] == "text/csv; charset=utf-8"
    assert "bytes saved" in capsys.readouterr().out
    b.puts.clear()
    b.objects[V + "data.ndjson"]["ContentEncoding"] = "gzip,aws-chunked"
    t = r2.restore_gzip(apply=True)
    assert b.puts == [] and t["done"] == 0


def test_restore_gzip_refuses_bytes_that_do_not_match_and_puts_back_a_bad_rewrite(
    monkeypatch, capsys
):
    b = ByteBucket({V + "data.csv": CSV, V + "data.json": b"[" + b"1," * 900 + b"1]"})
    b.objects[V + "data.json"]["Metadata"]["sha256"] = "0" * 64
    b.corrupt.add(V + "data.csv")
    monkeypatch.setattr(r2, "client", lambda: b)
    t = r2.restore_gzip(apply=True)
    assert t["failed"] == 2 and t["done"] == 0
    err = capsys.readouterr().err
    assert "data.json FAILED read back as" in err
    assert "data.csv FAILED" in err
    assert b.objects[V + "data.csv"]["body"] == CSV
    assert "ContentEncoding" not in b.objects[V + "data.csv"]
    assert b.objects[V + "data.json"]["body"].startswith(b"[")


def test_restore_gzip_never_overwrites_a_deploy_that_wrote_the_key_meanwhile(monkeypatch, capsys):
    b = ByteBucket({V + "data.csv": CSV})
    newer = b"\x1f\x8bnewer"

    def deploy(bucket):
        bucket.put(V + "data.csv", newer, {"ContentEncoding": "gzip"})

    b.before_put[V + "data.csv"] = deploy
    monkeypatch.setattr(r2, "client", lambda: b)
    t = r2.restore_gzip(apply=True)
    assert t["failed"] == 1
    assert b.objects[V + "data.csv"]["body"] == newer
    assert "PreconditionFailed" in capsys.readouterr().err


def test_a_failed_rewrite_is_not_put_back_over_a_deploy_that_wrote_after_it(
    monkeypatch, capsys, tmp_path
):
    b = ByteBucket({V + "data.csv": CSV})
    b.corrupt.add(V + "data.csv")
    newer = b"\x1f\x8bnewer"
    real_download = b.download_file

    def download(bucket, key, path):
        real_download(bucket, key, path)
        if Path(path).name == "back.gz":
            b.put(key, newer, {"ContentEncoding": "gzip"})

    monkeypatch.setattr(b, "download_file", download)
    monkeypatch.setattr(r2, "client", lambda: b)
    t = r2.restore_gzip(apply=True, kept=tmp_path / "kept")
    assert t["failed"] == 1
    assert b.objects[V + "data.csv"]["body"] == newer
    assert (tmp_path / "kept" / V / "data.csv").read_bytes() == CSV
    assert "was not put back" in capsys.readouterr().err


def test_restore_gzip_deletes_a_matching_csv_gz_only_when_asked(tmp_path, monkeypatch):
    v = _version(tmp_path)
    gz = (v / "data.csv.gz").read_bytes()
    objects = {V + "data.csv": CSV, V + "data.csv.gz": gz}
    b = ByteBucket(objects)
    monkeypatch.setattr(r2, "client", lambda: b)
    r2.restore_gzip(apply=True)
    assert b.deleted == [] and V + "data.csv.gz" in b.objects
    assert b.objects[V + "data.csv"]["body"] == gz
    t = r2.restore_gzip(apply=True, dedupe_csv_gz=True)
    assert b.deleted == [V + "data.csv.gz"] and t["deduped_bytes"] == len(gz)
    other = ByteBucket({V + "data.csv": CSV, V + "data.csv.gz": b"\x1f\x8bdifferent"})
    monkeypatch.setattr(r2, "client", lambda: other)
    r2.restore_gzip(apply=True, dedupe_csv_gz=True)
    assert other.deleted == []


class Dist(Bucket):
    """A bucket that answers byte ranges and lists each object's MD5, as R2 does for one part."""

    def __init__(self):
        super().__init__()
        self.ops = []

    def upload_file(self, path, bucket, key, ExtraArgs):
        super().upload_file(path, bucket, key, ExtraArgs)
        self.ops.append(("upload", key))

    def put_object(self, Bucket, Key, Body, ContentType):
        super().put_object(Bucket, Key, Body, ContentType)
        self.etags[Key] = hashlib.md5(Body).hexdigest()
        self.ops.append(("put", Key, Body))

    def head_object(self, Bucket, Key):
        super().head_object(Bucket, Key)
        return {"ContentLength": len(self.bytes[Key]), "Metadata": {}}

    def get_object(self, Bucket, Key, Range=None):
        import io

        a, b = (int(x) for x in Range.removeprefix("bytes=").split("-"))
        self.ops.append(("get", Key))
        return {"Body": io.BytesIO(self.bytes[Key][a : b + 1])}


Q = "_q/t/2026-01-02.parquet"
MARK = "_q/t/2026-01-02.layout.json"


def _lay(**kw):
    from publicdata.serialise import profile

    return {"profile": profile.VERSION, "sort": [], "key": ["id"], "lookup": [], "int32": []} | kw


def _copy(path, lay):
    import pyarrow as pa

    from publicdata.serialise import profile

    path.parent.mkdir(parents=True, exist_ok=True)
    t = pa.table({"id": [3, 1, 2], "year": [2024, 2023, 2024], "place": ["b", "a", "c"]})
    profile.write(t, {}, path, lay)
    return path


def _held(tmp_path, lay, marked=True):
    """A bucket holding the version's dated file and its query copy under lay."""
    from publicdata.serialise import profile

    fake = Dist()
    for key, p in (
        ("d/t/v/2026-01-02/data.parquet", _copy(tmp_path / "old" / "data.parquet", _lay())),
        (Q, _copy(tmp_path / "old" / "q.parquet", lay)),
    ):
        fake.upload_file(str(p), "b", key, {"ContentType": "x"})
    if marked:
        fake.put_object("b", MARK, profile.layout_body(lay), "application/json")
    fake.ops.clear()
    fake.puts.clear()
    return fake


def _remote(fake, key=Q):
    import io

    import pyarrow.parquet as pq

    return pq.read_metadata(io.BytesIO(fake.bytes[key]))


def test_a_query_push_lists_only_the_query_copies(tmp_path, monkeypatch):
    _copy(tmp_path / "t" / Q, _lay())
    fake = Dist()
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path / "t", "b", immutable=r2.dated_file, layouts={"t": _lay()}) == 1
    assert fake.listed == ["_q/"]


def test_a_layout_edit_reaches_the_query_copies_r2_holds(tmp_path, monkeypatch):
    from publicdata.serialise import profile

    for old, new in (
        (_lay(), _lay(sort=["year"])),
        (_lay(sort=["year"]), _lay(sort=["place"], lookup=["place"])),
        (_lay(int32=["id"]), _lay(int32=["id", "year"])),
        (_lay(int32=["id", "year"]), _lay()),
    ):
        fake = _held(tmp_path, old)
        dated = fake.bytes["d/t/v/2026-01-02/data.parquet"]
        root = tmp_path / "tree"
        _copy(root / Q, new)
        _copy(root / "d/t/v/2026-01-02/data.parquet", new)
        monkeypatch.setattr(r2, "client", lambda fake=fake: fake)
        assert r2.push(root, "b", immutable=r2.dated_file, layouts={"t": new}) == 1
        assert profile.follows(_remote(fake), new) and not profile.follows(_remote(fake), old)
        assert fake.bytes[MARK] == profile.layout_body(new)
        assert fake.bytes["d/t/v/2026-01-02/data.parquet"] == dated
        # The record follows the upload, so it never names a layout the copy does not have.
        assert [o[:2] for o in fake.ops] == [("upload", Q), ("put", MARK)]
        fake.ops.clear()
        assert r2.push(root, "b", immutable=r2.dated_file, layouts={"t": new}) == 0
        assert fake.ops == []


def test_an_unchanged_layout_uploads_no_query_copy(tmp_path, monkeypatch):
    lay = _lay(sort=["year"], int32=["id"])
    fake = _held(tmp_path, lay)
    _copy(tmp_path / "tree" / Q, lay)
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path / "tree", "b", immutable=r2.dated_file, layouts={"t": lay}) == 0
    assert fake.ops == [] and fake.heads == []


def test_a_copy_without_a_record_is_judged_by_its_footer(tmp_path, monkeypatch):
    from publicdata.serialise import profile

    lay = _lay(sort=["year"])
    fake = _held(tmp_path, lay, marked=False)
    _copy(tmp_path / "tree" / Q, lay)
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path / "tree", "b", immutable=r2.dated_file, layouts={"t": lay}) == 0
    assert ("upload", Q) not in fake.ops and fake.bytes[MARK] == profile.layout_body(lay)
    stale = _held(tmp_path, _lay(), marked=False)
    monkeypatch.setattr(r2, "client", lambda: stale)
    assert r2.push(tmp_path / "tree", "b", immutable=r2.dated_file, layouts={"t": lay}) == 1
    assert profile.follows(_remote(stale), lay) and stale.bytes[MARK] == profile.layout_body(lay)


def test_an_unreadable_copy_is_written_again(tmp_path, monkeypatch):
    from publicdata.serialise import profile

    lay = _lay(sort=["year"])
    fake = _held(tmp_path, lay, marked=False)
    fake.bytes[Q] = b"not parquet"
    _copy(tmp_path / "tree" / Q, lay)
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path / "tree", "b", immutable=r2.dated_file, layouts={"t": lay}) == 1
    assert profile.follows(_remote(fake), lay)


def test_a_cached_query_copy_must_follow_the_entry_in_r2(tmp_path, monkeypatch):
    import pytest

    from publicdata.serialise import profile

    lay = _lay(sort=["year"])
    (tmp_path / "tree").mkdir()
    for held, ok in ((lay, True), (_lay(), False)):
        for marked in (True, False):
            fake = _held(tmp_path, held, marked=marked)
            monkeypatch.setattr(r2, "client", lambda fake=fake: fake)
            args = (tmp_path / "tree", "b")
            kw = dict(immutable=r2.dated_file, expect=[Q], layouts={"t": lay})
            if ok:
                assert r2.push(*args, **kw) == 0
                assert fake.bytes[MARK] == profile.layout_body(lay)
            else:
                with pytest.raises(SystemExit, match=Q):
                    r2.push(*args, **kw)
                assert fake.ops == [] or fake.ops == [("get", Q)] * len(fake.ops)


def test_a_query_copy_the_build_wrote_otherwise_stops_the_push(tmp_path, monkeypatch):
    import pytest

    _copy(tmp_path / "tree" / Q, _lay())
    _copy(tmp_path / "tree" / "d/t/v/2026-01-02/data.parquet", _lay())
    fake = Dist()
    monkeypatch.setattr(r2, "client", lambda: fake)
    with pytest.raises(SystemExit, match=Q):
        r2.push(tmp_path / "tree", "b", immutable=r2.dated_file, layouts={"t": _lay(sort=["year"])})
    assert fake.ops == []


def test_the_shared_report_counts_identical_dated_files_and_changes_nothing(monkeypatch):
    from botocore.exceptions import ClientError

    class Multipart(ByteBucket):
        def etag(self, key):
            if key.endswith("2026-05-01/data.parquet"):
                return '"0123456789abcdef0123456789abcdef-2"'
            return super().etag(key)

        def head_object(self, Bucket, Key):
            if Key.endswith("2026-07-01/data.parquet"):
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return super().head_object(Bucket, Key)

    same, other = b"x" * 100, b"y" * 100
    bucket = Multipart(
        {
            "d/x/v/2026-04-24/data.csv": b"c" * 50,
            "d/x/v/2026-05-01/data.csv": b"c" * 50,
            "d/y/v/2026-05-01/data.csv": b"d" * 50,
            "d/x/v/2026-04-24/data.parquet": other,
            "d/x/v/2026-05-01/data.parquet": other,
            "d/x/v/2026-06-01/data.parquet": same,
            "d/y/v/2026-06-01/schema.json": same,
            "d/x/v/2026-07-01/data.parquet": other,
            "d/x/v/2026-04-24/index.html": same,
            "d/x/history.tar.zst": same,
            "_q/x/2026-04-24/data.parquet": same,
        }
    )
    monkeypatch.setattr(r2, "client", lambda: bucket)
    t = r2.shared_report()
    assert t["objects"] == 7 and t["bytes"] == 550 and t["gone"] == 1
    assert t["copies"] == 3 and t["saved"] == 250 and t["across"] == 100
    assert t["by_ext"] == {"csv": [1, 50], "json": [1, 100], "parquet": [1, 100]}
    assert sorted(bucket.heads) == sorted(
        f"d/{k}"
        for k in (
            "x/v/2026-04-24/data.parquet",
            "x/v/2026-05-01/data.parquet",
            "x/v/2026-06-01/data.parquet",
            "y/v/2026-06-01/schema.json",
        )
    )
    assert bucket.puts == [] and bucket.deleted == []


class _Tagged(ByteBucket):
    """A bucket whose listed ETags and stored SHA-256s are set per key, as a multipart upload or
    an object put before #51 leaves them."""

    def __init__(self, objects, tags, shas):
        super().__init__(objects)
        self.tags = tags
        for k, v in shas.items():
            self.objects[k]["Metadata"] = {"sha256": v} if v else {}

    def etag(self, key):
        return self.tags.get(key) or super().etag(key)


def test_the_shared_report_counts_files_it_could_match_on_etag_only(monkeypatch, capsys):
    from types import SimpleNamespace

    from publicdata.__main__ import cmd_r2_shared_report

    a, b = "d/x/v/2026-04-24/data.parquet", "d/x/v/2026-05-01/data.parquet"
    tags = {a: f'"{"1" * 32}-2"', b: f'"{"2" * 32}-3"'}
    bucket = _Tagged({a: b"p" * 100, b: b"p" * 100}, tags, {b: ""})
    monkeypatch.setattr(r2, "client", lambda: bucket)
    t = r2.shared_report()
    assert t["heads"] == 2 and t["unhashed"] == 1 and t["copies"] == 0
    assert cmd_r2_shared_report(SimpleNamespace(prefix="d/")) == 0
    assert "1 HEADed file(s) with no SHA-256 matched on ETag alone" in capsys.readouterr().out


def test_the_shared_report_joins_a_file_that_matches_two_copies(monkeypatch, capsys):
    from types import SimpleNamespace

    from publicdata.__main__ import cmd_r2_shared_report

    a, b, c = (f"d/x/v/2026-0{m}-01/SHA256SUMS" for m in (4, 5, 6))
    tags = {b: f'"{"3" * 32}-2"', c: f'"{"3" * 32}-2"'}
    shas = {a: "s1", b: "s2", c: "s1"}
    bucket = _Tagged({a: b"a" * 100, b: b"b" * 100, c: b"b" * 100}, tags, shas)
    monkeypatch.setattr(r2, "client", lambda: bucket)
    t = r2.shared_report()
    assert t["copies"] == 2 and t["saved"] == 200 and t["by_ext"] == {"": [2, 200]}
    assert cmd_r2_shared_report(SimpleNamespace(prefix="d/")) == 0
    assert "shared: no extension: 2 extra copies, 200 bytes" in capsys.readouterr().out


def test_the_shared_report_counts_a_file_once_per_dataset_across_datasets(monkeypatch):
    for owners, across in ((("a", "a", "b"), 100), (("a", "b", "b"), 100), (("a", "b", "c"), 200)):
        keys = [f"d/{o}/v/2026-0{i + 4}-01/data.csv" for i, o in enumerate(owners)]
        bucket = ByteBucket({k: b"z" * 100 for k in keys})
        monkeypatch.setattr(r2, "client", lambda bucket=bucket: bucket)
        t = r2.shared_report()
        assert (t["copies"], t["saved"], t["across"]) == (2, 200, across), owners
