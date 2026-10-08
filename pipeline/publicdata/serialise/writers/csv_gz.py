from __future__ import annotations

import gzip
import shutil
from pathlib import Path

from ...normalise import Table
from .csv import write_csv


def write_csv_gz(tbl: Table, path: Path, vdir: Path | None = None) -> None:
    """The CSV again, gzipped with no name or mtime so the bytes are reproducible. The CSV
    beside the target is read when it is there, as in a full build; else it is written first.
    """
    csv_path = (vdir or path.parent) / "data.csv"
    if not csv_path.exists():
        write_csv(tbl, csv_path)
    with csv_path.open("rb") as src, path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as gz:
            shutil.copyfileobj(src, gz, 1 << 20)
