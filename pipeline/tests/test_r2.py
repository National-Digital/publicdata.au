import hashlib

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
