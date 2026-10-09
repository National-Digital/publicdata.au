"""SHA256SUMS for every dated version in R2, written after the deploy pushes the version files.

Nothing the build imports reads this module, so adding or changing it keys no version. Each line
names the file as the site saves it (`site.download_name`), so `sha256sum -c --ignore-missing
SHA256SUMS` checks the files a browser or `curl -OJ` downloaded. The hashes come from the SHA-256
that `r2.push` stores with every object, and an object without one is read and hashed only when
the caller allows it. A source file served from the raw store takes its hash from the version's
manifest. A list is part of its version and is written once, when the version has none. It is
made again only when the version's prefix is replaced, which purges the edge. A file newer than
the list outside a replace breaks the rule that a version never changes, so it is reported and
the list is left as it is, unless R2 stored it again with the SHA-256 the list already holds.
The lists written are recorded as subjects, in the sha256sum format, for the deploy to attest.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from .store import ext_of

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ObjectTypeDef

    type Objects = dict[str, ObjectTypeDef]

BUCKET = "publicdata-dist"
NAME = "SHA256SUMS"
CONTENT_TYPE = "text/plain; charset=utf-8"
PAGES = ("index.html", "index.md")
KEY = re.compile(r"^d/([a-z0-9-]+)/v/(\d{4}-\d{2}-\d{2})/(.+)$")
HEX = re.compile(r"^[0-9a-f]{64}$")
WORKERS = 16
# The most subjects one attestation takes (actions/attest).
MAX_SUBJECTS = 1024


def render(sums: dict[str, str]) -> str:
    """The sha256sum text: one line per file, sorted by name, two spaces between."""
    return "".join(f"{h}  {n}\n" for n, h in sorted(sums.items()))


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        h, sep, n = line.partition("  ")
        if sep and HEX.match(h) and n:
            out[n] = h
    return out


def write_subjects(lists: dict[str, str], out: Path) -> list[Path]:
    """The lists' keys and SHA-256 as sha256sum files, one per attestation.

    Each holds at most MAX_SUBJECTS lines, named 1.sha256, 2.sha256 and so on.
    """
    out.mkdir(parents=True, exist_ok=True)
    items = sorted(lists.items())
    parts: list[Path] = []
    for i in range(0, len(items), MAX_SUBJECTS):
        p = out / f"{i // MAX_SUBJECTS + 1}.sha256"
        p.write_text(render(dict(items[i : i + MAX_SUBJECTS])))
        parts.append(p)
    return parts


def slugs_in(roots: Iterable[Path]) -> list[str]:
    """The datasets with dated versions in the built trees."""
    found: set[str] = set()
    for root in roots:
        d = Path(root) / "d"
        if d.is_dir():
            found |= {p.parent.name for p in d.glob("*/v") if p.is_dir()}
    return sorted(found)


def slugs_in_bucket(s3: S3Client, bucket: str = BUCKET) -> list[str]:
    found: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix="d/", Delimiter="/"
    ):
        found += [c["Prefix"].split("/")[1] for c in page.get("CommonPrefixes", [])]
    return sorted(found)


def _versions(s3: S3Client, bucket: str, slug: str) -> dict[str, Objects]:
    """Each version's objects under d/<slug>/v/, by path within the version."""
    out: dict[str, Objects] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"d/{slug}/v/"):
        for o in page.get("Contents", []):
            m = KEY.match(o["Key"])
            if m and m.group(1) == slug:
                out.setdefault(m.group(2), {})[m.group(3)] = o
    return out


def _files(objects: Objects) -> Objects:
    return {rel: o for rel, o in objects.items() if rel != NAME and rel not in PAGES}


def newer(objects: Objects) -> list[str]:
    """The files written after the version's list, by path within the version."""
    sums = objects[NAME]["LastModified"]
    return sorted(rel for rel, o in _files(objects).items() if o["LastModified"] > sums)


def _stored_sha256(s3: S3Client, bucket: str, key: str) -> str | None:
    h = s3.head_object(Bucket=bucket, Key=key).get("Metadata", {}).get("sha256", "")
    return h if HEX.match(h) else None


