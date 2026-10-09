import datetime as dt
import io
from dataclasses import replace
from typing import TYPE_CHECKING, ClassVar, cast

import pyarrow as pa
import pytest
import xlsxwriter
import yaml

from publicdata import fetch, normalise
from publicdata import normalise as nz
from publicdata.register import Field, RegisterError, load

from .conftest import ROOT, make_dataset, make_manifest

if TYPE_CHECKING:
    from pathlib import Path

    import requests

    from publicdata.register import Dataset, Wide


def workbook() -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    wb.add_worksheet("Notes").write(0, 0, "ignore me")
    ws = wb.add_worksheet("Data")
    ws.write(0, 0, "Road deaths")
    ws.write(1, 0, "(Data current to June 2026)")
    for c, h in enumerate(["Crash ID", "Speed Limit", "Bus", "Date", "Share"]):
        ws.write(3, c, h)
    rows: list[list[object]] = [
        ["1199208270377", 100, "No", dt.datetime(2026, 6, 1), 0.25],
        ["4200909170077", -9, "-9", dt.datetime(2026, 6, 2, 13, 5), 1.0],
        [None, None, None, None, None],
        ["6200203280014", 60, "Unknown", dt.datetime(2026, 6, 3), 2.5],
    ]
    fmt = wb.add_format({"num_format": "yyyy-mm-dd hh:mm"})
    for r, row in enumerate(rows, start=4):
        for c, v in enumerate(row):
            if isinstance(v, dt.datetime):
                ws.write_datetime(r, c, v, fmt)
            elif v is not None:
                ws.write(r, c, v)
    wb.close()
    return buf.getvalue()


def test_xlsx_reads_the_named_sheet_from_its_header_row_as_text() -> None:
    t = normalise.read_xlsx(workbook(), "Data", 4)
    assert t.column_names == ["Crash ID", "Speed Limit", "Bus", "Date", "Share"]
    assert t.num_rows == 3  # the blank row is dropped
    assert t.column("Crash ID").to_pylist()[0] == "1199208270377"
    assert t.column("Speed Limit").to_pylist() == ["100", "-9", "60"]
    assert t.column("Date").to_pylist() == ["2026-06-01", "2026-06-02 13:05:00", "2026-06-03"]
    assert t.column("Share").to_pylist() == ["0.25", "1", "2.5"]
    with pytest.raises(normalise.NormaliseError, match="not in workbook"):
        normalise.read_xlsx(workbook(), "Missing", 4)


def test_declared_unknown_markers_become_null_and_others_still_fail() -> None:
    t = normalise.read_xlsx(workbook(), "Data", 4)
    speed, _ = normalise.convert(
        t.column("Speed Limit"),
        Field("speed_limit", "Speed Limit", "integer", null_values=("-9",)),
        (),
    )
    assert speed.to_pylist() == [100, None, 60]
    bus = Field("bus", "Bus", "boolean", null_values=("Unknown", "-9"))
    assert normalise.convert(t.column("Bus"), bus, ())[0].to_pylist() == [False, None, None]
    with pytest.raises(normalise.NormaliseError, match="outside true/false"):
        normalise.convert(t.column("Bus"), Field("bus", "Bus", "boolean"), ())
    assert normalise.convert(pa.chunked_array([["-9"]]), Field("a", "a", "integer"), ())[
        0
    ].to_pylist() == [-9]


class Stub:
    def __init__(self, portal_id: str, licence_id: str = "CC-BY-4.0", slug: str = "x") -> None:
        self.slug = slug
        self.licence = type("L", (), {"id": licence_id, "portal_id": portal_id})()
        self.source = type("S", (), {"portal": ""})()


def stub(portal_id: str) -> Dataset:
    # Stands in for the three attributes check_licence reads of an entry.
    return cast("Dataset", Stub(portal_id))


def test_an_exact_portal_licence_id_is_compared_as_stated() -> None:
    fetch.check_licence(stub("cc-by"), {"id": "cc-by"})
    with pytest.raises(fetch.LicenceDrift, match="expects 'cc-by'"):
        fetch.check_licence(stub("cc-by"), {"id": "cc-by-nd"})
    with pytest.raises(fetch.LicenceDrift):
        fetch.check_licence(stub(""), {"id": "cc-by"})  # no portal named, so no version to read


