"""The rolling and feed fixtures fetched into a store, one scheduled read per day file, so the
tests and the determinism job build the same history the fetch would have made.

    python -m publicdata.classes_fixture <store>
"""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import sys
from pathlib import Path

from . import fetch, store
from .register import load

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "classes"
REGISTER = ROOT / "register"


def read(ds, st: Path, src: Path):
    """One read of the day's file, handed over as an adapter would, at a fixed time."""
    day = src.stem
    at = f"{day}T20:00:00+00:00"
    fetch._now = lambda: at
    data = src.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    lic = {"id": ds.licence.id, "read_from": ds.licence.evidence, "read_at": at}
    held = store.manifests(st, ds.slug, fetches=True)
    if held and held[-1].sha256 == sha:
        data, m = None, held[-1]
    else:
        m = store.Manifest(ds.slug, day, "", at, sha, len(data), f"{ds.slug}.csv", "utf-8",
                           {"url": ds.source.url}, lic)  # fmt: skip
    return fetch.fetch_rolling(ds, st, data, m, lic, dt.date.fromisoformat(day))


def make(st: Path) -> dict:
    """Every fixture day read in order into a fresh store; what each read returned, by day."""
    if st.exists():
        shutil.rmtree(st)
    st.mkdir(parents=True)
    now = fetch._now
    got = {}
    try:
        for ds in load(REGISTER):
            for src in sorted((ROOT / ds.slug).glob("*.csv")):
                got[(ds.slug, src.stem)] = read(ds, st, src)
    finally:
        fetch._now = now
    return got


if __name__ == "__main__":
    make(Path(sys.argv[1]))
    print(f"classes fixture store written to {sys.argv[1]}")
