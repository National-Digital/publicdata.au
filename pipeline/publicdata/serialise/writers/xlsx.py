from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

import xlsxwriter

from publicdata.serialise import _fixed_zip, dumps, field_rows

if TYPE_CHECKING:
    from pathlib import Path

    from publicdata.normalise import Table
    from publicdata.provenance import Header


def write_xlsx(tbl: Table, header: Header, path: Path) -> None:  # noqa: C901, PLR0912 - one branch per cell type
    """One workbook: records, fields and publicdata sheets. Dates are real Excel dates."""
    ds = tbl.dataset
    when = dt.datetime.fromisoformat(header["version"] + "T00:00:00")
    wb = xlsxwriter.Workbook(
        str(path), {"constant_memory": True, "default_date_format": "yyyy-mm-dd"}
    )
    wb.set_properties(
        {
            "title": ds.title,
            "author": ds.publisher.name,
            "company": header["operator"]["name"],
            "comments": header["attribution"],
            "hyperlink_base": header["url"],
            "created": when,
        }
    )
    bold = wb.add_format({"bold": True})
    date_fmt = wb.add_format({"num_format": "yyyy-mm-dd"})
    dt_fmt = wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
    ws = wb.add_worksheet("records")
    names = list(tbl.table.column_names)
    types = {f.name: f.type for f in ds.fields}
    ws.write_row(0, 0, names, bold)
    ws.freeze_panes(1, 0)
    r = 1
    for b in tbl.table.to_batches(20_000):
        for row in b.to_pylist():
            for c, n in enumerate(names):
                v = row[n]
                if v is None:
                    continue
                t = types.get(n)
                if t == "date":
                    ws.write_datetime(r, c, dt.datetime(v.year, v.month, v.day), date_fmt)  # noqa: DTZ001 - an Excel date has no zone
                elif t == "datetime":
                    ws.write_datetime(r, c, v.replace(tzinfo=None), dt_fmt)
                elif t == "boolean":
                    ws.write_boolean(r, c, bool(v))
                elif isinstance(v, list):
                    if v:
                        ws.write_string(r, c, ";".join(v))
                elif isinstance(v, (int, float)):
                    ws.write_number(r, c, v)
                else:
                    ws.write_string(r, c, str(v))
            r += 1
    fs = wb.add_worksheet("fields")
    fs.write_row(0, 0, ["name", "type", "publisher_header", "description"], bold)
    for i, f in enumerate(field_rows(tbl), 1):
        fs.write_row(i, 0, f)
    ps = wb.add_worksheet("publicdata")
    ps.write_row(0, 0, ["key", "value"], bold)
    for i, (k, v) in enumerate(header.items(), 1):
        ps.write_row(i, 0, [k, v if isinstance(v, str) else dumps(v)])
    wb.close()
    _fixed_zip(path, (when.year, when.month, when.day, 0, 0, 0))
