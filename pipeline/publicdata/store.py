"""The raw store: store/<slug>/<version>/manifest.json beside the bytes as fetched.

Manifests are committed. Bytes are kept out of git and attached to a GitHub release
tagged raw/<slug>/<version>, which the build downloads by hash before serialising.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .serialise.profile import Layout

VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The format rules a fetch stamps on each manifest it writes; a version without the stamp keeps
# the formats it was first built with.
CAPS_VERSION = 1


def ext_of(filename: str) -> str:
    return Path(filename).suffix.lstrip(".").lower() or "bin"


def _legacy_layout() -> Layout:
    return {}


@dataclass
class Manifest:
    dataset: str
    version: str
    as_at: str
    fetched_at: str
    sha256: str
    bytes: int
    filename: str
    encoding: str
    source: dict[str, Any]
    licence: dict[str, Any]
    backfilled: bool = False
    tombstone: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    # A digest of the normalised rows in any order, so a reordered export is no new version.
    rows_sha256: str = ""
    # The Parquet layout the fetch found in the register (serialise.profile.layout), which the
    # version's data.parquet keeps for good; empty for a version fetched before the profile.
    parquet: Layout = field(default_factory=_legacy_layout)
    # CAPS_VERSION when the fetch that wrote this manifest knew the caps, else 0.
    caps: int = 0

    @property
    def ext(self) -> str:
        return ext_of(self.filename)

    def to_json(self) -> str:
        d = asdict(self)
        # Left out when empty, so a manifest written before the field keeps its bytes and cache key.
        if not d["rows_sha256"]:
            del d["rows_sha256"]
        if not d["parquet"]:
            del d["parquet"]
        if not d["caps"]:
            del d["caps"]
        return json.dumps(d, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def read(cls, path: Path) -> Manifest:
        d = json.loads(path.read_text(encoding="utf-8"))
        # A field this code does not know, written by newer code, is dropped rather than refused.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_dir(store: Path, slug: str, version: str) -> Path:
    return store / slug / version


def manifests(store: Path, slug: str) -> list[Manifest]:
    d = store / slug
    if not d.is_dir():
        return []
    out = []
    for v in sorted(p for p in d.iterdir() if p.is_dir() and VERSION_RE.match(p.name)):
        mp = v / "manifest.json"
        if mp.exists():
            out.append(Manifest.read(mp))
    return out


def source_path(store: Path, m: Manifest) -> Path:
    return version_dir(store, m.dataset, m.version) / f"source.{m.ext}"


def write(store: Path, m: Manifest, data: bytes) -> Path:
    d = version_dir(store, m.dataset, m.version)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"source.{m.ext}").write_bytes(data)
    (d / "manifest.json").write_text(m.to_json(), encoding="utf-8")
    return d


def verify(store: Path, m: Manifest) -> None:
    p = source_path(store, m)
    if not p.exists():
        msg = f"{p} is missing; run `publicdata store pull {m.dataset}`"
        raise FileNotFoundError(msg)
    got = sha256_file(p)
    if got != m.sha256:
        msg = f"{p}: sha256 {got} does not match manifest {m.sha256}"
        raise ValueError(msg)
