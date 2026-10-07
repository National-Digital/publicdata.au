"""A cached version's Parquet, which its cache entry leaves out, read back from where the build
that made it put it: a shard's tree, or the published files in R2."""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .cache import _link_or_copy

# The deploy reads every version's Parquet for its figures, so downloads run side by side.
WORKERS = 16


class Published:
    def __init__(
        self,
        out: Path,
        roots: list[Path] = (),
        source: Path | None = None,
        download: Callable[[str, Path], bool] | None = None,
    ):
        """source is a directory laid out as the published site; download(rel, path) fetches a
        published file into path and says whether it was there."""
        self.out = out
        self.roots = [Path(r) for r in roots] + ([Path(source)] if source else [])
        self.source = Path(source) if source else None
        self.download = download
        self._copies: dict[str, Path | None] = {}
        self._side: Path | None = None
        self._lock = threading.Lock()

    def path(self, rel: str) -> Path:
        """out/rel, linked or downloaded in when the build left it out. A file no one holds
        stays missing, so the reader that needed it fails on its name."""
        p = self.out / rel
        if p.exists():
            return p
        for root in self.roots:
            if (root / rel).is_file():
                p.parent.mkdir(parents=True, exist_ok=True)
                _link_or_copy(str(root / rel), str(p))
                return p
        if self.download:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.part")
            if self.download(rel, tmp):
                tmp.rename(p)
        return p

    def copy(self, rel: str) -> Path | None:
        """The file the site already serves at rel, or None when it serves none yet. A dated file
        is never overwritten outside a replace, so this can be older bytes than the build made
        at out/rel, which stays as it is."""
        with self._lock:
            if rel in self._copies:
                return self._copies[rel]
            found = None
            if self.source is not None and (self.source / rel).is_file():
                found = self.source / rel
                if not (self.out / rel).exists():
                    (self.out / rel).parent.mkdir(parents=True, exist_ok=True)
                    _link_or_copy(str(found), str(self.out / rel))
                    found = self.out / rel
            elif self.download:
                p = self.out / rel
                if p.exists():
                    if self._side is None:
                        self._side = Path(tempfile.mkdtemp(prefix="publicdata-published-"))
                    p = self._side / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(f".{p.name}.part")
                if self.download(rel, tmp):
                    tmp.rename(p)
                    found = p
            self._copies[rel] = found
            return found

    def paths(self, rels: list[str]) -> list[Path]:
        with ThreadPoolExecutor(WORKERS) as pool:
            return list(pool.map(self.path, rels))


# Set by the build command; without it a build reads only what it wrote or linked from its cache.
current: Published | None = None


def path(out: Path, rel: str) -> Path:
    if current is None or current.out != out:
        return out / rel
    return current.path(rel)


def served(out: Path, rel: str) -> tuple[Path, bool]:
    """rel as the site serves it, and whether that is the copy already published: the history
    archive, the diffs and a grown format are made from it, so a version built again without a
    replace still yields what the published files hold, and the check can make the same."""
    if current is not None and current.out == out and (p := current.copy(rel)) is not None:
        return p, True
    return path(out, rel), False
