"""Move bytes between local trees and R2 over the S3 API.

Most objects are immutable once written, so an existing key is skipped. A key the caller marks
mutable is uploaded again when its bytes differ: by the listing's ETag, which is the MD5 of a
single-part upload, or else by the SHA-256 stored with the object. The S3 access key
is the Cloudflare API token id and the secret is the SHA-256 of the token, which is how R2 maps
account tokens onto S3 credentials.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import re
import sys
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


def versioned(key: str) -> bool:
    return bool(VERSIONED.search(key))


def dated_file(key: str) -> bool:
    """A dated version's file, which never changes, or a version's query copy, which push
    rewrites only when its layout changes. A dated page says whether it is the newest, so it may."""
    if key.startswith("_q/"):
        return True
    return versioned(key) and not key.endswith(("/index.html", "/index.md"))


def _scope(key: str, prefix: str) -> str:
    """The listing a key is looked up in: its dataset's directory, or the query copies."""
    if key.startswith("d/"):
        return "/".join(key.split("/")[:2]) + "/"
    return "_q/" if key.startswith("_q/") else prefix


class _Ranged(io.RawIOBase):
    """An object read by byte range, so a Parquet footer is read without the whole file."""

    def __init__(self, s3, bucket: str, key: str):
        self.s3, self.bucket, self.key, self.pos = s3, bucket, key, 0
        self.size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self.pos = (0, self.pos, self.size)[whence] + offset
        return self.pos

    def readinto(self, b) -> int:
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        rng = f"bytes={self.pos}-{self.pos + n - 1}"
        got = self.s3.get_object(Bucket=self.bucket, Key=self.key, Range=rng)["Body"].read()
        b[: len(got)] = got
        self.pos += len(got)
        return len(got)


def _follows(s3, bucket: str, key: str, lay: dict, etags: dict[str, str]) -> bool | None:
    """Whether the query copy R2 holds at key follows layout lay: True or False from the record
    beside it, which the listing settles, or, for a copy written before records were kept, from
    its footer, None meaning it follows but has no record yet."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from .serialise.profile import follows, layout_body, layout_key

    mark = layout_key(key)
    if mark in etags:
        return etags[mark] == hashlib.md5(layout_body(lay), usedforsecurity=False).hexdigest()
    try:
        with _Ranged(s3, bucket, key) as f:
            return None if follows(pq.read_metadata(f), lay) else False
    except pa.ArrowInvalid:
        return False


def _record(s3, bucket: str, key: str, lay: dict) -> None:
    from .serialise.profile import layout_body, layout_key

    s3.put_object(
        Bucket=bucket, Key=layout_key(key), Body=layout_body(lay), ContentType=TYPES[".json"]
    )


def push(
    root: Path,
    bucket: str,
    prefix: str = "",
    replace: tuple[str, ...] = (),
    immutable: Callable[[str], bool] = lambda key: True,
    expect: list[str] = (),
    include: Callable[[str], bool] = lambda key: True,
    layouts: dict[str, dict] | None = None,
) -> int:
    """Upload every file under root that include accepts. An existing immutable key is skipped unless it starts
    with one of the replace prefixes, which name the versions whose serialisation was rebuilt on
    purpose; the version notes for such a rebuild are committed separately. Every key in expect,
    which a cached build left out, must already be in the bucket, or nothing is uploaded.

    With layouts, each dataset's layout (`profile.layout`), a query copy is uploaded again only
    when the copy R2 holds follows another, and every query copy in expect must follow its
    dataset's."""
    from concurrent.futures import ThreadPoolExecutor

    s3 = client()
    files = sorted(x for x in root.rglob("*") if x.is_file())
    files = [p for p in files if include(prefix + str(p.relative_to(root)).replace(os.sep, "/"))]
    keys = [prefix + str(p.relative_to(root)).replace(os.sep, "/") for p in files]
    # One listing per dataset directory and one of the query copies, a page per thousand keys.
    scopes = sorted({_scope(k, prefix) for k in [*keys, *expect]})
    etags: dict[str, str] = {}
    for sc in scopes:
        etags |= _etags(s3, bucket, sc)
    existing = set(etags)
    check_expected(expect, existing, replace)

    def layout_of(key: str) -> dict | None:
        if layouts is None or not key.startswith("_q/"):
            return None
        if key.split("/")[1] not in layouts:
            sys.exit(f"{key} is a query copy of a dataset the register does not hold")
        return layouts[key.split("/")[1]]

    wrong = [k for p, k in zip(files, keys, strict=True) if not _built_to(p, layout_of(k))]
    if wrong:
        sys.exit(
            f"{len(wrong)} query copies in the tree do not follow their entry's layout, e.g. "
            + ", ".join(wrong[:5])
        )
    queries = [(k, lay) for k in expect if (lay := layout_of(k)) is not None]
    with ThreadPoolExecutor(WORKERS) as pool:
        kept = list(pool.map(lambda q: _follows(s3, bucket, *q, etags), queries))
    stale = [k for (k, _), ok in zip(queries, kept, strict=True) if ok is False]
    if stale:
        sys.exit(
            f"{len(stale)} query copies the cached build left out follow another layout in R2, e.g. "
            + ", ".join(stale[:5])
            + "; build without the cache so they are written again"
        )
    with ThreadPoolExecutor(WORKERS) as pool:
        unrecorded = [q for q, ok in zip(queries, kept, strict=True) if ok is None]
        list(pool.map(lambda q: _record(s3, bucket, *q), unrecorded))
    n = 0
    for p, key in zip(files, keys, strict=True):
        forced = key.startswith(replace or ("\0",))
        if (lay := layout_of(key)) is not None:
            n += _push_query(s3, bucket, p, key, lay, etags)
            continue
        if key in existing and not forced and immutable(key):
            continue
        digest = None
        if key in existing and not forced:
            # A single-part upload's ETag is the MD5 of its bytes, so the listing settles most
            # mutable keys without a HEAD each; a multipart one is checked by its stored SHA-256.
            tag = etags[key]
            if len(tag) == 32 and "-" not in tag:
                if tag == _md5(p):
                    continue
            else:
                digest = _sha256(p)
                meta = s3.head_object(Bucket=bucket, Key=key).get("Metadata", {})
                if meta.get("sha256") == digest:
                    continue
        digest = digest or _sha256(p)
        ctype = TYPES.get(p.suffix) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        s3.upload_file(
            str(p), bucket, key, ExtraArgs={"ContentType": ctype, "Metadata": {"sha256": digest}}
        )
        n += 1
        print(f"put {bucket}/{key} ({p.stat().st_size} bytes)")
    return n


