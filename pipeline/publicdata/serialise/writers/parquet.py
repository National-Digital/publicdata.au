from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ...normalise import Table
from .. import profile


def write_parquet(tbl: Table, header: dict, path: Path, int32: Sequence[str] | None = None) -> None:
    ds = tbl.dataset
    profile.write(tbl.table, header, path, ds.sort, ds.key, ds.lookup, int32)