def test_a_file_replaced_inside_one_resource_is_dated_by_the_resource_metadata(
    tmp_path: Path,
) -> None:
    ds = next(d for d in load(ROOT / "register") if d.slug == "au-road-deaths")
    body = workbook()

    class R:
        def __init__(self, j: object = None, content: bytes = b"") -> None:
            self._j, self.content, self.status_code = j, content, 200
            self.headers: dict[str, str] = {}

        def json(self) -> object:
            return self._j

        def raise_for_status(self) -> None:
            pass

    res = {
        "id": ds.source.resource,
        "url": "https://blob.example/bitre_fatalities_jun2026.xlsx",
        "name": "Fatalities 2026-06",
        "created": "2025-03-12T02:06:49",
        "last_modified": None,
        "metadata_modified": "2026-08-07T01:18:27",
    }
    pkg = {
        "success": True,
        "result": {
            "id": "p",
            "name": ds.source.package,
            "resources": [res],
            "metadata_modified": "2026-08-07T01:18:27",
            "license_id": "cc-by",
        },
    }

    class S:
        headers: ClassVar[dict[str, str]] = {}

        def get(
            self,
            url: str,
            params: object = None,
            timeout: float | None = None,
            *,
            allow_redirects: bool | None = None,
        ) -> R:
            return R(pkg) if "package_show" in url else R(content=body)

    session = cast("requests.Session", S())
    data, m, _ = fetch.ckan_resource(ds, tmp_path, session=session)
    assert data == body
    assert m.version == "2026-08-07"
    assert m.encoding == "xlsx"


def wide_workbook() -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Data")
    fmt = wb.add_format({"num_format": "mmm-yy"})
    for c, h in enumerate(["LGA", "Offence category", "Subcategory"]):
        ws.write(0, c, h)
    ws.write_datetime(0, 3, dt.datetime(1995, 1, 1), fmt)
    ws.write_datetime(0, 4, dt.datetime(1995, 2, 1), fmt)
    ws.write(0, 5, "Notes")
    ws.write_row(1, 0, ["Albury", "Assault", "Domestic violence related assault", 7, 5, "x"])
    ws.write_row(2, 0, ["Albury", "Arson", "", 0, 2, ""])
    wb.close()
    return buf.getvalue()


def test_a_wide_table_of_dated_columns_unpivots_to_one_row_per_cell() -> None:
    ds = next(d for d in load(ROOT / "register") if d.slug == "nsw-recorded-crime-by-lga")
    data = wide_workbook()
    t = normalise.normalise(
        ds, make_manifest(data, filename="RCI_offencebymonth.xlsm", encoding="xlsx"), data
    )
    rows = t.table.to_pylist()
    assert [(r["offence_category"], r["month"].isoformat(), r["incidents"]) for r in rows] == [
        ("Assault", "1995-01-01", 7),
        ("Assault", "1995-02-01", 5),
        ("Arson", "1995-01-01", 0),
        ("Arson", "1995-02-01", 2),
    ]
    assert rows[2]["subcategory"] is None
    assert t.unknown_columns == ["Notes"]  # a new column that is not a month is held, not published

    with pytest.raises(nz.NormaliseError, match="no dated columns"):
        nz.unpivot(
            pa.table(
                {"LGA": ["a"], "Offence category": ["b"], "Subcategory": [""], "Notes": ["x"]}
            ),
            ds,
        )


