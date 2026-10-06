"""Move bytes between local trees and R2 over the S3 API.

Most objects are immutable once written, so an existing key is skipped. A key the caller marks
mutable is uploaded again when its bytes differ: by the listing's ETag, which is the MD5 of a
single-part upload, or else by the SHA-256 stored with the object. The S3 access key
is the Cloudflare API token id and the secret is the SHA-256 of the token, which is how R2 maps
account tokens onto S3 credentials.

A dated text file, and a dated SQLite file, is stored gzipped (see stored_gzipped): marked with
Content-Encoding gzip, with the size and SHA-256 of its decoded bytes as metadata. Parquet,
DuckDB and the other binary formats stay as written, since readers ask them for byte ranges.
"""

from __future__ import annotations

import gzip
import hashlib
import mimetypes
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

TYPES = {
    ".parquet": "application/vnd.apache.parquet",
    ".sqlite": "application/vnd.sqlite3",
    ".ndjson": "application/x-ndjson",
    ".geojson": "application/geo+json",
    ".zst": "application/zstd",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".arrow": "application/vnd.apache.arrow.file",
    ".gpkg": "application/geopackage+sqlite3",
    ".pmtiles": "application/vnd.pmtiles",
    ".gz": "application/gzip",
    ".sql": "application/sql; charset=utf-8",
}

VERSIONED = re.compile(r"(^|/)v/\d{4}-\d{2}-\d{2}/")


def client():
    import boto3
    from botocore.config import Config

    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    token_id = os.environ.get("CLOUDFLARE_API_TOKEN_ID")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not token_id or not account:
        sys.exit(
            "CLOUDFLARE_API_TOKEN, CLOUDFLARE_API_TOKEN_ID and CLOUDFLARE_ACCOUNT_ID are needed for R2"
        )
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=token_id,
        aws_secret_access_key=hashlib.sha256(token.encode()).hexdigest(),
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
    )


