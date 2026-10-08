import hashlib
import io
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from publicdata import r2
from publicdata.__main__ import REGISTER, _cached_versions, main
from publicdata.build import cache_keys
from publicdata.cache import BuildCache
from publicdata.register import load
from publicdata.serialise import profile

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from typing import Unpack

    from botocore.exceptions import ClientError

    from publicdata.provenance import Header
    from publicdata.serialise.profile import Layout


# The parts of boto3's S3 requests and answers that r2 sends and reads.
class _Extra(TypedDict, total=False):
    ContentType: str
    Metadata: dict[str, str]


class _Listed(TypedDict):
    Key: str
    ETag: str


class _Page(TypedDict):
    Contents: list[_Listed]


class _Head(TypedDict, total=False):
    Metadata: dict[str, str]
    ContentLength: int


class _Got(TypedDict):
    Body: io.BytesIO


class _Key(TypedDict):
    Key: str


class _Delete(TypedDict):
    Objects: list[_Key]
    Quiet: bool


class _Error(TypedDict):
    Key: str
    Code: str


class _Deleted(TypedDict, total=False):
    Errors: list[_Error]


class FakeS3:
    def __init__(
        self,
        existing: Iterable[str],
        meta: dict[str, dict[str, str]] | None = None,
        etags: dict[str, str] | None = None,
    ) -> None:
        self.existing = set(existing)
        self.meta = meta or {}
        self.etags = etags or {}
        self.puts: list[tuple[str, str]] = []
        self.heads: list[str] = []
        self.listed: list[str] = []

    def get_paginator(self, name: str) -> object:
        fake = self

        class P:
            def paginate(self, Bucket: str, Prefix: str) -> Iterator[_Page]:
                fake.listed.append(Prefix)
                yield {
                    "Contents": [
                        {"Key": k, "ETag": f'"{fake.etags.get(k, "")}"'}
                        for k in sorted(fake.existing)
                        if k.startswith(Prefix)
                    ]
                }

        return P()

    def head_object(self, Bucket: str, Key: str) -> _Head:
        self.heads.append(Key)
        return {"Metadata": self.meta.get(Key, {})}

    def upload_file(self, path: str, bucket: str, key: str, ExtraArgs: _Extra) -> None:
        self.puts.append((key, ExtraArgs["ContentType"]))


def test_push_skips_existing_keys_unless_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_immutable_existing_keys_are_not_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = tmp_path / "d" / "x" / "v" / "2026-04-24"
    v.mkdir(parents=True)
    (v / "data.csv").write_text("a\n1\n")
    monkeypatch.setattr(r2, "client", lambda: FakeS3({"d/x/v/2026-04-24/data.csv"}))
    hashed: list[Path] = []

    def sha256(p: Path) -> str:
        hashed.append(p)
        return "h"

    monkeypatch.setattr(r2, "_sha256", sha256)
    assert r2.push(tmp_path, "b", immutable=r2.versioned) == 0
    assert hashed == []


def test_mutable_key_is_replaced_only_when_its_hash_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_split_moves_every_version_file_and_its_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_dist_push_rejects_a_prefix_that_is_not_a_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(r2, "client", lambda: FakeS3(set()))
    assert main(["dist-push", "--large", str(tmp_path), "--replace", "d/"]) == 2
    assert "prefixes only" in capsys.readouterr().out
    assert main(["dist-push", "--large", str(tmp_path), "--replace", "d/x/v/2026-04-24/"]) == 0


