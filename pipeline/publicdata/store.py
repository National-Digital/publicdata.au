"""The raw store: store/<slug>/<version>/manifest.json beside the bytes as fetched.

Manifests are committed. Bytes are kept out of git and attached to a GitHub release
tagged raw/<slug>/<version>, which the build downloads by hash before serialising.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def ext_of(filename: str) -> str:
    return Path(filename).suffix.lstrip(".").lower() or "bin"


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
    source: dict
    licence: dict
    backfilled: bool = False
    tombstone: dict | None = None
    notes: list[str] = field(default_factory=list)
    # A digest of the normalised rows in any order, so a reordered export is no new version.
    rows_sha256: str = ""
    # The Parquet layout the fetch found in the register (serialise.profile.layout), which the
    # version's data.parquet keeps for good; empty for a version fetched before the profile.
    parquet: dict = field(default_factory=dict)
    # A rolling source or a feed keeps every fetch that changed it; only some become snapshots,
    # the dated versions the site publishes, and `cut` says why.
    snapshot: bool = True
    cut: str = ""
    # A feed's table of every row state it has held, kept beside the source as history.parquet.
    history: dict | None = None
    # The register's period when this was fetched; the version is split by it, and a version
    # fetched before an entry had one keeps its whole-table layout.
    period: dict | None = None
    # The register's update class when this was fetched; empty for a release. It decides how the
    # version flags revisions, so a later change of class leaves the version as it was.
    update: str = ""

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
        if d["snapshot"]:
            del d["snapshot"]
        if not d["cut"]:
            del d["cut"]
        for k in ("history", "period"):
            if d[k] is None:
                del d[k]
        if not d["update"]:
            del d["update"]
        return json.dumps(d, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def read(cls, path: Path) -> Manifest:
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(**d)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_dir(store: Path, slug: str, version: str) -> Path:
    return store / slug / version


def manifests(store: Path, slug: str, fetches: bool = False) -> list[Manifest]:
    """The dataset's snapshots in date order, or with fetches, every fetch the store keeps."""
    d = store / slug
    if not d.is_dir():
        return []
    out = []
    for v in sorted(p for p in d.iterdir() if p.is_dir() and VERSION_RE.match(p.name)):
        mp = v / "manifest.json"
        if mp.exists():
            m = Manifest.read(mp)
            if fetches or m.snapshot:
                out.append(m)
    return out


def history_path(store: Path, m: Manifest) -> Path:
    return version_dir(store, m.dataset, m.version) / "history.parquet"


def read_path(store: Path, slug: str) -> Path:
    return store / slug / "read.json"


def write_read(store: Path, slug: str, day: str, fetch: str) -> None:
    """A feed's newest read, changed or not, and the fetch whose rows it found."""
    read_path(store, slug).write_text(
        json.dumps({"read": day, "fetch": fetch}, indent=2) + "\n", encoding="utf-8"
    )


def last_read(store: Path, slug: str) -> dict:
    p = read_path(store, slug)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def read_since(store: Path, slug: str, newest: str) -> str:
    """The day of a feed's newest read that found the newest fetch's rows, or "". The record lives
    in the raw store, not in git, so it can be missing, or name a fetch this checkout does not hold
    yet; either way last_seen stops at the newest fetch."""
    rec = last_read(store, slug)
    return (
        rec.get("read", "") if rec.get("fetch") == newest and rec.get("read", "") > newest else ""
    )


def change_log(store: Path, m: Manifest) -> Path:
    """A rolling source's or a feed's comparison of this fetch with the one before, by key."""
    return version_dir(store, m.dataset, m.version) / "changes.json"


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
        raise FileNotFoundError(f"{p} is missing; run `publicdata store pull {m.dataset}`")
    got = sha256_file(p)
    if got != m.sha256:
        raise ValueError(f"{p}: sha256 {got} does not match manifest {m.sha256}")