def test_unpivot_needs_one_header_field_and_one_cell_field(tmp_path: Path) -> None:
    raw = yaml.safe_load((ROOT / "register" / "nsw-recorded-crime-by-lga.yaml").read_text())
    raw["fields"] = [f for f in raw["fields"] if f["source"] != "(cell)"]
    raw["key"] = ["lga", "offence_category", "subcategory", "month"]
    (tmp_path / "x.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(RegisterError, match="exactly one field with source '\\(cell\\)'"):
        load(tmp_path)


def test_a_later_file_date_than_the_portal_dates_the_version(tmp_path: Path) -> None:
    ds = next(d for d in load(ROOT / "register") if d.slug == "nsw-recorded-crime-by-lga")
    body = wide_workbook()
    res = {
        "id": ds.source.resource,
        "url": "https://blob.example/RCI_offencebymonth.xlsm",
        "last_modified": None,
        "metadata_modified": "2026-04-23T03:08:32",
    }
    pkg = {
        "success": True,
        "result": {
            "id": "p",
            "name": ds.source.package,
            "resources": [res],
            "metadata_modified": "2026-04-28T06:29:34",
            "license_id": "cc-by",
        },
    }

    class R:
        def __init__(
            self,
            j: object = None,
            content: bytes = b"",
            headers: dict[str, str] | None = None,
        ) -> None:
            self._j, self.content, self.headers, self.status_code = j, content, headers or {}, 200

        def json(self) -> object:
            return self._j

        def raise_for_status(self) -> None:
            pass

    class S:
        headers: ClassVar[dict[str, str]] = {}

        def get(
            self,
            url: str,
            params: object = None,
            timeout: float | None = None,
            *,
            allow_redirects: bool | None = None,
        ) -> R:
            if "package_show" in url:
                return R(pkg)
            return R(content=body, headers={"Last-Modified": "Tue, 15 Sep 2026 17:57:17 GMT"})

    session = cast("requests.Session", S())
    _, m, _ = fetch.ckan_resource(ds, tmp_path, session=session)
    assert m.version == "2026-09-16"  # Brisbane date of the file's own timestamp


def presentation_workbook() -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    for name, base in (("1 bedroom flat", 100), ("House", 300)):
        ws = wb.add_worksheet(name)
        ws.write(0, 0, "Moving annual median rent by suburb")
        ws.write(1, 0, name)
        for j, period in enumerate(["Mar 2000", "Mar 2000", "Jun 2000", "Jun 2000"]):
            ws.write(1, 2 + j, period)
            ws.write(2, 2 + j, ["Count", "Median", "Count", "Median"][j])
        ws.write(2, 6, "Share")
        body: list[list[object]] = [
            ["Inner", "Carlton", 10, base, 11, base + 5, 0.5],
            [None, "Fitzroy", "-", "-", 12, base + 10, 0.4],
            [None, None, None, None, None, None, None],
            ["Note: a dash means too few lettings", None, None, None, None, None, None],
            ["Outer", "Werribee", 20, base - 50, 21, base - 45, 0.1],
        ]
        for i, row in enumerate(body, start=3):
            for j, v in enumerate(row):
                if v is not None:
                    ws.write(i, j, v)
    wb.close()
    return buf.getvalue()


def wide_dataset() -> Dataset:
    fields = [
        Field("property_type", "(sheet)"),
        Field("region", "(row header 1)"),
        Field("suburb", "(row header 2)"),
        Field("year_ending", "(column header 1)", "date", date_format="%b %Y"),
        Field("lettings", "(cell: Count)", "integer", null_values=("-",)),
        Field("median", "(cell: Median)", "integer", null_values=("-",)),
    ]
    # Empty matches read as none, as a parsed entry's do.
    wide: Wide = {
        "sheets": ["1 bedroom flat", "House"],
        "header_row": 2,
        "header_rows": 2,
        "first_column": 1,
        "row_headers": 2,
        "fill_down": [1],
        "column_match": "",
        "sheet_match": "",
    }
    return make_dataset(fields, wide=wide)


def test_a_presentation_table_reads_one_row_per_group_of_cells() -> None:
    data = presentation_workbook()
    t = normalise.normalise(
        wide_dataset(), make_manifest(data, filename="rents.xlsx", encoding="xlsx"), data
    )
    rows = [
        (
            r["property_type"],
            r["region"],
            r["suburb"],
            r["year_ending"].isoformat(),
            r["lettings"],
            r["median"],
        )
        for r in t.table.to_pylist()
    ]
    assert rows[:4] == [
        ("1 bedroom flat", "Inner", "Carlton", "2000-03-01", 10, 100),
        ("1 bedroom flat", "Inner", "Carlton", "2000-06-01", 11, 105),
        # A blank row header under a filled one is the same region; a dash is no figure.
        ("1 bedroom flat", "Inner", "Fitzroy", "2000-03-01", None, None),
        ("1 bedroom flat", "Inner", "Fitzroy", "2000-06-01", 12, 110),
    ]
    # The note row carries no data and is skipped; the next sheet follows the first.
    assert rows[4] == ("1 bedroom flat", "Outer", "Werribee", "2000-03-01", 20, 50)
    assert rows[6][0] == "House"
    assert len(rows) == 12
    # A header the register does not name is held, not published.
    assert t.unknown_columns == ["Share"]
    ds = wide_dataset()
    fields = (*ds.fields[:-1], Field("median", "(cell: Mean)", "integer"))
    with pytest.raises(normalise.NormaliseError, match=r"no columns headed \['Mean'\]"):
        normalise.normalise(
            replace(ds, fields=fields),
            make_manifest(data, filename="rents.xlsx", encoding="xlsx"),
            data,
        )


def test_a_wide_table_names_every_value_it_reads(tmp_path: Path) -> None:
    base = yaml.safe_load((ROOT / "register" / "nsw-recorded-crime-by-lga.yaml").read_text())
    base.pop("unpivot")
    base.pop("key")
    base.pop("partition_by")
    base["wide"] = {"header_row": 2, "header_rows": 2, "row_headers": 1}
    cases = [
        ([{"name": "a", "source": "(row header 1)"}], "one field with source"),
        (
            [{"name": "a", "source": "(row header 2)"}, {"name": "v", "source": "(cell)"}],
            "not in the wide table",
        ),
        (
            # The last header row names the fields here, so there is one header level to read.
            [
                {"name": "a", "source": "(column header 2)"},
                {"name": "v", "source": "(cell: Count)"},
            ],
            "not in the wide table",
        ),
        (
            [{"name": "a", "source": "(cell)"}, {"name": "v", "source": "(cell: Count)"}],
            "one field with source",
        ),
        (
            [
                {"name": "a", "source": "(column header 1)"},
                {"name": "v", "source": "(cell: Count)"},
                {"name": "w", "source": "(cell: Count)"},
            ],
            "read the same source",
        ),
    ]
    for fields, msg in cases:
        raw = dict(base, fields=fields)
        (tmp_path / "x.yaml").write_text(yaml.safe_dump(raw))
        with pytest.raises(RegisterError, match=msg):
            load(tmp_path)
    raw = dict(
        base,
        fields=[
            {"name": "a", "source": "(column header 1)"},
            {"name": "v", "source": "(cell: Count)"},
        ],
    )
    raw["unpivot"] = {"headers": "date"}
    (tmp_path / "x.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(RegisterError, match="not both"):
        load(tmp_path)


def test_a_wide_table_reads_only_the_columns_its_pattern_names() -> None:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Sheet1")
    ws.write(0, 25, "Change")
    for j, h in enumerate(["Locality", "2015", "", "2016", "", "Prelim", "", "24-25"]):
        if h:
            ws.write(1, j, h)
    ws.write(2, 5, "2026")
    for i, row in enumerate(
        [
            ["ABBOTSFORD", 925000, " ", 1187500, " ", 1555000, "^", 7],
            ["YEA", "NA", " ", 305000, " ", 700000, "^", 22],
        ],
        start=5,
    ):
        for j, v in enumerate(row):
            ws.write(i, j, v)
    wb.close()
    data = buf.getvalue()
    fields = [
        Field("locality", "(row header 1)"),
        Field("year", "(column header 1)", "integer"),
        Field("median", "(cell)", "integer", null_values=("NA",)),
    ]
    wide: Wide = {
        "sheets": [],
        "header_row": 2,
        "header_rows": 1,
        "first_column": 1,
        "row_headers": 1,
        "fill_down": [],
        "column_match": r"^\d{4}$",
        "sheet_match": "",
    }
    t = normalise.normalise(
        make_dataset(fields, wide=wide),
        make_manifest(data, filename="h.xlsx", encoding="xlsx"),
        data,
    )
    rows = [(r["locality"], r["year"], r["median"]) for r in t.table.to_pylist()]
    assert rows == [
        ("ABBOTSFORD", 2015, 925000),
        ("ABBOTSFORD", 2016, 1187500),
        ("YEA", 2015, None),
        ("YEA", 2016, 305000),
    ]
    # The preliminary year and the derived change are held as columns the register does not read.
    assert t.unknown_columns == ["Prelim", "24-25"]
