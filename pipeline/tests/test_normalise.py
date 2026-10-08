import datetime as dt
import io
import json
import zipfile
from pathlib import Path

import pyarrow as pa
import pytest

from publicdata.normalise import NormaliseError, normalise, read_csv, read_xlsx, xls_to_xlsx
from publicdata.register import LAT_SOURCE, LON_SOURCE, Field, Source

from .conftest import make_dataset, make_manifest

CSV = b"\xef\xbb\xbfId,Name,Count,Flag,When,Extra\n1, Alpha ,5,Yes,03/02/2024,zzz\n2,,<5,No,,zzz\n3,Gamma,,Yes,31/12/2025,\n"
FIELDS = [
    Field("id", "Id", "integer"),
    Field("name", "Name"),
    Field("count", "Count", "integer"),
    Field("flag", "Flag", "boolean", true_values=("Yes",), false_values=("No",)),
    Field("when", "When", "date", date_format="%d/%m/%Y"),
]


def test_types_blanks_suppression_and_allow_list():
    ds = make_dataset(FIELDS, suppression=("<5",), key=("id",))
    t = normalise(ds, make_manifest(CSV, encoding="utf-8-sig"), CSV)
    rows = t.table.to_pylist()
    assert t.unknown_columns == ["Extra"]
    assert t.suppressed_cells == 1
    assert rows[0]["name"] == "Alpha"
    assert rows[1]["name"] is None
    assert rows[0]["count"] == 5
    assert rows[1]["count"] is None
    assert rows[2]["count"] is None
    assert rows[1]["suppressed"] == ["count"]
    assert rows[2]["suppressed"] == []
    assert rows[0]["flag"] is True
    assert rows[1]["flag"] is False
    assert str(rows[0]["when"]) == "2024-02-03"
    assert rows[1]["when"] is None
    assert "Extra" not in t.table.column_names


def test_missing_register_column_stops_the_build():
    ds = make_dataset([*FIELDS, Field("gone", "Gone")])
    with pytest.raises(NormaliseError, match="absent upstream"):
        normalise(ds, make_manifest(CSV, encoding="utf-8-sig"), CSV)


def test_untypable_value_stops_the_build():
    bad = b"Id,Name,Count,Flag,When,Extra\n1,A,many,Yes,01/01/2024,\n"
    ds = make_dataset(FIELDS)
    with pytest.raises(NormaliseError, match="count"):
        normalise(ds, make_manifest(bad), bad)
    bad2 = b"Id,Name,Count,Flag,When,Extra\n1,A,1,Maybe,01/01/2024,\n"
    with pytest.raises(NormaliseError, match="true/false"):
        normalise(ds, make_manifest(bad2), bad2)


def test_cp1252_source_becomes_utf8():
    data = "Id,Name,Count,Flag,When,Extra\n1,Caf\xe9,1,Yes,01/01/2024,\n".encode("cp1252")
    ds = make_dataset(FIELDS)
    t = normalise(ds, make_manifest(data, encoding="cp1252"), data)
    assert t.table.to_pylist()[0]["name"] == "Café"


def test_geojson_source_becomes_rows_with_the_point_as_two_fields():
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [147.33, -42.88]},
                "properties": {"ID": 1, "WHEN": 1364602560000, "SPEED": "060"},
            },
            {
                "type": "Feature",
                "geometry": None,
                "properties": {"ID": 2, "WHEN": None, "SPEED": "100", "LATE": "x"},
            },
        ],
    }
    data = json.dumps(fc).encode()
    ds = make_dataset(
        [
            Field("id", "ID", "integer"),
            Field("when", "WHEN", "datetime", date_format="epoch_ms"),
            Field("speed_zone", "SPEED"),
            Field("longitude", LON_SOURCE, "number"),
            Field("latitude", LAT_SOURCE, "number"),
        ],
        key=("id",),
    )
    t = normalise(ds, make_manifest(data, filename="t.geojson", encoding="utf-8"), data)
    rows = t.table.to_pylist()
    assert t.unknown_columns == ["LATE"]
    assert rows[0]["longitude"] == 147.33
    assert rows[0]["latitude"] == -42.88
    assert rows[1]["longitude"] is None
    assert rows[1]["latitude"] is None
    assert str(rows[0]["when"]) == "2013-03-30 00:16:00"
    assert rows[1]["when"] is None
    assert rows[0]["speed_zone"] == "060"


def test_zip_source_reads_the_named_member():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("License.txt", "CC BY")
        z.writestr("a.csv", "Id,Name\n1,A\n")
        z.writestr("b.csv", "Id,Name\n2,B\n")
    data = buf.getvalue()
    fields = [Field("id", "Id", "integer"), Field("name", "Name")]
    src = Source(adapter="ckan-resource", url="https://example.gov.au/data", member="b.csv")
    t = normalise(
        make_dataset(fields, source=src),
        make_manifest(data, filename="x.zip", encoding="zip"),
        data,
    )
    assert t.table.to_pylist() == [{"id": 2, "name": "B"}]
    with pytest.raises(NormaliseError, match="holds 2 data files"):
        normalise(make_dataset(fields), make_manifest(data, filename="x.zip", encoding="zip"), data)


