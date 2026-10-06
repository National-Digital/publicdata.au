"""A cached version's Parquet and SQLite, which its cache entry leaves out, read back from where
the build that made them put them: a shard's tree, or the published files in R2."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .cache import _link_or_copy

# The deploy reads every version's SQLite for its figures, so downloads run side by side.
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
        self.download = download

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

    def paths(self, rels: list[str]) -> list[Path]:
        with ThreadPoolExecutor(WORKERS) as pool:
            return list(pool.map(self.path, rels))


# Set by the build command; without it a build reads only what it wrote or linked from its cache.
current: Published | None = None


def path(out: Path, rel: str) -> Path:
    if current is None or current.out != out:
        return out / rel
    return current.path(rel)