def _read_sha256(s3: S3Client, bucket: str, key: str) -> str:
    h = hashlib.sha256()
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    for chunk in iter(lambda: body.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def _source_line(s3: S3Client, bucket: str, prefix: str) -> tuple[str, str] | None:
    """The publisher's file as the manifest records it.

    This is for a version whose source.<ext> is served from the raw store and so is missing from
    this listing.
    """
    m: dict[str, str] = json.loads(
        s3.get_object(Bucket=bucket, Key=prefix + "manifest.json")["Body"].read()
    )
    if m.get("source_withheld") or not HEX.match(m.get("sha256", "")):
        return None
    return f"source.{ext_of(m.get('filename', ''))}", m["sha256"]


def version_sums(  # noqa: PLR0913, PLR0917 - update passes them in place
    s3: S3Client,
    bucket: str,
    slug: str,
    version: str,
    objects: Objects,
    download: bool = False,  # noqa: FBT001, FBT002 - update passes it in place
    pool: ThreadPoolExecutor | None = None,
) -> tuple[dict[str, str], list[str]]:
    """The version's sums by download name, and the keys whose hash is unknown."""
    # The site is imported only when the lists are written, as the module docstring says.
    from .site import download_name  # noqa: PLC0415 - kept out of the build's imports

    prefix = f"d/{slug}/v/{version}/"
    sums: dict[str, str] = {}
    todo = [(download_name(slug, version, rel), prefix + rel) for rel in sorted(_files(objects))]

    def one(item: tuple[str, str]) -> tuple[str, str, str | None]:
        name, key = item
        h = _stored_sha256(s3, bucket, key)
        if h is None and download:
            h = _read_sha256(s3, bucket, key)
        return name, key, h

    unknown: list[str] = []
    for name, key, h in pool.map(one, todo) if pool else map(one, todo):
        if h is None:
            unknown.append(key)
        else:
            sums[name] = h
    if "manifest.json" in objects and not any(r.startswith("source.") for r in objects):
        src = _source_line(s3, bucket, prefix)
        if src:
            sums[download_name(slug, version, src[0])] = src[1]
    return sums, unknown


def differing(s3: S3Client, bucket: str, slug: str, version: str, rels: list[str]) -> list[str]:
    """Of the files newer than the version's list, those whose stored SHA-256 differs from it.

    A file R2 stored again with the same bytes, such as one compressed at rest, matches its line
    and is left out.
    """
    from .site import download_name  # noqa: PLC0415 - kept out of the build's imports

    prefix = f"d/{slug}/v/{version}/"
    listed = parse(s3.get_object(Bucket=bucket, Key=prefix + NAME)["Body"].read().decode())
    return [
        rel
        for rel in rels
        if (h := listed.get(download_name(slug, version, rel))) is None
        or _stored_sha256(s3, bucket, prefix + rel) != h
    ]


def update(  # noqa: C901, PLR0913, PLR0917 - one pass over the versions, its options named at each call
    slugs: Iterable[str],
    bucket: str = BUCKET,
    replace: tuple[str, ...] = (),
    download: bool = False,  # noqa: FBT001, FBT002 - every caller names it
    s3: S3Client | None = None,
    workers: int = WORKERS,
    lists: dict[str, str] | None = None,
    every: bool = False,  # noqa: FBT001, FBT002 - every caller names it
) -> tuple[int, list[str], list[str]]:
    """Writes SHA256SUMS for every version of these datasets that has none, and for a replaced one.

    Returns how many were written, the versions left without one because a file's hash is
    unknown, and the keys of files changed after their version's list outside a replace, which no
    list is rewritten for. Each list written goes into `lists` as key -> SHA-256, and with `every`
    so does each list left as it is, so all of them can be attested again.
    """
    if s3 is None:
        from .r2 import client  # noqa: PLC0415 - the deploy extra

        s3 = client()
    written = 0
    held: list[str] = []
    changed: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for slug in slugs:
            for version, objects in sorted(_versions(s3, bucket, slug).items()):
                prefix = f"d/{slug}/v/{version}/"
                replaced = prefix.startswith(replace) if replace else False
                if not _files(objects):
                    continue
                if NAME in objects and not replaced:
                    if late := newer(objects):
                        changed += [prefix + r for r in differing(s3, bucket, slug, version, late)]
                    if every and lists is not None:
                        h = _stored_sha256(s3, bucket, prefix + NAME)
                        if h:
                            lists[prefix + NAME] = h
                    continue
                if NAME in objects:
                    # Gone before the new one is made, so a run that stops leaves the version
                    # with no list, which the next deploy writes, never with the old one.
                    s3.delete_object(Bucket=bucket, Key=prefix + NAME)
                sums, unknown = version_sums(s3, bucket, slug, version, objects, download, pool)
                if unknown:
                    held.append(prefix)
                    print(
                        f"checksums: {prefix} has {len(unknown)} file(s) with no stored SHA-256, "
                        f"e.g. {unknown[0]}",
                        file=sys.stderr,
                    )
                    continue
                body = render(sums).encode()
                digest = hashlib.sha256(body).hexdigest()
                s3.put_object(
                    Bucket=bucket,
                    Key=prefix + NAME,
                    Body=body,
                    ContentType=CONTENT_TYPE,
                    Metadata={"sha256": digest},
                )
                if lists is not None:
                    lists[prefix + NAME] = digest
                written += 1
                print(f"put {bucket}/{prefix}{NAME} ({len(sums)} files)")
    return written, held, changed
