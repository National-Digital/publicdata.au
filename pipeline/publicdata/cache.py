"""Built version directories, reused when nothing that shapes their bytes has changed."""

from __future__ import annotations

import ast
import dataclasses
import functools
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
from importlib.metadata import version as dist_version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import duckdb

from .register import Licence
from .serialise import WRITER_DEPENDS, WRITER_MODULES, WRITER_VARIANTS, WRITERS, duckdb_digest
from .serialise.geo import geo_kind

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from .register import Dataset

    class _Hasher(Protocol):
        def update(self, data: bytes, /) -> None: ...


PACKAGE = Path(__file__).resolve().parent
# Raised in a reviewed change whose edit to the build code changes the bytes of versions across
# datasets; it rebuilds every version. A register entry's `rebuild` does the same for one dataset.
REBUILD = 1
LIBRARIES = (
    "pyarrow",
    "xlsxwriter",
    "openpyxl",
    "xlrd",
    "zstandard",
    "pyyaml",
    "duckdb",
    "pmtiles",
)
# One module per format. A version's rows are shaped by everything else the build imports, so a
# writer added or changed rewrites only its own file in a cached version; the JSON and GeoJSON
# writers also make the partition files, so they shape the version and stay in the rows key.
WRITERS_DIR_PARTS = ("serialise", "writers")
ROW_WRITERS = ("json", "geojson")
# Modules only one kind of dataset runs. They are in the keys of that kind's versions, since the
# sample cannot afford to build such a dataset (G-NAF is the only database).
KIND_MODULES = {"database": ("database.py",)}


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
    """The build's code files: build.py and everything it imports inside the package.

    The format writers are left out, since they are keyed one by one; the writers that shape a
    version are added back. These shape every version's bytes but are not in its key: an edit to
    one is checked against real versions (`publicdata verify`) and raises a rebuild number when
    it changes them.
    """
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


@functools.cache
def spatial_version() -> str:
    """The installed DuckDB spatial extension, which joins the spine and draws the shapes."""
    con = duckdb.connect()
    try:
        row = con.execute(
            "SELECT extension_version FROM duckdb_extensions() "
            "WHERE extension_name = 'spatial' AND installed"
        ).fetchone()
    finally:
        con.close()
    got = row[0] if row and row[0] else "none"
    # The extension is fetched per runner, so every job of a deploy must key on the same build.
    want = os.environ.get("PUBLICDATA_SPATIAL", "")
    if want and want != got:
        msg = f"DuckDB spatial extension {got} is installed, and the plan keyed on {want}"
        raise RuntimeError(msg)
    return got


def spatial(ds: Dataset) -> bool:
    """Whether a dataset's build loads the spatial extension.

    It does for a layer with geometry, or a dataset joined to the place spine.
    """
    return bool(ds.geometry or ds.enrich)


def shape_layer(ds: Dataset) -> bool:
    """Whether a dataset's Parquet is written by the shape layer writer."""
    return geo_kind(ds) in ("polygon", "line")