def test_tab_delimited_source_reads_by_the_declared_delimiter():
    data = b"\xef\xbb\xbfId\tName, Ltd\tWhen\r\n1\t  Alpha, Beta Ltd\t03/02/2024\r\n"
    fields = [
        Field("id", "Id", "integer"),
        Field("name", "Name, Ltd"),
        Field("when", "When", "date", date_format="%d/%m/%Y"),
    ]
    src = Source(adapter="ckan-resource", url="https://example.gov.au/data", delimiter="tab")
    t = normalise(make_dataset(fields, source=src), make_manifest(data, encoding="utf-8-sig"), data)
    assert t.table.to_pylist() == [
        {"id": 1, "name": "Alpha, Beta Ltd", "when": t.table[2][0].as_py()}
    ]
    assert str(t.table[2][0].as_py()) == "2024-02-03"
    # Without the declaration the commas in the header split it and the register's columns are absent.
    with pytest.raises(NormaliseError, match="absent upstream"):
        normalise(make_dataset(fields), make_manifest(data, encoding="utf-8-sig"), data)


def test_a_csv_with_a_preamble_starts_at_the_header_row_and_drops_separator_only_rows():
    data = (
        b"\xef\xbb\xbfTABLE A2\nTitle,Rate,Other\n\n\nSeries ID,ARBAX,ARBAY\n23-Jan-1990,17.00 to 17.50\n"
        b"15-Feb-1990,16.50,1\n,,\n,,\n"
    )
    fields = [
        Field("date", "Series ID", "date", date_format="%d-%b-%Y"),
        Field("rate", "ARBAX", "number", null_values=("17.00 to 17.50",)),
        Field("other", "ARBAY", "integer"),
    ]
    src = Source(adapter="file", url="https://example.gov.au/a2.csv", header_row=5)
    t = normalise(make_dataset(fields, source=src), make_manifest(data, encoding="utf-8-sig"), data)
    rows = t.table.to_pylist()
    assert [str(r["date"]) for r in rows] == ["1990-01-23", "1990-02-15"]
    assert [r["rate"] for r in rows] == [None, 16.5]
    # The first row is short of a cell, as a spreadsheet export leaves it; the cell is null and
    # the padding is counted so a truncated download shows up.
    assert [r["other"] for r in rows] == [None, 1]
    assert t.short_rows == 1


def test_thousands_separators_are_read_as_the_figure_they_write():
    data = b'Id,Count,Amount,Code\n1,"19,918","1,234.5","1,2"\n2,7,8.5,x\n'
    fields = [
        Field("id", "Id", "integer"),
        Field("count", "Count", "integer"),
        Field("amount", "Amount", "number"),
        Field("code", "Code"),
    ]
    t = normalise(make_dataset(fields), make_manifest(data), data)
    rows = t.table.to_pylist()
    assert [r["count"] for r in rows] == [19918, 7]
    assert [r["amount"] for r in rows] == [1234.5, 8.5]
    # A string keeps its commas, and "1,2" is not a grouped figure.
    assert [r["code"] for r in rows] == ["1,2", "x"]


def test_a_date_may_come_in_any_of_several_declared_formats():
    data = b"Id,When\n1,2023-07-01\n2,1/07/2023 12:00:00 AM\n3,\n"
    fields = [
        Field("id", "Id", "integer"),
        Field("when", "When", "date", date_format="%Y-%m-%d|%d/%m/%Y %I:%M:%S %p"),
    ]
    t = normalise(make_dataset(fields), make_manifest(data), data)
    assert [str(r["when"]) for r in t.table.to_pylist()] == ["2023-07-01", "2023-07-01", "None"]
    bad = b"Id,When\n1,July 2023\n"
    with pytest.raises(NormaliseError, match="none of the formats"):
        normalise(make_dataset(fields), make_manifest(bad), bad)


def test_a_column_the_register_omits_on_purpose_is_recorded_and_not_held():
    ds = make_dataset(
        FIELDS, suppression=("<5",), omit={"Extra": "A vendor's series, not the publisher's."}
    )
    t = normalise(ds, make_manifest(CSV, encoding="utf-8-sig"), CSV)
    assert t.unknown_columns == []
    assert t.omitted_columns == ["Extra"]


def test_a_header_with_trailing_or_non_breaking_spaces_is_still_read_as_text():
    t = read_csv("Supplier \u00a0,Value \n001,2\n,\n".encode(), "utf-8")
    assert [c.type for c in t.columns] == [pa.string(), pa.string()]
    assert t.column(0).to_pylist() == ["001"]


def test_a_legacy_xls_workbook_reads_like_an_xlsx_one():
    data = (Path(__file__).parent / "fixtures" / "legacy.xls").read_bytes()
    t = read_xlsx(xls_to_xlsx(data), "", 1)
    assert t.column_names == ["Name", "Count", "When"]
    assert t.column("Count").to_pylist() == ["3", "4"]
    assert t.column("When").to_pylist()[0].startswith(dt.date(2024, 7, 1).isoformat())
