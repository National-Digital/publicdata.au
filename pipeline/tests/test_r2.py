import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta

from publicdata import r2


class FakeS3:
    def __init__(self, existing, meta=None, etags=None):
        self.existing = set(existing)
        self.meta = meta or {}
        self.etags = etags or {}
        self.puts = []
        self.heads = []

    def get_paginator(self, name):
        fake = self

        class P:
            def paginate(self, Bucket, Prefix):
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
    monkeypatch.setattr(r2, "client", lambda: FakeS3({"x/2026-10-01/source.csv"}))
    assert r2.check_sources([tmp_path]) == 1
    monkeypatch.setattr(r2, "client", lambda: FakeS3({"x/2026-09-01/source.csv"}))
    with pytest.raises(SystemExit, match="d/x/v/2026-10-01/source.csv"):
        r2.check_sources([tmp_path])