def kind_key(kind: str) -> str:
    """The modules only this kind of dataset runs, or "" for a kind that has none."""
    h = hashlib.sha256()
    names = KIND_MODULES.get(kind, ())
    for name in names:
        p = PACKAGE / name
        h.update(name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest() if names else ""


def _runtime(h: _Hasher) -> None:
    h.update(sys.version.encode() + sqlite3.sqlite_version.encode())
    for lib in LIBRARIES:
        h.update(f"{lib}={dist_version(lib)}\0".encode())


def environment_key() -> str:
    """What every version's key shares.

    That is the global rebuild number, the writers that also make the partition files, and the
    runtime and libraries.
    """
    h = hashlib.sha256(f"rebuild={REBUILD}\0".encode())
    for w in ROW_WRITERS:
        p = _writers_dir() / f"{w}.py"
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    _runtime(h)
    return h.hexdigest()


def _plain(v: object) -> object:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        out: dict[str, object] = {}
        for f in sorted(dataclasses.fields(v), key=lambda f: f.name):
            x = getattr(v, f.name)
            if not f.repr:
                continue
            if f.default is not dataclasses.MISSING and x == f.default:
                continue
            if f.default_factory is not dataclasses.MISSING and x == f.default_factory():
                continue
            out[f.name] = _plain(x)

        if isinstance(v, Licence):
            # Read from the grant files, and written into every format's header.
            out |= {"title": v.title, "url": v.url, "condition": v.condition}
        return out
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    return v


def entry_key(ds: Dataset) -> str:
    """A register entry as its key reads it.

    Only the fields in its repr that differ from their defaults count, so a field added to the
    register changes no existing key.
    """
    return json.dumps(_plain(ds), ensure_ascii=False, separators=(",", ":"), default=str)


def digest(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digests(
    root: Path, only: Iterable[str] | None = None, *, databases: bool = True
) -> dict[str, str]:
    """The SHA-256 of each file under root, or of those named in only.

    A DuckDB file's bytes differ from one write to the next, so it gets a digest of its tables,
    rows and comments instead, or none when databases is False, as for a database release of
    many gigabytes.
    """
    rels = (
        only
        if only is not None
        else [p.relative_to(root).as_posix() for p in sorted(root.rglob("*")) if p.is_file()]
    )
    out: dict[str, str] = {}
    for rel in sorted(rels):
        p = root / rel
        if not p.is_file():
            continue
        if p.name != "data.duckdb":
            out[rel] = digest(p)
        elif databases:
            out[rel] = "duckdb:" + duckdb_digest(p)
    return out


def writer_files(fmt: str, *, shape: bool | None = None) -> list[Path]:
    """The writer modules one format's file comes from.

    These are the modules its entry in WRITERS calls, those of the formats it derives its file
    from, and the writer modules each of them imports. A format with a writer for shape layers
    (WRITER_VARIANTS) reads only the one a dataset runs when shape says which; None reads both.
    """
    todo: set[Path] = set()
    for f in (fmt, *WRITER_DEPENDS.get(fmt, ())):
        if shape is not None and f in WRITER_VARIANTS:
            objs: list[object] = [WRITER_VARIANTS[f][shape]]
        else:
            fn = WRITERS[f]
            objs = [fn.__globals__.get(name) for name in fn.__code__.co_names]
        for obj in objs:
            mod = sys.modules.get(getattr(obj, "__module__", "") or "")
            if mod is not None and getattr(mod, "__file__", None):
                p = Path(mod.__file__).resolve()  # type: ignore[arg-type]  # the getattr above found it
                if _is_writer(p):
                    todo.add(p)
    seen: set[Path] = set()
    while todo:
        p = todo.pop()
        seen.add(p)
        todo |= {q for q in _imports(p) if _is_writer(q)} - seen
    return sorted(seen)


def writer_key(fmt: str, *, shape: bool = False) -> str:
    """What shapes one format's file beyond the rows: its writer modules and the libraries."""
    h = hashlib.sha256()
    for p in writer_files(fmt, shape=shape):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    _runtime(h)
    return h.hexdigest()


def writer_keys(*, shape: bool = False) -> dict[str, str]:
    """Each format's writer key for a table, or for a shape layer when shape is True."""
    return {fmt: writer_key(fmt, shape=shape) for fmt in WRITER_MODULES}


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _read_only(p: Path) -> None:
    """A later write through a shared link fails instead of changing the cache."""
    p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


class BuildCache:
    def __init__(self, root: Path) -> None:
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

    def get(self, key: str, dest: Path | None = None) -> dict[str, Any] | None:
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
        got: dict[str, Any] = json.loads(meta.read_text(encoding="utf-8"))
        return got

    def put(
        self,
        key: str,
        meta: dict[str, Any],
        src: Path | None = None,
        keep: Callable[[str], bool] = lambda _rel: True,
        extra: dict[str, bytes] | None = None,
    ) -> None:
        """Stores meta, the files under src that keep(relative path) accepts, and `extra`.

        The `extra` files go beside meta.json by name. meta.json is written last, so an entry is
        whole when it has one.
        """
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
        """Drop entries this build did not use, or with keep, every entry outside it.

        Pruning to the keys a build can use before it starts means a code change, which misses
        every entry, never holds the old cache and a whole new build on disk at once.
        """
        gone = 0
        if not self.root.is_dir():
            return gone
        keep = self.used if keep is None else keep
        for p in self.root.iterdir():
            if p.name not in keep:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                gone += 1
        return gone