def test_pull_skips_the_versions_the_build_cache_holds(
    fixture_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = tmp_path / "store"
    shutil.copytree(fixture_store, s)
    cache = tmp_path / "cache"
    assert main(["build", "--store", str(s), "--out", str(tmp_path / "o"), "--cache", str(cache), "--absent", str(tmp_path / "a.json")]) == 0  # fmt: skip
    held = _cached_versions(s, cache)
    datasets = {p.parts[-3] for p in s.glob("*/*/manifest.json")} - {"catalogue"}
    assert {slug for slug, _ in held} == datasets
    for p in s.glob("*/*/source.*"):
        p.unlink()
    got: list[str] = []

    class Fake(FakeS3):
        def download_file(self, bucket: str, key: str, dest: str) -> None:
            got.append(key)
            shutil.copyfile(fixture_store / key, dest)

    monkeypatch.setattr(r2, "client", lambda: Fake(set()))
    r2.pull_store(s, skip=held)
    assert got == [k for k in got if k.startswith("catalogue/")]
    assert got


def test_a_version_page_is_uploaded_again_only_when_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = tmp_path / "d" / "x" / "v" / "2026-04-24"
    v.mkdir(parents=True)
    (v / "index.html").write_text("newest")
    (v / "data.csv").write_text("a\n1\n")
    keys = {"d/x/v/2026-04-24/index.html", "d/x/v/2026-04-24/data.csv"}
    md5 = hashlib.md5(b"newest", usedforsecurity=False).hexdigest()
    same = FakeS3(keys, etags={"d/x/v/2026-04-24/index.html": md5})
    monkeypatch.setattr(r2, "client", lambda: same)
    assert r2.push(tmp_path, "b", immutable=r2.dated_file) == 0
    assert same.heads == []
    old = FakeS3(
        keys,
        etags={
            "d/x/v/2026-04-24/index.html": hashlib.md5(b"old", usedforsecurity=False).hexdigest()
        },
    )
    monkeypatch.setattr(r2, "client", lambda: old)
    assert r2.push(tmp_path, "b", immutable=r2.dated_file) == 1
    assert [k for k, _ in old.puts] == ["d/x/v/2026-04-24/index.html"]
    assert old.heads == []
    assert r2.dated_file("d/x/v/2026-04-24/data.csv")
    assert not r2.dated_file("d/x/history.tar.zst")


def test_a_dated_only_push_leaves_the_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = tmp_path / "d" / "x" / "v" / "2026-04-24"
    v.mkdir(parents=True)
    (v / "data.csv").write_text("a\n1\n")
    (v / "index.html").write_text("<p>newest</p>")
    (tmp_path / "d" / "x" / "history.tar.zst").write_bytes(b"z")
    fake = FakeS3(set())
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path, "b", include=r2.dated_file) == 1
    assert [k for k, _ in fake.puts] == ["d/x/v/2026-04-24/data.csv"]


