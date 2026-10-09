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
        self.heads, self.gets, self.puts, self.deletes = [], [], [], []

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

    def delete_object(self, Bucket, Key):
        self.deletes.append(Key)
        del self.objects[Key]

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
    assert checksums.update(["x"], s3=s3) == (1, [], [])
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
    assert checksums.update(["x"], s3=s3) == (0, [], [])
    assert s3.heads == [] and s3.gets == [] and s3.puts == []


def test_a_file_written_after_the_list_is_reported_and_the_list_is_kept():
    s3 = FakeS3(version())
    checksums.update(["x"], s3=s3)
    before = s3.objects[V + "SHA256SUMS"]
    s3.puts.clear()
    s3.objects[V + "data.xlsx"] = (b"PK", T0 + dt.timedelta(days=1), sha(b"PK"))
    s3.objects[V + "data.csv"] = (b"b\n2\n", T0 + dt.timedelta(days=1), sha(b"b\n2\n"))
    assert checksums.update(["x"], s3=s3) == (0, [], [V + "data.csv", V + "data.xlsx"])
    assert s3.puts == [] and s3.deletes == []
    assert s3.objects[V + "SHA256SUMS"] == before


def test_a_file_stored_again_with_the_bytes_its_line_holds_is_not_reported():
    s3 = FakeS3(version())
    checksums.update(["x"], s3=s3)
    s3.heads.clear()
    s3.puts.clear()
    s3.objects[V + "data.csv"] = (b"gzipped", T0 + dt.timedelta(days=1), sha(b"a\n1\n"))
    assert checksums.update(["x"], s3=s3) == (0, [], [])
    assert s3.heads == [V + "data.csv"] and s3.puts == []


def test_a_resign_records_the_list_as_it_stands_and_never_rewrites_it():
    s3 = FakeS3(version())
    checksums.update(["x"], s3=s3)
    before = s3.objects[V + "SHA256SUMS"]
    s3.puts.clear()
    s3.objects[V + "data.xlsx"] = (b"PK", T0 + dt.timedelta(days=1), sha(b"PK"))
    lists: dict[str, str] = {}
    n, _, changed = checksums.update(["x"], s3=s3, lists=lists, every=True, download=True)
    assert n == 0 and changed == [V + "data.xlsx"] and s3.puts == []
    assert lists == {V + "SHA256SUMS": sha(before[0])}


def test_a_replaced_version_is_listed_again_from_scratch():
    s3 = FakeS3(version())
    checksums.update(["x"], s3=s3)
    s3.heads.clear()
    s3.objects[V + "data.csv"] = (b"b\n2\n", T0 + dt.timedelta(days=1), sha(b"b\n2\n"))
    s3.objects[V + "data.xlsx"] = (b"PK", T0 + dt.timedelta(days=1), sha(b"PK"))
    assert checksums.update(["x"], s3=s3, replace=(V,)) == (1, [], [])
    assert s3.deletes == [V + "SHA256SUMS"] and len(s3.heads) == 4
    sums = checksums.parse(s3.objects[V + "SHA256SUMS"][0].decode())
    assert sums["x_2026-04-24.csv"] == sha(b"b\n2\n") and sums["x_2026-04-24.xlsx"] == sha(b"PK")


def test_a_replace_that_stops_leaves_no_list_for_the_next_deploy_to_write():
    objs = version()
    s3 = FakeS3(objs)
    checksums.update(["x"], s3=s3)
    s3.objects[V + "data.csv"] = (b"b\n2\n", T0 + dt.timedelta(days=1), None)
    assert checksums.update(["x"], s3=s3, replace=(V,)) == (0, [V], [])
    assert V + "SHA256SUMS" not in s3.objects
    assert checksums.update(["x"], s3=s3, download=True)[0] == 1


def test_a_file_without_a_stored_hash_holds_the_version_unless_downloads_are_allowed():
    objs = version()
    objs[V + "data.csv"] = (b"a\n1\n", T0, None)
    s3 = FakeS3(objs)
    assert checksums.update(["x"], s3=s3) == (0, [V], [])
    assert V + "SHA256SUMS" not in s3.objects
    assert checksums.update(["x"], s3=s3, download=True) == (1, [], [])
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


def manifest(**kw) -> bytes:
    import json

    return json.dumps({"sha256": sha(b"raw bytes"), "filename": "Crashes.CSV", **kw}).encode()


def test_a_source_served_from_the_raw_store_takes_its_hash_from_the_manifest():
    objs = version()
    objs[V + "manifest.json"] = (manifest(), T0, sha(manifest()))
    s3 = FakeS3(objs)
    assert checksums.update(["x"], s3=s3) == (1, [], [])
    sums = checksums.parse(s3.objects[V + "SHA256SUMS"][0].decode())
    assert sums["x_2026-04-24_source.csv"] == sha(b"raw bytes")
    assert len(sums) == 5


def test_a_source_in_the_listing_or_withheld_adds_no_manifest_line():
    objs = version()
    objs[V + "manifest.json"] = (manifest(), T0, sha(manifest()))
    objs[V + "source.csv"] = (b"raw bytes", T0, sha(b"raw bytes"))
    s3 = FakeS3(objs)
    checksums.update(["x"], s3=s3)
    assert V + "manifest.json" not in s3.gets
    sums = checksums.parse(s3.objects[V + "SHA256SUMS"][0].decode())
    assert sums["x_2026-04-24_source.csv"] == sha(b"raw bytes")
    withheld = manifest(source_withheld="changed one column")
    objs = version()
    objs[V + "manifest.json"] = (withheld, T0, sha(withheld))
    s3 = FakeS3(objs)
    checksums.update(["x"], s3=s3)
    sums = checksums.parse(s3.objects[V + "SHA256SUMS"][0].decode())
    assert not any("source" in n for n in sums)


def test_the_lists_written_are_recorded_to_attest_and_a_resign_adds_the_rest():
    s3 = FakeS3(version())
    written: dict[str, str] = {}
    checksums.update(["x"], s3=s3, lists=written)
    assert written == {V + "SHA256SUMS": sha(s3.objects[V + "SHA256SUMS"][0])}
    again: dict[str, str] = {}
    assert checksums.update(["x"], s3=s3, lists=again) == (0, [], []) and again == {}
    checksums.update(["x"], s3=s3, lists=again, every=True)
    assert again == written


def test_subjects_are_split_into_attestations_of_at_most_1024(tmp_path):
    lists = {f"d/x/v/{i:04d}/SHA256SUMS": sha(str(i).encode()) for i in range(1500)}
    parts = checksums.write_subjects(lists, tmp_path / "lists")
    assert [p.name for p in parts] == ["1.sha256", "2.sha256"]
    first, second = (checksums.parse(p.read_text()) for p in parts)
    assert len(first) == 1024 and len(second) == 476
    assert first | second == lists
    assert checksums.write_subjects({}, tmp_path / "none") == []


def test_the_command_warns_of_a_file_written_after_the_list(tmp_path, monkeypatch, capsys):
    from publicdata import __main__, r2

    s3 = FakeS3(version())
    checksums.update(["x"], s3=s3)
    s3.objects[V + "data.xlsx"] = (b"PK", T0 + dt.timedelta(days=1), sha(b"PK"))
    monkeypatch.setattr(r2, "client", lambda: s3)
    (tmp_path / "d" / "x" / "v").mkdir(parents=True)
    assert __main__.main(["checksums", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"::warning::{V}data.xlsx was written after {V}SHA256SUMS, outside a replace." in out
    assert "0 SHA256SUMS written" in out
