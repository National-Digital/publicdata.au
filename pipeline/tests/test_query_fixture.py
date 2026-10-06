"""The Parquet file the functions' tests read, written by the pipeline's own writer.

Only the row-group size differs from a published version, so a file of 40 rows has five groups
to prune. Run `python -m tests.test_query_fixture` in pipeline/ to write it again.
"""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from publicdata.normalise import ARROW_TYPES
from publicdata.serialise.writers import parquet as writer

FIXTURE = Path(__file__).parent / "fixtures" / "parquet" / "rows.parquet"
GROUP_ROWS = 8
FIELDS = {
    "lga": "string",
    "year": "integer",
    "fatal": "boolean",
    "speed": "number",
    "day": "date",
    "seen": "datetime",
    "ref": "integer",
}
HEADER = {
    "dataset": "crashes",
    "version": "2026-04-24",
    "licence": {"id": "CC-BY-4.0"},
    "attribution": "Fixture publisher, licensed under CC BY 4.0.",
}


def rows() -> dict[str, list]:
    places = ["Brisbane", "Gold Coast", "Logan", "Cairns", None]
    out = {k: [] for k in FIELDS}
    for i in range(40):
        out["lga"].append(places[i % 5])
        out["year"].append(2018 + i // GROUP_ROWS)
        out["fatal"].append(None if i % 7 == 6 else i % 3 == 0)
        out["speed"].append(None if i % 6 == 5 else 40 + 10 * (i % 5) + 0.5 * (i % 2))
        out["day"].append(dt.date(2018 + i // GROUP_ROWS, 1 + i % 12, 1 + i % 28))
        out["seen"].append(dt.datetime(2026, 4, 24, i % 24, i, 5))
        # Above 2**53, so a reader must keep it as text to keep it exact.
        out["ref"].append(2**53 + i if i % 10 == 9 else 1000 * i)
    return out


def write(path: Path) -> None:
    data = rows()
    table = pa.table({k: pa.array(data[k], ARROW_TYPES[t]) for k, t in FIELDS.items()})
    write_table = writer.pq.write_table
    writer.pq = SimpleNamespace(
        write_table=lambda *a, **k: write_table(*a, **{**k, "row_group_size": GROUP_ROWS})
    )
    try:
        writer.write_parquet(SimpleNamespace(table=table), HEADER, path)
    finally:
        writer.pq = pq


def test_the_committed_fixture_is_what_the_writer_writes(tmp_path):
    fresh = tmp_path / "rows.parquet"
    write(fresh)
    a, b = pq.ParquetFile(FIXTURE), pq.ParquetFile(fresh)
    assert a.metadata.num_row_groups == b.metadata.num_row_groups == 5
    assert a.schema_arrow.equals(b.schema_arrow, check_metadata=True)
    assert a.read().equals(b.read())


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    write(FIXTURE)