def _etags(s3, bucket: str, prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out.update((o["Key"], o.get("ETag", "").strip('"')) for o in page.get("Contents", []))
    return out


def _md5(p: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Stored gzipped. SQLite is read whole by its clients, so it goes in; the binary formats that
# readers range over do not, and the publisher's file is served from the raw store as fetched.
GZIP_SUFFIXES = (".csv", ".ndjson", ".json", ".geojson", ".sql", ".sqlite", ".md", ".txt")
GZIP_MIN = 1024
GZIP_LEVEL = 6
GZIP_MAGIC = b"\x1f\x8b"


def stored_gzipped(key: str, size: int) -> bool:
    name = key.rsplit("/", 1)[-1]
    return (
        dated_file(key)
        and name.endswith(GZIP_SUFFIXES)
        and not name.startswith("source.")
        and size >= GZIP_MIN
    )


def gzip_to(src: Path, dest: Path) -> None:
    """No name and no mtime, so the bytes are a function of the input; at the level the csv.gz
    writer uses, a CSV gzips to its data.csv.gz byte for byte."""
    with src.open("rb") as f, dest.open("wb") as raw:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=GZIP_LEVEL
        ) as gz:
            shutil.copyfileobj(f, gz, 1 << 20)


def gunzip_to(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as gz, dest.open("wb") as out:
        shutil.copyfileobj(gz, out, 1 << 20)


def versioned(key: str) -> bool:
    return bool(VERSIONED.search(key))


def dated_file(key: str) -> bool:
    """A dated version's file, which never changes. Its page says whether it is the newest."""
    return versioned(key) and not key.endswith(("/index.html", "/index.md"))


def push(
    root: Path,
    bucket: str,
    prefix: str = "",
    replace: tuple[str, ...] = (),
    immutable: Callable[[str], bool] = lambda key: True,
    expect: list[str] = (),
    include: Callable[[str], bool] = lambda key: True,
) -> int:
    """Upload every file under root that include accepts. An existing immutable key is skipped unless it starts
    with one of the replace prefixes, which name the versions whose serialisation was rebuilt on
    purpose; the version notes for such a rebuild are committed separately. Every key in expect,
    which a cached build left out, must already be in the bucket, or nothing is uploaded."""
    s3 = client()
    files = sorted(x for x in root.rglob("*") if x.is_file())
    files = [p for p in files if include(prefix + str(p.relative_to(root)).replace(os.sep, "/"))]
    keys = [prefix + str(p.relative_to(root)).replace(os.sep, "/") for p in files]
    # One listing per dataset directory, which is a page per thousand keys.
    scopes = sorted(
        {
            "/".join(k.split("/")[:2]) + "/" if k.startswith("d/") else prefix
            for k in [*keys, *expect]
        }
    )
    etags: dict[str, str] = {}
    for sc in scopes:
        etags |= _etags(s3, bucket, sc)
    existing = set(etags)
    check_expected(expect, existing, replace)
    n = 0
    tmp = Path(tempfile.mkdtemp(prefix="dist-push-"))
    gzipped: dict[str, str] = {}
    try:
        for p, key in zip(files, keys, strict=True):
            n += _push_one(s3, bucket, p, key, existing, etags, replace, immutable, tmp, gzipped)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return n


def _push_one(s3, bucket, p, key, existing, etags, replace, immutable, tmp, gzipped) -> int:
    forced = key.startswith(replace or ("\0",))
    if key in existing and not forced and immutable(key):
        return 0
    # The stored CSV this push just gzipped is these bytes already, and the function serves
    # data.csv.gz from it.
    if key.endswith(".csv.gz") and gzipped.get(key[:-3]) == _sha256(p):
        print(f"alias {bucket}/{key} (served from {key[:-3]})")
        return 0
    digest = None
    if key in existing and not forced:
        # A single-part upload's ETag is the MD5 of its bytes, so the listing settles most
        # mutable keys without a HEAD each; a multipart one is checked by its stored SHA-256.
        tag = etags[key]
        if len(tag) == 32 and "-" not in tag:
            if tag == _md5(p):
                return 0
        else:
            digest = _sha256(p)
            meta = s3.head_object(Bucket=bucket, Key=key).get("Metadata", {})
            if meta.get("sha256") == digest:
                return 0
    digest = digest or _sha256(p)
    ctype = TYPES.get(p.suffix) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    size = p.stat().st_size
    if stored_gzipped(key, size):
        gz = tmp / "body.gz"
        gzip_to(p, gz)
        s3.upload_file(str(gz), bucket, key, ExtraArgs=gzip_args(ctype, digest, size))
        gzipped[key] = _sha256(gz)
        print(f"put {bucket}/{key} ({size} bytes, {gz.stat().st_size} gzipped)")
        gz.unlink()
        return 1
    s3.upload_file(
        str(p), bucket, key, ExtraArgs={"ContentType": ctype, "Metadata": {"sha256": digest}}
    )
    print(f"put {bucket}/{key} ({size} bytes)")
    return 1


def gzip_args(ctype: str, sha256: str, size: int) -> dict:
    return {
        "ContentType": ctype,
        "ContentEncoding": "gzip",
        "Metadata": {"sha256": sha256, "size": str(size)},
    }


def check_expected(expect, existing: set[str], replace: tuple[str, ...] = ()) -> None:
    rebuilt = [k for k in expect if replace and k.startswith(replace)]
    if rebuilt:
        sys.exit(
            f"{len(rebuilt)} file(s) under --replace came from the build cache, e.g. {rebuilt[0]}; "
            "build without the cache to replace them"
        )
    # A data.csv.gz is not stored apart from the gzipped data.csv it is served from.
    missing = sorted(
        k for k in set(expect) - existing if not (k.endswith(".csv.gz") and k[:-3] in existing)
    )
    if missing:
        sys.exit(
            f"{len(missing)} file(s) the cached build left out are not in R2, e.g. "
            + ", ".join(missing[:5])
            + "; build without the cache so they are written again"
        )


def pull_store(
    store: Path,
    bucket: str = "publicdata-raw",
    only: tuple[str, ...] = (),
    skip: set[tuple[str, str]] = frozenset(),
) -> int:
    """Fetch every source file a committed manifest names and is missing locally, except the
    versions in skip, which the build takes from its cache. Only the newest catalogue snapshot is
    needed to build."""
    from . import store as st
    from .catalogue import SLUG as CATALOGUE

    s3 = client()
    n = 0
    paths = sorted(store.glob("*/*/manifest.json"))
    old_catalogues = [p for p in paths if p.parts[-3] == CATALOGUE][:-1]
    for mp in (p for p in paths if p not in old_catalogues and (not only or p.parts[-3] in only)):
        if (mp.parts[-3], mp.parts[-2]) in skip:
            continue
        m = st.Manifest.read(mp)
        dest = st.source_path(store, m)
        if dest.exists() and st.sha256_file(dest) == m.sha256:
            continue
        key = f"{m.dataset}/{m.version}/source.{m.ext}"
        s3.download_file(bucket, key, str(dest))
        st.verify(store, m)
        n += 1
        print(f"got {bucket}/{key}")
    return n


def pull_fonts(dest: Path, bucket: str = "publicdata-raw") -> int:
    """Fetch the brand fonts the repository leaves out, checked against their pinned hashes."""
    from .brand import FONT_KEY, FONT_SHA256

    s3 = client()
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for name, sha in FONT_SHA256.items():
        p = dest / name
        if p.is_file() and _sha256(p) == sha:
            continue
        s3.download_file(bucket, FONT_KEY + name, str(p))
        if _sha256(p) != sha:
            p.unlink()
            sys.exit(f"{bucket}/{FONT_KEY}{name} does not match its pinned SHA-256")
        n += 1
    return n


def downloader(bucket: str) -> Callable[[str, Path], bool]:
    """download(key, path) for a bucket, False when the key is not there. One client serves every
    thread."""
    from botocore.exceptions import ClientError

    s3 = client()

    def download(key: str, dest: Path) -> bool:
        try:
            s3.download_file(bucket, key, str(dest))
        except ClientError as e:
            dest.unlink(missing_ok=True)
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise
        decode_stored(s3, bucket, key, dest)
        return True

    return download


def decode_stored(s3, bucket: str, key: str, dest: Path) -> None:
    """Turn a downloaded object stored gzipped back into the file it was, checked against the
    SHA-256 stored with it. Only bytes that open like gzip cost a HEAD."""
    with dest.open("rb") as f:
        if f.read(2) != GZIP_MAGIC:
            return
    head = s3.head_object(Bucket=bucket, Key=key)
    if head.get("ContentEncoding") != "gzip":
        return
    plain = dest.with_name(dest.name + ".plain")
    gunzip_to(dest, plain)
    want = head.get("Metadata", {}).get("sha256")
    if want and _sha256(plain) != want:
        plain.unlink()
        dest.unlink()
        sys.exit(f"{bucket}/{key} does not decode to the SHA-256 stored with it")
    plain.replace(dest)


CACHE_BUCKET = "publicdata-raw"
CACHE_PREFIX = "_build/"
# When each entry the last pruning push found unused was first found so; it is deleted only once
# it has stayed unused this long, so a preview that listed it beforehand still finds it whole.
UNUSED = CACHE_PREFIX + ".unused.json"
GRACE_HOURS = 24
WORKERS = 16


def _missing(e) -> bool:
    return e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey")


def cache_pull(root: Path, meta_only: bool = False, entries: set[str] | None = None) -> int:
    """Copy the build cache down from R2 into root, each entry's meta.json last, so an entry whose
    files did not all arrive is never taken for whole; an entry deleted while it was copied is
    left out. With meta_only, the records alone, which is all a plan needs. With entries, only
    those."""
    from concurrent.futures import ThreadPoolExecutor

    from botocore.exceptions import ClientError

    s3 = client()
    keys = [
        k
        for k in _etags(s3, CACHE_BUCKET, CACHE_PREFIX)
        if not k.endswith("/") and not k[len(CACHE_PREFIX) :].startswith(".")
    ]
    if entries is not None:
        keys = [k for k in keys if k.split("/")[1] in entries]
    if meta_only:
        keys = [k for k in keys if k.endswith("/meta.json")]
    files = [k for k in keys if not k.endswith("/meta.json")]
    metas = [k for k in keys if k.endswith("/meta.json")]
    gone: set[str] = set()

    def get(key: str) -> bool:
        entry = key.split("/")[1]
        if entry in gone:
            return False
        dest = root / key[len(CACHE_PREFIX) :]
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            s3.download_file(CACHE_BUCKET, key, str(dest))
        except ClientError as e:
            if not _missing(e):
                raise
            gone.add(entry)
            dest.unlink(missing_ok=True)
            return False
        return True

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(get, files))
        got = sum(pool.map(get, [k for k in metas if k.split("/")[1] not in gone]))
    return got


def _delete(s3, keys: list[str]) -> None:
    for i in range(0, len(keys), 1000):
        r = s3.delete_objects(
            Bucket=CACHE_BUCKET,
            Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]], "Quiet": True},
        )
        if r.get("Errors"):
            e = r["Errors"]
            sys.exit(
                f"cache push: {len(e)} key(s) were not deleted, e.g. "
                + ", ".join(f"{x.get('Key')} ({x.get('Code')})" for x in e[:5])
            )