def test_store_push_replaces_raw_bytes_only_for_versions_main_never_took(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def __init__(self) -> None:
        super().__init__(set())
        self.bytes: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.batches: list[list[str]] = []

    def upload_file(self, path: str, bucket: str, key: str, ExtraArgs: _Extra) -> None:
        super().upload_file(path, bucket, key, ExtraArgs)
        data = Path(path).read_bytes()
        self.bytes[key] = data
        self.existing.add(key)
        self.etags[key] = hashlib.md5(data, usedforsecurity=False).hexdigest()

    def download_file(self, bucket: str, key: str, dest: str) -> None:
        if key not in self.bytes:
            raise _missing()
        Path(dest).write_bytes(self.bytes[key])

    def get_object(self, Bucket: str, Key: str) -> _Got:
        if Key not in self.bytes:
            raise _missing()
        return {"Body": io.BytesIO(self.bytes[Key])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        self.bytes[Key] = Body
        self.existing.add(Key)

    def delete_objects(self, Bucket: str, Delete: _Delete) -> _Deleted:
        self.batches.append([o["Key"] for o in Delete["Objects"]])
        for o in Delete["Objects"]:
            self.deleted.append(o["Key"])
            self.existing.discard(o["Key"])
            self.bytes.pop(o["Key"])
        return {}


def _missing() -> ClientError:
    from botocore.exceptions import ClientError  # noqa: PLC0415 - the deploy extra

    return ClientError({"Error": {"Code": "404"}}, "GetObject")


def _entry(root: Path, key: str, meta: str = "{}", files: Iterable[str] = ()) -> None:
    (root / key / "files").mkdir(parents=True)
    for name in files:
        (root / key / "files" / name).write_text(name)
    (root / key / "meta.json").write_text(meta)


def test_the_cache_goes_up_once_comes_back_whole_and_prunes_what_the_disk_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bucket = Bucket()
    monkeypatch.setattr(r2, "client", lambda: bucket)
    up = tmp_path / "up"
    _entry(up, "a" * 64, files=("manifest.json",))
    _entry(up, "b" * 64)
    (up / ".c.tmp").mkdir()  # an entry a failed put left half written
    assert r2.cache_push(up) == (3, 0)
    # meta.json goes last, so an interrupted push never leaves a record without its files.
    puts = [k for k, _ in bucket.puts]
    assert sorted(puts[-2:]) == [f"_build/{'a' * 64}/meta.json", f"_build/{'b' * 64}/meta.json"]
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

    shutil.rmtree(up / ("b" * 64))
    assert r2.cache_push(up) == (0, 0)  # without prune nothing goes
    t0 = datetime(2026, 10, 6, tzinfo=UTC)
    # A preview may have listed the entry, so the first push to find it unused only notes it.
    assert r2.cache_push(up, prune=True, now=t0) == (0, 0)
    assert bucket.deleted == []
    assert r2.cache_pull(tmp_path / "again") == 2
    assert r2.cache_push(up, prune=True, now=t0 + timedelta(hours=23)) == (0, 0)
    assert r2.cache_push(up, prune=True, now=t0 + timedelta(hours=24)) == (0, 1)
    assert bucket.deleted == [f"_build/{'b' * 64}/meta.json"]
    assert json.loads(bucket.bytes[r2.UNUSED]) == {}


def test_an_entry_used_again_is_no_longer_due_and_pruning_takes_its_record_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        [f"_build/{a}/meta.json"],
        [f"_build/{a}/files/x", f"_build/{a}/files/y"],
    ]


def test_a_delete_r2_refuses_fails_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Refusing(Bucket):
        def delete_objects(self, Bucket: str, Delete: _Delete) -> _Deleted:
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


def test_a_pull_leaves_out_an_entry_deleted_under_it_and_can_keep_to_some(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        def download_file(self, bucket_: str, key: str, dest: str) -> None:
            if key == f"_build/{a}/files/x":
                bucket.bytes.pop(f"_build/{a}/meta.json")
                raise _missing()
            Bucket.download_file(bucket, bucket_, key, dest)

    racing = Racing()
    racing.existing, racing.etags = bucket.existing, bucket.etags
    monkeypatch.setattr(r2, "client", lambda: racing)
    assert r2.cache_pull(tmp_path / "raced") == 1
    assert not (tmp_path / "raced" / a / "meta.json").exists()
    assert (tmp_path / "raced" / b / "meta.json").exists()


def test_a_shard_pulls_only_the_entries_its_datasets_key_to(
    fixture_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, set[str] | None] = {}
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


def test_every_listed_source_must_be_in_the_raw_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    with pytest.raises(SystemExit, match=re.escape("d/x/v/2026-10-01/source.csv")):
        r2.check_sources([tmp_path])


class Dist(Bucket):
    """A bucket that answers byte ranges and lists each object's MD5, as R2 does for one part."""

    def __init__(self) -> None:
        super().__init__()
        self.ops: list[tuple[object, ...]] = []

    def upload_file(self, path: str, bucket: str, key: str, ExtraArgs: _Extra) -> None:
        super().upload_file(path, bucket, key, ExtraArgs)
        self.ops.append(("upload", key))

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        super().put_object(Bucket, Key, Body, ContentType)
        self.etags[Key] = hashlib.md5(Body, usedforsecurity=False).hexdigest()
        self.ops.append(("put", Key, Body))

    def head_object(self, Bucket: str, Key: str) -> _Head:
        super().head_object(Bucket, Key)
        return {"ContentLength": len(self.bytes[Key]), "Metadata": {}}

    def get_object(self, Bucket: str, Key: str, Range: str | None = None) -> _Got:
        assert Range is not None
        a, b = (int(x) for x in Range.removeprefix("bytes=").split("-"))
        self.ops.append(("get", Key))
        return {"Body": io.BytesIO(self.bytes[Key][a : b + 1])}


Q = "_q/t/2026-01-02.parquet"
MARK = "_q/t/2026-01-02.layout.json"


def _lay(**kw: Unpack[Layout]) -> Layout:
    base: Layout = {
        "profile": profile.VERSION,
        "sort": [],
        "key": ["id"],
        "lookup": [],
        "int32": [],
    }
    return base | kw


def _copy(path: Path, lay: Layout) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    t = pa.table({"id": [3, 1, 2], "year": [2024, 2023, 2024], "place": ["b", "a", "c"]})
    profile.write(t, cast("Header", {}), path, lay)
    return path


def _held(tmp_path: Path, lay: Layout, *, marked: bool = True) -> Dist:
    """A bucket holding the version's dated file and its query copy under lay."""
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


def _remote(fake: Bucket, key: str = Q) -> pq.FileMetaData:
    return pq.read_metadata(io.BytesIO(fake.bytes[key]))


def test_a_query_push_lists_only_the_query_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _copy(tmp_path / "t" / Q, _lay())
    fake = Dist()
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path / "t", "b", immutable=r2.dated_file, layouts={"t": _lay()}) == 1
    assert fake.listed == ["_q/"]


def test_a_layout_edit_reaches_the_query_copies_r2_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        assert profile.follows(_remote(fake), new)
        assert not profile.follows(_remote(fake), old)
        assert fake.bytes[MARK] == profile.layout_body(new)
        assert fake.bytes["d/t/v/2026-01-02/data.parquet"] == dated
        # The record follows the upload, so it never names a layout the copy does not have.
        assert [o[:2] for o in fake.ops] == [("upload", Q), ("put", MARK)]
        fake.ops.clear()
        assert r2.push(root, "b", immutable=r2.dated_file, layouts={"t": new}) == 0
        assert fake.ops == []


def test_an_unchanged_layout_uploads_no_query_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = _lay(sort=["year"], int32=["id"])
    fake = _held(tmp_path, lay)
    _copy(tmp_path / "tree" / Q, lay)
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path / "tree", "b", immutable=r2.dated_file, layouts={"t": lay}) == 0
    assert fake.ops == []
    assert fake.heads == []