def _built_to(p: Path, lay: dict | None) -> bool:
    """Whether a local query copy follows lay, so the record written beside it is true."""
    if lay is None:
        return True
    import pyarrow.parquet as pq

    from .serialise.profile import follows

    return follows(pq.read_metadata(p), lay)


def _push_query(s3, bucket: str, p: Path, key: str, lay: dict, etags: dict[str, str]) -> int:
    """Upload a query copy unless R2's follows lay. The record is written after the upload, so it
    never names a layout the copy in R2 does not follow."""
    if key in etags and (ok := _follows(s3, bucket, key, lay, etags)) is not False:
        if ok is None:
            _record(s3, bucket, key, lay)
        return 0
    s3.upload_file(
        str(p),
        bucket,
        key,
        ExtraArgs={"ContentType": TYPES[".parquet"], "Metadata": {"sha256": _sha256(p)}},
    )
    _record(s3, bucket, key, lay)
    print(f"put {bucket}/{key} ({p.stat().st_size} bytes)")
    return 1


def check_expected(expect, existing: set[str], replace: tuple[str, ...] = ()) -> None:
    rebuilt = [k for k in expect if replace and k.startswith(replace)]
    if rebuilt:
        sys.exit(
            f"{len(rebuilt)} file(s) under --replace came from the build cache, e.g. {rebuilt[0]}; "
            "build without the cache to replace them"
        )
    missing = sorted(set(expect) - existing)
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
    newest: bool = False,
) -> int:
    """Fetch every source file a committed manifest names and is missing locally, except the
    versions in skip, which the build takes from its cache. Only the newest catalogue snapshot is
    needed to build, and of a rolling source's fetches that are no snapshot, only the newest,
    which latest/ serves. With newest, only each dataset's newest fetch is fetched."""
    from . import store as st
    from .catalogue import SLUG as CATALOGUE

    s3 = client()
    n = 0
    paths = sorted(store.glob("*/*/manifest.json"))
    last = {p.parts[-3]: p for p in paths}
    old_catalogues = [p for p in paths if p.parts[-3] == CATALOGUE][:-1]
    for mp in (p for p in paths if p not in old_catalogues and (not only or p.parts[-3] in only)):
        if (mp.parts[-3], mp.parts[-2]) in skip or (newest and last[mp.parts[-3]] != mp):
            continue
        m = st.Manifest.read(mp)
        if not m.snapshot and last[mp.parts[-3]] != mp:
            continue
        want = [(st.source_path(store, m), m.sha256)]
        if m.history:
            want.append((st.history_path(store, m), m.history["sha256"]))
        for dest, sha in want:
            if dest.exists() and st.sha256_file(dest) == sha:
                continue
            key = f"{m.dataset}/{m.version}/{dest.name}"
            try:
                s3.download_file(bucket, key, str(dest))
                if st.sha256_file(dest) != sha:
                    raise ValueError(f"{dest}: sha256 does not match the manifest")
            except Exception as e:  # noqa: BLE001 - the fetch reports the dataset it holds back
                if not newest:
                    raise
                dest.unlink(missing_ok=True)
                print(f"WARNING {m.dataset}: {key} not pulled ({e}); its fetch will be held back")
                continue
            n += 1
            print(f"got {bucket}/{key}")
    # A feed's newest read, beside its folders and rewritten by every fetch, so always read again.
    # A feed without one carries last_seen to its newest fetch alone.
    if not newest:
        for slug in sorted({p.parts[-3] for p in paths if not only or p.parts[-3] in only}):
            if not st.Manifest.read(last[slug]).history:
                continue
            dest = st.read_path(store, slug)
            try:
                s3.download_file(bucket, f"{slug}/read.json", str(dest))
                n += 1
            except Exception:  # noqa: BLE001 - absent until the feed's first quiet read
                dest.unlink(missing_ok=True)
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
        return True

    return download


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
    from concurrent.futures import ThreadPoolExecutor

    slugs = sorted({v.split("/")[0] + "/" for v in want.values()})
    with ThreadPoolExecutor(WORKERS) as pool:
        have = {k for got in pool.map(lambda sc: _etags(s3, bucket, sc), slugs) for k in got}
    missing = sorted(k for k, v in want.items() if v not in have)
    if missing:
        sys.exit(
            f"{len(missing)} version(s) list a publisher's file the raw store does not hold, e.g. "
            + ", ".join(missing[:5])
        )
    return len(want)
