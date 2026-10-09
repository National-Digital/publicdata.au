"""The rolling and feed fixtures fetched into a store, one scheduled read per day file.

The tests and the determinism job build the same history the fetch would have made.

    python -m publicdata.classes_fixture <store>
"""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from . import fetch, store
from .register import load

if TYPE_CHECKING:
    from .register import Dataset
    from .store import ManifestLicence

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "classes"
REGISTER = ROOT / "register"


def read(ds: Dataset, st: Path, src: Path) -> store.Manifest | None:
    """One read of the day's file, handed over as an adapter would, at a fixed time."""
    day = src.stem
    at = f"{day}T20:00:00+00:00"
    fetch._now = lambda: at  # noqa: SLF001 - the fixture sets the fetch's clock
    raw = src.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    lic: ManifestLicence = {"id": ds.licence.id, "read_from": ds.licence.evidence, "read_at": at}
    held = store.manifests(st, ds.slug, fetches=True)
    data: bytes | None
    if held and held[-1].sha256 == sha:
        data, m = None, held[-1]
    else:
        data = raw
        m = store.Manifest(ds.slug, day, "", at, sha, len(raw), f"{ds.slug}.csv", "utf-8",
                           {"url": ds.source.url}, lic)  # fmt: skip
    return fetch.fetch_rolling(ds, st, data, m, lic, dt.date.fromisoformat(day))


def make(st: Path) -> dict[tuple[str, str], store.Manifest | None]:
    """Every fixture day read in order into a fresh store; what each read returned, by day."""
    if st.exists():
        shutil.rmtree(st)
    st.mkdir(parents=True)
    now = fetch._now  # noqa: SLF001 - the fixture sets the fetch's clock
    got: dict[tuple[str, str], store.Manifest | None] = {}
    try:
        for ds in load(REGISTER):
            for src in sorted((ROOT / ds.slug).glob("*.csv")):
                got[(ds.slug, src.stem)] = read(ds, st, src)
    finally:
        fetch._now = now  # noqa: SLF001 - the fixture sets the fetch's clock
    return got


if __name__ == "__main__":
    make(Path(sys.argv[1]))
    print(f"classes fixture store written to {sys.argv[1]}")
