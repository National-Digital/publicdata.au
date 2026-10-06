import math
import sqlite3

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from publicdata.records import connect

ROWS = {
    "n": [1, 2, 3, None],
    "x": [1.5, math.nan, 2.25, None],
    "s": ["01", "1", "b", None],
}


@pytest.fixture
def pair(tmp_path):
    """The same rows as a version's Parquet and as its data.sqlite."""
    parquet = tmp_path / "data.parquet"
    pq.write_table(
        pa.table(
            {
                "n": pa.array(ROWS["n"], pa.int64()),
                "x": pa.array(ROWS["x"], pa.float64()),
                "s": pa.array(ROWS["s"], pa.string()),
            }
        ),
        parquet,
    )
    db = sqlite3.connect(":memory:")
    db.execute('CREATE TABLE records ("n" INTEGER, "x" REAL, "s" TEXT)')
    db.executemany("INSERT INTO records VALUES (?, ?, ?)", zip(*ROWS.values(), strict=True))
    with connect(parquet) as con:
        yield con, db


def test_a_nan_reads_as_null_as_it_does_in_sqlite(pair):
    con, db = pair
    for fn in ("count", "sum", "avg", "min", "max"):
        got = con.execute(f"SELECT {con.agg(fn, 'x')} FROM records").fetchone()[0]
        want = db.execute(f'SELECT {fn.upper()}("x") FROM records').fetchone()[0]
        assert got == want, fn
    assert (
        con.execute('SELECT "x" FROM records ORDER BY rowid').fetchall()
        == db.execute('SELECT "x" FROM records ORDER BY rowid').fetchall()
    )


@pytest.mark.parametrize("op", ["=", "!=", ">", "<", ">=", "<="])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("n", "1.5"),
        ("n", "2"),
        ("n", " 2 "),
        ("n", "2.0"),
        ("n", "1e0"),
        ("x", "2"),
        ("x", 2),
        ("s", 1),
        ("s", "01"),
        ("s", 1.0),
    ],
)
def test_a_parameter_compares_as_it_would_against_sqlite(pair, op, field, value):
    con, db = pair
    sql = f'SELECT rowid FROM records WHERE "{field}" {op} ? ORDER BY rowid'
    got = con.execute(sql, [con.param(field, value)]).fetchall()
    assert got == db.execute(sql, (value,)).fetchall()


def test_text_that_is_not_a_number_is_refused_against_a_number_column(pair):
    con, _ = pair
    with pytest.raises(ValueError, match="holds numbers"):
        con.param("n", "abc")