def _unused(s3) -> dict[str, str]:
    import json

    from botocore.exceptions import ClientError

    try:
        body = s3.get_object(Bucket=CACHE_BUCKET, Key=UNUSED)["Body"].read()
    except ClientError as e:
        if _missing(e):
            return {}
        raise
    return json.loads(body)


def cache_push(root: Path, prune: bool = False, now=None) -> tuple[int, int]:
    """Upload the entries under root that R2 lacks or holds in another form, files before each
    meta.json. With prune, R2's entries that root no longer holds are noted as unused, and those
    a push noted GRACE_HOURS or more ago are deleted, meta.json first; so run it only from a build
    that pruned root to the entries the store can use. Returns (uploaded, deleted)."""
    import json
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime, timedelta

    s3 = client()
    remote = {k: v for k, v in _etags(s3, CACHE_BUCKET, CACHE_PREFIX).items() if k != UNUSED}
    local = {
        CACHE_PREFIX + p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.relative_to(root).parts[0].startswith(".")
    }

    def changed(key: str) -> bool:
        tag = remote.get(key)
        if tag is None:
            return True
        if len(tag) == 32 and "-" not in tag:
            return tag != _md5(local[key])
        meta = s3.head_object(Bucket=CACHE_BUCKET, Key=key).get("Metadata", {})
        return meta.get("sha256") != _sha256(local[key])

    def put(key: str) -> None:
        p = local[key]
        ctype = TYPES.get(p.suffix) or "application/octet-stream"
        s3.upload_file(
            str(p),
            CACHE_BUCKET,
            key,
            ExtraArgs={"ContentType": ctype, "Metadata": {"sha256": _sha256(p)}},
        )

    with ThreadPoolExecutor(WORKERS) as pool:
        todo = [k for k, c in zip(local, pool.map(changed, local), strict=True) if c]
        list(pool.map(put, [k for k in todo if not k.endswith("/meta.json")]))
        list(pool.map(put, [k for k in todo if k.endswith("/meta.json")]))
    if not prune:
        return len(todo), 0
    now = now or datetime.now(UTC)
    held = {k.split("/")[1] for k in local}
    seen = _unused(s3)
    unused = {
        e: seen.get(e, now.isoformat()) for e in sorted({k.split("/")[1] for k in remote} - held)
    }
    due = {
        e
        for e, t in unused.items()
        if now - datetime.fromisoformat(t) >= timedelta(hours=GRACE_HOURS)
    }
    gone = sorted(k for k in remote if k.split("/")[1] in due)
    # Without its meta.json an entry is a miss, so one that is half deleted is never read as whole.
    _delete(s3, [k for k in gone if k.endswith("/meta.json")])
    _delete(s3, [k for k in gone if not k.endswith("/meta.json")])
    s3.put_object(
        Bucket=CACHE_BUCKET,
        Key=UNUSED,
        Body=json.dumps({e: t for e, t in unused.items() if e not in due}, indent=0).encode(),
        ContentType="application/json",
    )
    return len(todo), len(due)