def test_a_copy_without_a_record_is_judged_by_its_footer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = _lay(sort=["year"])
    fake = _held(tmp_path, lay, marked=False)
    _copy(tmp_path / "tree" / Q, lay)
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path / "tree", "b", immutable=r2.dated_file, layouts={"t": lay}) == 0
    assert ("upload", Q) not in fake.ops
    assert fake.bytes[MARK] == profile.layout_body(lay)
    stale = _held(tmp_path, _lay(), marked=False)
    monkeypatch.setattr(r2, "client", lambda: stale)
    assert r2.push(tmp_path / "tree", "b", immutable=r2.dated_file, layouts={"t": lay}) == 1
    assert profile.follows(_remote(stale), lay)
    assert stale.bytes[MARK] == profile.layout_body(lay)


def test_an_unreadable_copy_is_written_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = _lay(sort=["year"])
    fake = _held(tmp_path, lay, marked=False)
    fake.bytes[Q] = b"not parquet"
    _copy(tmp_path / "tree" / Q, lay)
    monkeypatch.setattr(r2, "client", lambda: fake)
    assert r2.push(tmp_path / "tree", "b", immutable=r2.dated_file, layouts={"t": lay}) == 1
    assert profile.follows(_remote(fake), lay)


def test_a_cached_query_copy_must_follow_the_entry_in_r2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lay = _lay(sort=["year"])
    (tmp_path / "tree").mkdir()
    for held, ok in ((lay, True), (_lay(), False)):
        for marked in (True, False):
            fake = _held(tmp_path, held, marked=marked)
            monkeypatch.setattr(r2, "client", lambda fake=fake: fake)
            args = (tmp_path / "tree", "b")
            if ok:
                assert r2.push(*args, immutable=r2.dated_file, expect=[Q], layouts={"t": lay}) == 0
                assert fake.bytes[MARK] == profile.layout_body(lay)
            else:
                with pytest.raises(SystemExit, match=Q):
                    r2.push(*args, immutable=r2.dated_file, expect=[Q], layouts={"t": lay})
                assert fake.ops == [] or fake.ops == [("get", Q)] * len(fake.ops)


def test_a_query_copy_the_build_wrote_otherwise_stops_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _copy(tmp_path / "tree" / Q, _lay())
    _copy(tmp_path / "tree" / "d/t/v/2026-01-02/data.parquet", _lay())
    fake = Dist()
    monkeypatch.setattr(r2, "client", lambda: fake)
    with pytest.raises(SystemExit, match=Q):
        r2.push(tmp_path / "tree", "b", immutable=r2.dated_file, layouts={"t": _lay(sort=["year"])})
    assert fake.ops == []
