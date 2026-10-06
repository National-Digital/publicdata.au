"""SHA256SUMS for every dated version in R2, written after the deploy pushes the version files.

Nothing the build imports reads this module, so adding or changing it keys no version. Each line
names the file as the site saves it (`site.download_name`), so `sha256sum -c --ignore-missing
SHA256SUMS` checks the files a browser or `curl -OJ` downloaded. The hashes come from the SHA-256
that `r2.push` stores with every object, and an object without one is read and hashed only when
the caller allows it. A version's list is written again when R2 holds a file newer than it, which
is how a format added to a cached version reaches the list, or when its prefix is replaced.
"""

from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BUCKET = "publicdata-dist"
NAME = "SHA256SUMS"
CONTENT_TYPE = "text/plain; charset=utf-8"
PAGES = ("index.html", "index.md")
KEY = re.compile(r"^d/([a-z0-9-]+)/v/(\d{4}-\d{2}-\d{2})/(.+)$")
HEX = re.compile(r"^[0-9a-f]{64}$")
WORKERS = 16


def render(sums: dict[str, str]) -> str:
    """The sha256sum text: one line per file, sorted by name, two spaces between."""
    return "".join(f"{h}  {n}\n" for n, h in sorted(sums.items()))


def parse(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        h, sep, n = line.partition("  ")
        if sep and HEX.match(h) and n:
            out[n] = h
    return out


def slugs_in(roots: Iterable[Path]) -> list[str]:
    """The datasets with dated versions in the built trees."""
    found = set()
    for root in roots:
        d = Path(root) / "d"
        if d.is_dir():
            found |= {p.parent.name for p in d.glob("*/v") if p.is_dir()}
    return sorted(found)


def slugs_in_bucket(s3, bucket: str = BUCKET) -> list[str]:
    found = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix="d/", Delimiter="/"
    ):
        found += [c["Prefix"].split("/")[1] for c in page.get("CommonPrefixes", [])]
    return sorted(found)


def _versions(s3, bucket: str, slug: str) -> dict[str, dict[str, dict]]:
    """Each version's objects under d/<slug>/v/, by path within the version."""
    out: dict[str, dict[str, dict]] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"d/{slug}/v/"):
        for o in page.get("Contents", []):
            m = KEY.match(o["Key"])
            if m and m.group(1) == slug:
                out.setdefault(m.group(2), {})[m.group(3)] = o
    return out


def stale(objects: dict[str, dict], replaced: bool) -> bool:
    files = [o for rel, o in objects.items() if rel != NAME and rel not in PAGES]
    if not files:
        return False
    sums = objects.get(NAME)
    return replaced or sums is None or any(o["LastModified"] > sums["LastModified"] for o in files)


def _stored_sha256(s3, bucket: str, key: str) -> str | None:
    h = s3.head_object(Bucket=bucket, Key=key).get("Metadata", {}).get("sha256", "")
    return h if HEX.match(h) else None


def _read_sha256(s3, bucket: str, key: str) -> str:
    h = hashlib.sha256()
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    for chunk in iter(lambda: body.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def version_sums(
    s3,
    bucket: str,
    slug: str,
    version: str,
    objects: dict[str, dict],
    replaced: bool = False,
    download: bool = False,
    pool: ThreadPoolExecutor | None = None,
) -> tuple[dict[str, str], list[str]]:
    """The version's sums by download name, and the keys whose hash is unknown. A line in the
    current list is kept for a file no newer than the list, unless the version was replaced."""
    from .site import download_name

    prefix = f"d/{slug}/v/{version}/"
    old: dict[str, str] = {}
    sums_obj = objects.get(NAME)
    if sums_obj is not None and not replaced:
        old = parse(s3.get_object(Bucket=bucket, Key=prefix + NAME)["Body"].read().decode())
    sums: dict[str, str] = {}
    todo: list[tuple[str, str]] = []
    for rel, o in sorted(objects.items()):
        if rel == NAME or rel in PAGES:
            continue
        name = download_name(slug, version, rel)
        if name in old and o["LastModified"] <= sums_obj["LastModified"]:
            sums[name] = old[name]
        else:
            todo.append((name, prefix + rel))

    def one(item: tuple[str, str]) -> tuple[str, str, str | None]:
        name, key = item
        h = _stored_sha256(s3, bucket, key)
        if h is None and download:
            h = _read_sha256(s3, bucket, key)
        return name, key, h

    unknown = []
    for name, key, h in pool.map(one, todo) if pool else map(one, todo):
        if h is None:
            unknown.append(key)
        else:
            sums[name] = h
    return sums, unknown


def update(
    slugs: Iterable[str],
    bucket: str = BUCKET,
    replace: tuple[str, ...] = (),
    download: bool = False,
    s3=None,
    workers: int = WORKERS,
) -> tuple[int, list[str]]:
    """Writes SHA256SUMS for every version of these datasets whose list is missing or stale.
    Returns how many were written and the versions left without one because a file's hash is
    unknown."""
    if s3 is None:
        from .r2 import client

        s3 = client()
    written, held = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for slug in slugs:
            for version, objects in sorted(_versions(s3, bucket, slug).items()):
                prefix = f"d/{slug}/v/{version}/"
                replaced = prefix.startswith(replace) if replace else False
                if not stale(objects, replaced):
                    continue
                sums, unknown = version_sums(
                    s3, bucket, slug, version, objects, replaced, download, pool
                )
                if unknown:
                    held.append(prefix)
                    print(
                        f"checksums: {prefix} has {len(unknown)} file(s) with no stored SHA-256, "
                        f"e.g. {unknown[0]}",
                        file=sys.stderr,
                    )
                    continue
                body = render(sums).encode()
                s3.put_object(
                    Bucket=bucket,
                    Key=prefix + NAME,
                    Body=body,
                    ContentType=CONTENT_TYPE,
                    Metadata={"sha256": hashlib.sha256(body).hexdigest()},
                )
                written += 1
                print(f"put {bucket}/{prefix}{NAME} ({len(sums)} files)")
    return written, held
