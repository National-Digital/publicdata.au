import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from publicdata.register import Dataset, Licence, Publisher, Source
from publicdata.store import Manifest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def register_dir() -> Path:
    return ROOT / "register"


@pytest.fixture
def fixture_store() -> Path:
    return Path(__file__).parent / "fixtures" / "store"


@pytest.fixture(scope="session")
def fixture_site(tmp_path_factory) -> Path:
    """One `build --fixtures` per session, read only; a test that edits it takes site_copy."""
    out = tmp_path_factory.mktemp("fixture-site") / "dist"
    subprocess.run(
        [sys.executable, "-m", "publicdata", "build", "--fixtures", "--out", str(out)], check=True
    )
    return out


@pytest.fixture
def site_copy(fixture_site, tmp_path) -> Path:
    return Path(shutil.copytree(fixture_site, tmp_path / "dist"))


@pytest.fixture(scope="session")
def fixture_builds(tmp_path_factory, fixture_site):
    """The plain build (the shared fixture site) and a cold cached build of the same store."""
    from publicdata.__main__ import main

    root = tmp_path_factory.mktemp("fixture-builds")
    store = Path(__file__).parent / "fixtures" / "store"
    plain, cold, cache = fixture_site, root / "cold", root / "cache"
    assert main(["build", "--store", str(store), "--out", str(cold), "--cache", str(cache), "--absent", str(root / "cold.json")]) == 0  # fmt: skip
    return plain, cold, cache


def make_dataset(fields, **kw) -> Dataset:
    base = dict(
        slug="t",
        title="Test",
        status="live",
        publisher=Publisher("Test Agency", "Test", "Qld", "https://example.gov.au/"),
        licence=Licence(
            "CC-BY-4.0",
            "https://example.gov.au/data",
            "Test Agency, Test, sourced {sourced}. CC BY 4.0.",
        ),
        source=Source(adapter="ckan-resource", url="https://example.gov.au/data"),
        fields=tuple(fields),
    )
    base.update(kw)
    return Dataset(**base)


def make_manifest(data: bytes, **kw) -> Manifest:
    import hashlib

    base = dict(
        dataset="t",
        version="2026-01-02",
        as_at="2025-12-31",
        fetched_at="2026-01-02T01:00:00+00:00",
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        filename="source.csv",
        encoding="utf-8",
        source={"url": "https://example.gov.au/file.csv"},
        licence={"id": "CC-BY-4.0", "title": "Creative Commons Attribution 4.0"},
    )
    base.update(kw)
    return Manifest(**base)


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def as_parquet(db: Path) -> Path:
    """A hand-made data.sqlite's records table as the data.parquet beside it, typed from its
    declared columns, which is what the build reads now."""
    import sqlite3

    import pyarrow as pa
    import pyarrow.parquet as pq

    types = {"INTEGER": pa.int64(), "REAL": pa.float64(), "TEXT": pa.string()}
    con = sqlite3.connect(db)
    cols = con.execute("PRAGMA table_info(records)").fetchall()
    rows = con.execute("SELECT * FROM records").fetchall()
    con.close()
    out = db.with_name("data.parquet")
    pq.write_table(
        pa.table({c[1]: pa.array([r[i] for r in rows], types[c[2]]) for i, c in enumerate(cols)}),
        out,
    )
    return out