def source_keys(root: Path) -> dict[str, str]:
    """The raw store key of each publisher's file the versions under root list, by the URL path
    the /d/ function serves it at."""
    import json

    from . import store

    out = {}
    for man in sorted(root.glob("d/*/v/*/manifest.json")):
        m = json.loads(man.read_text(encoding="utf-8"))
        if m.get("source_withheld"):
            continue
        slug, version = man.parts[-4], man.parts[-2]
        ext = store.ext_of(m.get("filename", ""))
        out[f"d/{slug}/v/{version}/source.{ext}"] = f"{slug}/{version}/source.{ext}"
    return out


def check_sources(roots: list[Path], bucket: str = "publicdata-raw") -> int:
    """Every version under the roots has its publisher's file in the raw store, which is where
    the site serves it from. Returns how many were checked."""
    s3 = client()
    want = {}
    for r in roots:
        want |= source_keys(r)
    have = set(_etags(s3, bucket, "")) if want else set()
    missing = sorted(k for k, v in want.items() if v not in have)
    if missing:
        sys.exit(
            f"{len(missing)} version(s) list a publisher's file the raw store does not hold, e.g. "
            + ", ".join(missing[:5])
        )
    return len(want)


def _listing(s3, bucket: str, prefix: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out.update((o["Key"], o.get("Size", 0)) for o in page.get("Contents", []))
    return out


def restore_gzip(
    bucket: str = "publicdata-dist",
    prefix: str = "d/",
    apply: bool = False,
    workers: int = 4,
) -> dict:
    """Store the dated text files that went up before gzip at rest gzipped, in place at the same
    key. Each object's decoded bytes are checked against the SHA-256 it was stored with before it
    is rewritten, and read back and checked again after; an object that fails the second check is
    put back as it was. An object already marked gzip is skipped, so a run that stops can be run
    again. Without apply it only counts. Returns the totals it printed."""
    from concurrent.futures import ThreadPoolExecutor

    s3 = client()
    todo = sorted((k, n) for k, n in _listing(s3, bucket, prefix).items() if stored_gzipped(k, n))
    tmp = Path(tempfile.mkdtemp(prefix="restore-gzip-"))
    totals = {"objects": 0, "done": 0, "already": 0, "failed": 0, "before": 0, "after": 0}
    by_ext: dict[str, list[int]] = {}

    def one(item: tuple[str, int]) -> tuple[str, int, int]:
        key, size = item
        head = s3.head_object(Bucket=bucket, Key=key)
        if head.get("ContentEncoding") == "gzip":
            return "already", size, size
        if not apply:
            return "candidate", size, size
        work = Path(tempfile.mkdtemp(dir=tmp))
        try:
            return _restore_one(s3, bucket, key, head, work)
        except Exception as e:  # one bad object must not stop the run
            print(f"restore: {key} FAILED {e}", file=sys.stderr)
            return "failed", size, size
        finally:
            shutil.rmtree(work, ignore_errors=True)

    try:
        with ThreadPoolExecutor(workers) as pool:
            for (key, _), (state, before, after) in zip(todo, pool.map(one, todo), strict=True):
                totals["objects"] += 1
                ext = key.rsplit(".", 1)[-1]
                if state == "already":
                    totals["already"] += 1
                    continue
                if state == "failed":
                    totals["failed"] += 1
                    continue
                if state == "done":
                    totals["done"] += 1
                    print(f"gzip {bucket}/{key}: {before} -> {after} bytes")
                totals["before"] += before
                totals["after"] += after
                e = by_ext.setdefault(ext, [0, 0, 0])
                e[0] += 1
                e[1] += before
                e[2] += after
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for ext, (n, before, after) in sorted(by_ext.items()):
        line = f"restore: .{ext} {n} object(s), {before} bytes"
        print(line + (f" -> {after}, {before - after} saved" if apply else ""))
    left = totals["objects"] - totals["already"]
    if apply:
        print(
            f"restore: {totals['done']} object(s) gzipped, {totals['before'] - totals['after']} "
            f"bytes saved; {totals['already']} were already, {totals['failed']} failed"
        )
    else:
        print(
            f"restore: dry run, {left} object(s) of {totals['before']} bytes would be gzipped; "
            f"{totals['already']} already are. Pass --apply to rewrite them."
        )
    totals["saved"] = totals["before"] - totals["after"]
    return totals


def _restore_one(s3, bucket: str, key: str, head: dict, work: Path) -> tuple[str, int, int]:
    plain, gz, back = work / "plain", work / "body.gz", work / "back.gz"
    s3.download_file(bucket, key, str(plain))
    digest, size = _sha256(plain), plain.stat().st_size
    want = head.get("Metadata", {}).get("sha256")
    tag = head.get("ETag", "").strip('"')
    if want and want != digest:
        raise ValueError(f"read back as {digest}, stored with {want}")
    if not want and len(tag) == 32 and "-" not in tag and tag != _md5(plain):
        raise ValueError("read back with an MD5 that is not its ETag")
    if size != head.get("ContentLength", size):
        raise ValueError(f"read back {size} bytes of {head['ContentLength']}")
    gzip_to(plain, gz)
    check = work / "check"
    gunzip_to(gz, check)
    if _sha256(check) != digest:
        raise ValueError("does not survive gzip")
    ctype = head.get("ContentType") or TYPES.get(Path(key).suffix) or "application/octet-stream"
    meta = {**head.get("Metadata", {}), "sha256": digest, "size": str(size)}
    s3.upload_file(
        str(gz), bucket, key, ExtraArgs={**gzip_args(ctype, digest, size), "Metadata": meta}
    )
    try:
        s3.download_file(bucket, key, str(back))
        if s3.head_object(Bucket=bucket, Key=key).get("ContentEncoding") != "gzip":
            raise ValueError("is not marked gzip after the rewrite")
        if back.read_bytes()[:2] == GZIP_MAGIC:
            gunzip_to(back, check)
        else:
            back.replace(check)
        if _sha256(check) != digest:
            raise ValueError("does not decode to its bytes after the rewrite")
    except Exception:
        args = {"ContentType": ctype, "Metadata": head.get("Metadata", {})}
        s3.upload_file(str(plain), bucket, key, ExtraArgs=args)
        raise
    return "done", size, gz.stat().st_size
