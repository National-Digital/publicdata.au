"""Built version directories, reused when nothing that shapes their bytes has changed."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from importlib.metadata import version as dist_version
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
# Raised in a reviewed change whose edit to the build code changes the bytes of versions across
# datasets; it rebuilds every version. A register entry's `rebuild` does the same for one dataset.
REBUILD = 1
LIBRARIES = ("pyarrow", "xlsxwriter", "openpyxl", "zstandard", "pyyaml", "duckdb")
# One module per format. A version's rows are shaped by everything else the build imports, so a
# writer added or changed rewrites only its own file in a cached version; the JSON and GeoJSON
# writers also make the partition files, so they shape the version and stay in the rows key.
WRITERS_DIR_PARTS = ("serialise", "writers")
ROW_WRITERS = ("json", "geojson")


def _writers_dir() -> Path:
    return PACKAGE.joinpath(*WRITERS_DIR_PARTS)


def _imports(path: Path) -> set[Path]:
    """The package modules one module imports, resolved to files."""
    pkg = path.relative_to(PACKAGE).parent.parts
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for a in node.names:
                mod = a.name.split(".")
                if mod[0] == PACKAGE.name:
                    found |= _files(tuple(mod[1:]))
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            mod = (node.module or "").split(".")
            if mod[0] != PACKAGE.name:
                continue
            parts = tuple(mod[1:])
        else:
            parts = pkg[: len(pkg) - node.level + 1]
            if node.module:
                parts += tuple(node.module.split("."))
        for cand in [parts, *((*parts, a.name) for a in node.names)]:
            found |= _files(cand)
    return found


def _files(cand: tuple[str, ...]) -> set[Path]:
    if not cand:
        return set()
    return {
        p
        for p in (
            PACKAGE.joinpath(*cand, "__init__.py"),
            PACKAGE.joinpath(*cand).with_suffix(".py"),
        )
        if p.is_file()
    }


def _is_writer(p: Path) -> bool:
    return _writers_dir() in p.parents and p.name != "__init__.py"


def code_files() -> list[Path]:
    """build.py and everything it imports inside the package, except the format writers, which
    are keyed one by one; the writers that shape a version are added back. These shape every
    version's bytes but are not in its key: an edit to one is checked against real versions
    (`publicdata verify`) and raises a rebuild number when it changes them."""
    seen: set[Path] = set()
    todo = [PACKAGE / "build.py", PACKAGE / "__init__.py"]
    todo += [p for w in ROW_WRITERS if (p := _writers_dir() / f"{w}.py").is_file()]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        todo.extend(q for q in _imports(p) - seen if not _is_writer(q))
    return sorted(seen)


def _runtime(h) -> None:
    h.update(sys.version.encode() + sqlite3.sqlite_version.encode())
    for lib in LIBRARIES:
        h.update(f"{lib}={dist_version(lib)}\0".encode())


def environment_key() -> str:
    """What every version's key shares: the global rebuild number, the writers that also make
    the partition files, and the runtime and libraries."""
    h = hashlib.sha256(f"rebuild={REBUILD}\0".encode())
    for w in ROW_WRITERS:
        p = _writers_dir() / f"{w}.py"
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    _runtime(h)
    return h.hexdigest()


def _plain(v):
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        out = {}
        for f in sorted(dataclasses.fields(v), key=lambda f: f.name):
            x = getattr(v, f.name)
            if not f.repr:
                continue
            if f.default is not dataclasses.MISSING and x == f.default:
                continue
            if f.default_factory is not dataclasses.MISSING and x == f.default_factory():
                continue
            out[f.name] = _plain(x)
        return out
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    return v


def entry_key(ds) -> str:
    """A register entry as its key reads it: the fields in its repr that differ from their
    defaults, so a field added to the register changes no existing key."""
    return json.dumps(_plain(ds), ensure_ascii=False, separators=(",", ":"), default=str)


def digest(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digests(root: Path, only=None) -> dict[str, str]:
    """The SHA-256 of each file under root, or of those named in only. A DuckDB file's bytes
    differ from one write to the next, so it has none."""
    rels = (
        only
        if only is not None
        else [p.relative_to(root).as_posix() for p in sorted(root.rglob("*")) if p.is_file()]
    )
    return {
        rel: digest(root / rel)
        for rel in sorted(rels)
        if Path(rel).name != "data.duckdb" and (root / rel).is_file()
    }


def writer_key(fmt: str) -> str:
    """What shapes one format's file beyond the rows: its writer's module, the modules of the
    formats it derives its file from, and the libraries."""
    from .serialise import WRITER_DEPENDS, WRITER_MODULES

    h = hashlib.sha256()
    for f in (fmt, *WRITER_DEPENDS.get(fmt, ())):
        p = _writers_dir() / f"{WRITER_MODULES[f]}.py"
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    _runtime(h)
    return h.hexdigest()


def writer_keys() -> dict[str, str]:
    from .serialise import WRITER_MODULES

    return {fmt: writer_key(fmt) for fmt in WRITER_MODULES}


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _read_only(p: Path) -> None:
    """A later write through a shared link fails instead of changing the cache."""
    p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


class BuildCache:
    def __init__(self, root: Path):
        self.root = root
        self.env = environment_key()
        self.used: set[str] = set()
        self.hits = self.misses = 0
        self.grown = 0  # files written into reused versions for writers the entry had not seen

    def key(self, *parts: str) -> str:
        h = hashlib.sha256(self.env.encode())
        for part in parts:
            h.update(b"\0" + part.encode())
        return h.hexdigest()

    def has(self, key: str) -> bool:
        return (self.root / key / "meta.json").is_file()

    def get(self, key: str, dest: Path | None = None) -> dict | None:
        """The entry's metadata, with its files linked into dest when one is given."""
        entry = self.root / key
        meta = entry / "meta.json"
        if not meta.is_file():
            self.misses += 1
            return None
        if dest is not None:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(entry / "files", dest, copy_function=_link_or_copy)
        self.used.add(key)
        self.hits += 1
        return json.loads(meta.read_text(encoding="utf-8"))

    def put(
        self,
        key: str,
        meta: dict,
        src: Path | None = None,
        keep=lambda rel: True,
        extra: dict[str, bytes] | None = None,
    ) -> None:
        """Stores meta, the files under src that keep(relative path) accepts, and `extra`, files
        beside meta.json by name. meta.json is written last, so an entry is whole when it has one."""
        entry = self.root / key
        tmp = self.root / f".{key}.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        if src is not None:
            (tmp / "files").mkdir()
            for p in sorted(src.rglob("*")):
                rel = p.relative_to(src).as_posix()
                if not p.is_file() or not keep(rel):
                    continue
                _read_only(p)
                dst = tmp / "files" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                _link_or_copy(str(p), str(dst))
        for name, data in (extra or {}).items():
            (tmp / name).write_bytes(data)
        (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if entry.exists():
            shutil.rmtree(entry)
        tmp.rename(entry)
        self.used.add(key)

    def prune(self, keep: set[str] | None = None) -> int:
        """Drop entries this build did not use, or with keep, every entry outside it. Pruning to
        the keys a build can use before it starts means a code change, which misses every entry,
        never holds the old cache and a whole new build on disk at once."""
        gone = 0
        if not self.root.is_dir():
            return gone
        keep = self.used if keep is None else keep
        for p in self.root.iterdir():
            if p.name not in keep:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                gone += 1
        return gone
