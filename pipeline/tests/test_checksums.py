import datetime as dt
import hashlib
import io

from publicdata import cache, checksums

T0 = dt.datetime(2026, 4, 24, tzinfo=dt.UTC)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class FakeS3:
    """Objects as key -> (bytes, last modified, stored sha256 or None)."""

    def __init__(self, objects):
        self.objects = dict(objects)
        self.heads, self.gets, self.puts = [], [], []

    def get_paginator(self, name):
        fake = self

        class P:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                keys = sorted(k for k in fake.objects if k.startswith(Prefix))
                if Delimiter:
                    tops = sorted(
                        {Prefix + k[len(Prefix) :].split(Delimiter)[0] + "/" for k in keys}
                    )
                    yield {"CommonPrefixes": [{"Prefix": p} for p in tops]}
                    return
                yield {"Contents": [{"Key": k, "LastModified": fake.objects[k][1]} for k in keys]}

        return P()

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        h = self.objects[Key][2]
        return {"Metadata": {"sha256": h} if h else {}}

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def put_object(self, Bucket, Key, Body, ContentType, Metadata):
        assert Metadata["sha256"] == sha(Body)
        self.puts.append((Key, ContentType))
        self.objects[Key] = (Body, T0 + dt.timedelta(hours=1), Metadata["sha256"])


V = "d/x/v/2026-04-24/"


def version(at=T0):
    files = {"data.csv": b"a\n1\n", "data.parquet": b"PAR1", "by/a/1.json": b"{}"}
    out = {V + k: (b, at, sha(b)) for k, b in files.items()}
    out[V + "index.html"] = (b"<html>", at, sha(b"<html>"))
    out[V + "index.md"] = (b"# v", at, sha(b"# v"))
    return out


def test_render_parses_back_sorted():
    sums = {"b.csv": "1" * 64, "a.csv": "2" * 64}
    text = checksums.render(sums)
    assert text == f"{'2' * 64}  a.csv\n{'1' * 64}  b.csv\n"
    assert checksums.parse(text + "junk\n") == sums


def test_writes_a_list_under_download_names_from_stored_hashes():
    s3 = FakeS3(version())
    assert checksums.update(["x"], s3=s3) == (1, [])
    assert s3.puts == [(V + "SHA256SUMS", "text/plain; charset=utf-8")]
    assert s3.gets == []
    sums = checksums.parse(s3.objects[V + "SHA256SUMS"][0].decode())
    assert sums == {
        "x_2026-04-24.csv": sha(b"a\n1\n"),
        "x_2026-04-24.parquet": sha(b"PAR1"),
        "x_2026-04-24_by_a_1.json": sha(b"{}"),
    }


def test_a_current_list_is_left_alone():
    s3 = FakeS3(version())
    checksums.update(["x"], s3=s3)
    s3.heads.clear()
    s3.puts.clear()
    assert checksums.update(["x"], s3=s3) == (0, [])
    assert s3.heads == [] and s3.gets == [] and s3.puts == []


def test_a_file_added_later_extends_the_list_without_rehashing_the_rest():
    s3 = FakeS3(version())
    checksums.update(["x"], s3=s3)
    s3.heads.clear()
    s3.objects[V + "data.xlsx"] = (b"PK", T0 + dt.timedelta(days=1), sha(b"PK"))
    assert checksums.update(["x"], s3=s3)[0] == 1
    assert s3.heads == [V + "data.xlsx"]
    sums = checksums.parse(s3.objects[V + "SHA256SUMS"][0].decode())
    assert sums["x_2026-04-24.xlsx"] == sha(b"PK")
    assert len(sums) == 4


def test_a_replaced_version_is_hashed_again():
    s3 = FakeS3(version())
    checksums.update(["x"], s3=s3)
    s3.heads.clear()
    assert checksums.update(["x"], s3=s3, replace=(V,))[0] == 1
    assert len(s3.heads) == 3


def test_a_file_without_a_stored_hash_holds_the_version_unless_downloads_are_allowed():
    objs = version()
    objs[V + "data.csv"] = (b"a\n1\n", T0, None)
    s3 = FakeS3(objs)
    assert checksums.update(["x"], s3=s3) == (0, [V])
    assert V + "SHA256SUMS" not in s3.objects
    assert checksums.update(["x"], s3=s3, download=True) == (1, [])
    sums = checksums.parse(s3.objects[V + "SHA256SUMS"][0].decode())
    assert sums["x_2026-04-24.csv"] == sha(b"a\n1\n")


def test_backfill_finds_every_dataset_in_the_bucket(tmp_path):
    objs = version()
    objs["d/y/v/2026-01-01/data.csv"] = (b"z", T0, sha(b"z"))
    objs["d/y/versions.json"] = (b"[]", T0, sha(b"[]"))
    s3 = FakeS3(objs)
    assert checksums.slugs_in_bucket(s3) == ["x", "y"]
    assert checksums.update(checksums.slugs_in_bucket(s3), s3=s3)[0] == 2
    (tmp_path / "dist" / "d" / "x" / "v").mkdir(parents=True)
    (tmp_path / "dist" / "d" / "z").mkdir(parents=True)
    (tmp_path / "large" / "d" / "y" / "v").mkdir(parents=True)
    assert checksums.slugs_in([tmp_path / "dist", tmp_path / "large"]) == ["x", "y"]


def test_the_build_cache_key_does_not_cover_checksums():
    assert all(p.name not in ("checksums.py", "__main__.py") for p in cache.code_files())
