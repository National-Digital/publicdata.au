import dataclasses
import json

import pytest

from publicdata import fetch as f
from publicdata.register import Field, Licence, Source

from .conftest import make_dataset

CSV = b"a,b\n1,x\n2,y\n"


class Resp:
    def __init__(self, body, status=200, ctype="text/csv", headers=None):
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status_code = status
        self.headers = {"Content-Type": ctype if isinstance(body, bytes) else "application/json"}
        self.headers.update(headers or {})

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(self.status_code)

    def json(self):
        return json.loads(self.content)


class Portal:
    def __init__(self, routes):
        self.routes, self.headers, self.asked = routes, {}, []

    def get(self, url, params=None, **kw):
        self.asked.append(url)
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                return answer.pop(0) if isinstance(answer, list) else answer
        msg = f"unexpected {url}"
        raise AssertionError(msg)


def ds_for(adapter, portal, package, licence="CC-BY-4.0", resource=""):
    return make_dataset(
        [Field("a", "a", "integer"), Field("b", "b")],
        licence=Licence(licence, "https://example.gov.au/", "Example, sourced {sourced}."),
        source=Source(
            adapter=adapter,
            url=f"{portal}/x",
            portal=portal,
            package=package,
            resource=resource,
        ),
    )


def run(adapter, ds, routes, tmp_path, monkeypatch):
    s = Portal(routes)
    monkeypatch.setattr(f, "_session", lambda session: s)
    data, m, lic = f.ADAPTERS[adapter](ds, tmp_path)
    f.check_licence(ds, lic)
    return data, m, s


def test_socrata_exports_the_whole_view_and_reads_its_licence(tmp_path, monkeypatch):
    base = "https://www.data.act.gov.au"
    ds = ds_for("socrata", base, "426s-vdu4")
    view = {
        "name": "Traffic speed camera locations",
        "rowsUpdatedAt": 1787290543,
        "viewLastModified": 1787290542,
        "licenseId": "CC_40_BY",
        "license": {"name": "Creative Commons Attribution 4.0 International"},
    }
    data, m, s = run(
        "socrata",
        ds,
        {
            f"{base}/api/views/426s-vdu4.json": Resp(view),
            f"{base}/api/views/426s-vdu4/rows.csv": Resp(CSV),
        },
        tmp_path,
        monkeypatch,
    )
    assert data == CSV and m.filename == "426s-vdu4.csv" and m.version == "2026-08-21"
    assert m.licence["id"] == "CC-BY-4.0" and m.licence["stated"] == "CC_40_BY"
    assert "accessType=DOWNLOAD" in s.asked[-1]


def test_opendatasoft_exports_comma_separated_csv(tmp_path, monkeypatch):
    base = "https://data.brisbane.qld.gov.au"
    ds = ds_for("opendatasoft", base, "search-terms")
    meta = {
        "metas": {
            "default": {
                "title": "Search terms",
                "license": "CC BY 4.0",
                "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "modified": "2026-09-16T04:12:52+00:00",
                "data_processed": "2026-09-15T20:00:00+00:00",
            }
        }
    }
    root = f"{base}/api/explore/v2.1/catalog/datasets/search-terms"
    data, m, s = run(
        "opendatasoft",
        ds,
        {f"{root}/exports/csv": Resp(CSV), root: Resp(meta)},
        tmp_path,
        monkeypatch,
    )
    # 20:00 UTC on the 15th is the 16th in Brisbane.
    assert m.version == "2026-09-16" and m.licence["id"] == "CC-BY-4.0"
    assert "delimiter=%2C" in s.asked[-1] and "with_bom=false" in s.asked[-1]


def test_arcgis_hub_waits_for_its_export_and_dates_it_by_the_layer_edit(tmp_path, monkeypatch):
    base = "https://data-goldcoast.opendata.arcgis.com"
    ds = ds_for("arcgis-hub", base, "58e6", licence="CC-BY-3.0-AU", resource="2")
    ds = dataclasses.replace(ds, licence=Licence("CC-BY-3.0-AU", "e", "a", "CC-BY-3.0"))
    service = "https://services.arcgis.com/x/arcgis/rest/services/Traffic_Count/FeatureServer"
    item = {
        "properties": {
            "title": "Traffic Count",
            "license": "CC-BY-3.0",
            "modified": 1650929629000,
            "url": service,
        }
    }
    monkeypatch.setattr(f, "EXPORT_WAIT_SECONDS", 0)
    data, m, s = run(
        "arcgis-hub",
        ds,
        {
            f"{base}/api/search/v1/collections/dataset/items/58e6": Resp(item),
            f"{service}/2": Resp({"editingInfo": {"lastEditDate": 1790540276483}}),
            f"{base}/api/download/v1/items/58e6/csv": [
                Resp(b'{"status":"Pending"}', 202, "application/json"),
                Resp(CSV),
            ],
        },
        tmp_path,
        monkeypatch,
    )
    assert data == CSV and m.version == "2026-09-28" and m.filename == "58e6_2.csv"
    assert s.asked[-1].endswith("layers=2") and m.source["service"] == service


def test_an_export_still_building_after_the_waits_is_refused(tmp_path, monkeypatch):
    base = "https://data.brisbane.qld.gov.au"
    ds = ds_for("opendatasoft", base, "slow")
    root = f"{base}/api/explore/v2.1/catalog/datasets/slow"
    monkeypatch.setattr(f, "EXPORT_WAIT_SECONDS", 0)
    meta = {"metas": {"default": {"license": "CC BY 4.0", "modified": "2026-09-16T04:12:52+00:00"}}}
    with pytest.raises(f.FetchError, match="202"):
        run(
            "opendatasoft",
            ds,
            {f"{root}/exports/csv": Resp(b"{}", 202, "application/json"), root: Resp(meta)},
            tmp_path,
            monkeypatch,
        )


def test_an_export_whose_portal_states_no_change_date_is_a_failed_fetch(tmp_path, monkeypatch):
    base = "https://data-goldcoast.opendata.arcgis.com"
    ds = ds_for("arcgis-hub", base, "58e6")
    item = {"properties": {"title": "No dates", "license": "CC-BY-4.0"}}
    with pytest.raises(f.FetchError, match="no change date"):
        run(
            "arcgis-hub",
            ds,
            {f"{base}/api/search/v1/collections/dataset/items/58e6": Resp(item)},
            tmp_path,
            monkeypatch,
        )
    base = "https://data.brisbane.qld.gov.au"
    ds = ds_for("opendatasoft", base, "undated")
    root = f"{base}/api/explore/v2.1/catalog/datasets/undated"
    with pytest.raises(f.FetchError, match="no change date"):
        run(
            "opendatasoft",
            ds,
            {root: Resp({"metas": {"default": {"license": "CC BY 4.0"}}})},
            tmp_path,
            monkeypatch,
        )


def test_an_entry_that_names_the_portals_own_licence_code_is_checked_against_it(
    tmp_path, monkeypatch
):
    base = "https://www.data.act.gov.au"
    ds = ds_for("socrata", base, "426s-vdu4")
    ds = dataclasses.replace(ds, licence=Licence("CC-BY-4.0", "e", "a", "CC_40_BY"))

    def view(code):
        return {
            "rowsUpdatedAt": 1787290543,
            "licenseId": code,
            "license": {"name": "Creative Commons Attribution 4.0 International"},
        }

    routes = {
        f"{base}/api/views/426s-vdu4.json": Resp(view("CC_40_BY")),
        f"{base}/api/views/426s-vdu4/rows.csv": Resp(CSV),
    }
    _, m, _ = run("socrata", ds, routes, tmp_path, monkeypatch)
    assert m.licence["id"] == "CC_40_BY"
    routes[f"{base}/api/views/426s-vdu4.json"] = Resp(view("CC_40_BY_SA"))
    with pytest.raises(f.LicenceDrift):
        run("socrata", ds, routes, tmp_path, monkeypatch)


def test_a_worked_out_licence_id_is_compared_as_it_stands():
    ds = ds_for("socrata", "https://www.data.act.gov.au", "x", licence="CC-BY-4.0")
    bare = {
        "id": "CC-BY",
        "stated": "CC_BY",
        "normalised": "CC-BY",
        "title": "Creative Commons Attribution",
    }
    # The CKAN alias would read a bare CC-BY as 3.0 AU; here it stays unversioned and is reported.
    with pytest.raises(f.LicenceDrift, match="CC-BY"):
        f.check_licence(ds, bare)
    f.check_licence(ds, bare | {"id": "CC-BY-4.0", "normalised": "CC-BY-4.0"})
    f.check_licence(
        ds_for("socrata", "https://www.data.act.gov.au", "x", licence="CC-BY-3.0-AU"),
        bare | {"id": "CC-BY-3.0-AU", "normalised": "CC-BY-3.0-AU"},
    )
    with pytest.raises(f.LicenceDrift):
        f.check_licence(
            ds_for("socrata", "https://www.data.act.gov.au", "x", licence="CC-BY-3.0-AU"), bare
        )


PAGE = b"<html><p>All  material is provided under a\n <b>Creative Commons</b> Attribution 4.0 licence.</p></html>"


def file_ds(statement="material is provided under a Creative Commons Attribution 4.0 licence"):
    return make_dataset(
        [Field("a", "a", "integer"), Field("b", "b")],
        licence=Licence(
            "CC-BY-4.0",
            "https://example.gov.au/copyright",
            "Example, sourced {sourced}.",
            "",
            statement,
        ),
        source=Source(adapter="file", url="https://example.gov.au/files/t.csv"),
    )


def test_a_file_source_is_dated_by_last_modified_and_licensed_by_the_pages_words(
    tmp_path, monkeypatch
):
    routes = {
        "https://example.gov.au/copyright": Resp(PAGE, ctype="text/html"),
        "https://example.gov.au/files/t.csv": Resp(
            CSV, headers={"Last-Modified": "Tue, 29 Sep 2026 23:00:37 GMT"}
        ),
    }
    data, m, s = run("file", file_ds(), routes, tmp_path, monkeypatch)
    assert data == CSV and m.version == "2026-09-30" and m.filename == "t.csv"
    assert m.licence["id"] == "CC-BY-4.0" and m.notes == []
    assert s.asked[0] == "https://example.gov.au/copyright"


def test_a_file_whose_server_states_no_date_is_dated_by_the_fetch_and_says_so(
    tmp_path, monkeypatch
):
    routes = {
        "https://example.gov.au/copyright": Resp(PAGE, ctype="text/html"),
        "https://example.gov.au/files/t.csv": Resp(CSV),
    }
    _, m, _ = run("file", file_ds(), routes, tmp_path, monkeypatch)
    assert m.notes == [f.FILE_NOTE]


def test_a_licence_page_that_lost_its_grant_stops_the_fetch(tmp_path, monkeypatch):
    routes = {
        "https://example.gov.au/copyright": Resp(b"<p>All rights reserved.</p>", ctype="text/html"),
        "https://example.gov.au/files/t.csv": Resp(CSV),
    }
    with pytest.raises(f.LicenceDrift, match="no longer says"):
        run("file", file_ds(), routes, tmp_path, monkeypatch)


def manual_portal(modified="2026-09-01T00:00:00"):
    res = {"id": "r", "name": "T", "format": "CSV", "url": "https://p.example/t.csv"}
    res["last_modified"] = modified
    return {
        "success": True,
        "result": {
            "id": "p",
            "name": "p",
            "license_id": "cc-by",
            "license_title": "CC BY",
            "license_url": "",
            "metadata_modified": modified,
            "resources": [res],
        },
    }


def manual_ds():
    return make_dataset(
        [Field("a", "a", "integer"), Field("b", "b")],
        licence=Licence(
            "CC-BY-4.0", "https://p.example/dataset/p", "Example, sourced {sourced}.", "cc-by"
        ),
        source=Source(
            adapter="ckan-resource",
            url="https://p.example/dataset/p",
            portal="https://p.example",
            package="p",
            resource="r",
            manual=True,
        ),
    )


def run_manual(modified, store_dir):
    s = Portal({"https://p.example/api/3/action/package_show": Resp(manual_portal(modified))})
    data, m, lic = f.ckan_resource(manual_ds(), store_dir, session=s)
    f.check_licence(manual_ds(), lic)
    return data, m, s


def test_a_manual_source_is_never_downloaded_and_a_new_one_is_due(tmp_path, monkeypatch):
    monkeypatch.setattr(f, "MANUAL", {})
    with pytest.raises(f.ManualDue, match="download https://p.example/t.csv"):
        run_manual("2026-09-01T00:00:00", tmp_path)


def test_a_manual_source_reads_the_downloaded_file_then_waits_for_the_record_to_change(
    tmp_path, monkeypatch
):
    from publicdata import store

    got = tmp_path / "t.csv"
    got.write_bytes(CSV)
    monkeypatch.setattr(f, "MANUAL", {"t": got})
    data, m, s = run_manual("2026-09-01T00:00:00", tmp_path / "store")
    assert data == CSV and m.version == "2026-09-01"
    assert s.asked == ["https://p.example/api/3/action/package_show"]
    store.write(tmp_path / "store", m, data)

    monkeypatch.setattr(f, "MANUAL", {})
    data, _, s = run_manual("2026-09-01T00:00:00", tmp_path / "store")
    assert data is None and s.asked == ["https://p.example/api/3/action/package_show"]
    with pytest.raises(f.ManualDue):
        run_manual("2026-10-01T00:00:00", tmp_path / "store")


STATIONS = [
    [
        "station_no",
        "station_name",
        "station_latitude",
        "station_longitude",
        "DATA_OWNER_NAME",
        "FullStorageVolume",
    ],
    ["212243", "Warragamba Dam", "-33.9", "150.6", "NSW - Water NSW", "2027000"],
    ["143001", "Wivenhoe", "-27.4", "152.6", "QLD - Seqwater", ""],
]
SERIES = [
    ["station_no", "ts_id", "ts_name", "from", "to"],
    [
        "212243",
        "1",
        "DMQaQc.Merged.DailyMean.24HR",
        "2026-01-01T00:00:00.000+10:00",
        "2026-01-02T00:00:00.000+10:00",
    ],
    [
        "143001",
        "2",
        "DMQaQc.Merged.DailyMean.24HR",
        "2026-01-02T00:00:00.000+10:00",
        "2026-01-02T00:00:00.000+10:00",
    ],
    ["999", "3", "DMQaQc.Merged.DailyMean.24HR", "", ""],
]
VALUES = [
    {
        "station_no": "212243",
        "ts_id": "1",
        "rows": "2",
        "data": [
            ["2026-01-02T00:00:00.000+10:00", 1931364.77, 140],
            ["2026-01-01T00:00:00.000+10:00", 1930000.0, 90],
        ],
    },
    {
        "station_no": "143001",
        "ts_id": "2",
        "rows": "1",
        "data": [["2026-01-02T00:00:00.000+10:00", 900000.5, 10]],
    },
]


def kiwis_ds(resource):
    return make_dataset(
        [Field("station_no", "station_no")],
        licence=Licence(
            "CC-BY-3.0-AU",
            "https://example.gov.au/waterdata/",
            "BoM, sourced {sourced}.",
            "",
            "licensed under the Creative Commons Attribution Australia Licence",
        ),
        source=Source(
            adapter="kiwis",
            url="https://example.gov.au/waterdata/services",
            search="Storage Volume",
            package="DMQaQc.Merged.DailyMean.24HR",
            resource=resource,
        ),
    )


WATER_PAGE = b"<p>Unless otherwise noted, all material on this page is\n licensed under the Creative Commons Attribution Australia Licence</p>"


def test_kiwis_values_are_one_csv_ordered_by_station_and_day_and_dated_by_the_newest_value(
    tmp_path, monkeypatch
):
    routes = {
        "https://example.gov.au/waterdata/services": [Resp(STATIONS), Resp(SERIES), Resp(VALUES)],
        "https://example.gov.au/waterdata/": Resp(WATER_PAGE, ctype="text/html"),
    }
    data, m, s = run("kiwis", kiwis_ds("values"), routes, tmp_path, monkeypatch)
    lines = data.decode().splitlines()
    assert lines[0] == "station_no,station_name,state,date,value,quality_code"
    assert lines[1:] == [
        "143001,Wivenhoe,QLD,2026-01-02,900000.5,10",
        "212243,Warragamba Dam,NSW,2026-01-01,1930000.0,90",
        "212243,Warragamba Dam,NSW,2026-01-02,1931364.77,140",
    ]
    assert m.version == "2026-01-02" and m.source["series_read"] == 2
    assert m.licence["id"] == "CC-BY-3.0-AU" and f.KIWIS_NOTE in m.notes


def test_kiwis_stations_carry_the_owner_state_and_capacity(tmp_path, monkeypatch):
    routes = {
        "https://example.gov.au/waterdata/services": [Resp(STATIONS)],
        "https://example.gov.au/waterdata/": Resp(WATER_PAGE, ctype="text/html"),
    }
    data, m, _ = run("kiwis", kiwis_ds("stations"), routes, tmp_path, monkeypatch)
    lines = data.decode().splitlines()
    assert lines[0].startswith(
        "station_no,station_name,latitude,longitude,data_owner,full_storage_volume_ml"
    )
    assert lines[0].endswith(",state")
    assert lines[1].startswith("143001,Wivenhoe,-27.4,152.6,QLD - Seqwater,,") and lines[
        1
    ].endswith(",QLD")
    assert lines[2].startswith("212243,Warragamba Dam,-33.9,150.6,NSW - Water NSW,2027000,")
    assert f.KIWIS_STATIONS_NOTE in m.notes


def test_kiwis_series_are_packed_under_the_services_value_limit(monkeypatch):
    monkeypatch.setattr(f, "KIWIS_BATCH_VALUES", 50_000)
    long = {"ts_id": "a", "from": "1900-01-01T00:00:00", "to": "2026-01-01T00:00:00"}
    short = [
        {"ts_id": str(i), "from": "2026-01-01T00:00:00", "to": "2026-01-10T00:00:00"}
        for i in range(3)
    ]
    batches = f._kiwis_batches([*short, long, dict(long, ts_id="b")])
    assert batches == [["a"], ["b", "0", "1", "2"]]


AIHW_PAGE = (
    b'<div class="s-downloadable-resources" data-enable-tag-filter="false" data-report-node-guid="abc"'
    b' data-current-node-id="42" data-filter-by-children-of-current-node="true"'
    b' data-filter-by-children-of-selected-node="" data-order-by-column="date" data-content-type="x"></div>'
)
AIHW_LIST = {
    "totalResults": 2,
    "results": [
        {
            "resultTitle": "Data tables: SHS monthly data",
            "resultUrl": "/getmedia/g1/shs_May-2026.xlsx",
            "resultDateTimeFormatted": "2026-05-01",
            "catNum": "HOU 321",
        },
        {
            "resultTitle": "Data tables: SHS monthly data",
            "resultUrl": "/getmedia/g2/shs_June-2026.xlsx.aspx",
            "resultDateTimeFormatted": "2026-06-01",
            "catNum": "HOU 321",
        },
        {
            "resultTitle": "Technical notes",
            "resultUrl": "/getmedia/g3/notes.pdf",
            "resultDateTimeFormatted": "2026-06-01",
        },
    ],
}


class AihwPortal(Portal):
    def post(self, url, json=None, **kw):
        self.asked.append(("POST", url, json["reportNodeGuid"], json["currentNodeId"]))
        return Resp(AIHW_LIST)


def test_aihw_takes_the_newest_listed_file_whose_title_matches(tmp_path, monkeypatch):
    routes = {
        "https://example.gov.au/copyright": Resp(PAGE, ctype="text/html"),
        "https://www.aihw.gov.au/reports/x/data": Resp(AIHW_PAGE, ctype="text/html"),
        "https://www.aihw.gov.au/getmedia/g2/": Resp(
            CSV, headers={"Last-Modified": "Wed, 19 Aug 2026 02:03:41 GMT"}
        ),
    }
    s = AihwPortal(routes)
    monkeypatch.setattr(f, "_session", lambda session: s)
    ds = make_dataset(
        [Field("a", "a", "integer")],
        licence=Licence(
            "CC-BY-4.0",
            "https://example.gov.au/copyright",
            "AIHW, sourced {sourced}.",
            "",
            "material is provided under a Creative Commons Attribution 4.0 licence",
        ),
        source=Source(
            adapter="aihw",
            url="https://www.aihw.gov.au/reports/x/data",
            resource_match="^Data tables",
        ),
    )
    data, m, lic = f.ADAPTERS["aihw"](ds, tmp_path)
    f.check_licence(ds, lic)
    assert data == CSV and m.version == "2026-08-19" and m.filename == "shs_June-2026.xlsx"
    assert m.source["resource_name"] == "Data tables: SHS monthly data"
    assert ("POST", f.AIHW_LISTING, "abc", 42) in s.asked
    with pytest.raises(f.FetchError, match="no listed file matches"):
        f.ADAPTERS["aihw"](
            dataclasses.replace(ds, source=dataclasses.replace(ds.source, resource_match="^Nope")),
            tmp_path,
        )


ZENODO = {
    "hits": {
        "hits": [
            {
                "id": 22262542,
                "doi": "10.5281/zenodo.22262542",
                "updated": "2026-09-02T23:31:28.421133+00:00",
                "links": {"self_html": "https://zenodo.org/records/22262542"},
                "metadata": {"publication_date": "2026-09-02", "license": {"id": "cc-by-4.0"}},
                "files": [
                    {
                        "key": "Database Public Version.xlsx",
                        "size": 3,
                        "checksum": "md5:x",
                        "links": {
                            "self": "https://zenodo.org/api/records/22262542/files/Database%20Public%20Version.xlsx/content"
                        },
                    },
                    {
                        "key": "README.txt",
                        "size": 3,
                        "links": {
                            "self": "https://zenodo.org/api/records/22262542/files/README.txt/content"
                        },
                    },
                ],
            }
        ]
    }
}


def test_zenodo_follows_the_concept_record_to_its_newest_version_and_licence(tmp_path, monkeypatch):
    routes = {
        "https://zenodo.org/api/records/22262542/files/": Resp(CSV),
        "https://zenodo.org/api/records": Resp(ZENODO),
    }
    ds = make_dataset(
        [Field("a", "a", "integer")],
        licence=Licence(
            "CC-BY-4.0", "https://zenodo.org/records/22262542", "Taronga, sourced {sourced}."
        ),
        source=Source(
            adapter="zenodo",
            url="https://zenodo.org/api/records",
            package="5612259",
            resource_match=r"\.xlsx$",
        ),
    )
    data, m, s = run("zenodo", ds, routes, tmp_path, monkeypatch)
    assert data == CSV and m.version == "2026-09-03" and m.as_at == "2026-09-02"
    assert m.filename == "Database Public Version.xlsx" and m.source["record"] == 22262542
    assert m.licence["stated"] == "cc-by-4.0" and m.licence["id"] == "CC-BY-4.0"
    with pytest.raises(f.LicenceDrift):
        run(
            "zenodo",
            dataclasses.replace(ds, licence=Licence("CC0-1.0", "e", "a")),
            routes,
            tmp_path,
            monkeypatch,
        )


def _stack_book(title_rows: int, rows: list[list], header=("FullDate", "Brand", "Diesel")) -> bytes:
    import io

    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Sheet1")
    for i in range(title_rows):
        ws.write(i, 0, f"Title {i}")
    ws.write_row(title_rows, 0, list(header))
    for j, r in enumerate(rows, start=title_rows + 1):
        ws.write_row(j, 0, r)
    wb.close()
    return buf.getvalue()


def test_a_ckan_stack_reads_every_workbook_once_into_one_ordered_table(tmp_path, monkeypatch):
    pkgs = {
        "success": True,
        "result": {
            "results": [
                {
                    "name": "fuel-2020",
                    "license_id": "cc-by",
                    "license_title": "CC BY",
                    "license_url": "",
                    "metadata_modified": "2023-02-06T00:00:00",
                    "resources": [
                        {
                            "id": "r2",
                            "name": "Feb",
                            "format": "XLSX",
                            "created": "2023-02-03T02:00:00",
                            "url": "https://p.example/feb.xlsx",
                        },
                        {
                            "id": "r1",
                            "name": "Jan",
                            "format": "XLSX",
                            "created": "2023-02-03T01:00:00",
                            "url": "https://p.example/jan.xlsx",
                        },
                        {
                            "id": "r3",
                            "name": "Notes",
                            "format": "PDF",
                            "created": "2023-02-03T01:00:00",
                            "url": "https://p.example/notes.pdf",
                        },
                    ],
                },
                {
                    "name": "fuel-other",
                    "license_id": "cc-by",
                    "license_title": "CC BY",
                    "license_url": "",
                    "metadata_modified": "2023-02-06T00:00:00",
                    "resources": [],
                },
            ]
        },
    }
    routes = {
        "https://p.example/api/3/action/package_search": Resp(pkgs),
        "https://p.example/jan.xlsx": Resp(
            _stack_book(1, [["2020-01-31", "Shell", 150.5], ["2020-01-30", "BP", 0]]),
            ctype="application/octet-stream",
        ),
        "https://p.example/feb.xlsx": Resp(
            _stack_book(0, [["2020-01-31", "Shell", 150.5], ["2020-02-01", "BP", 149]]),
            ctype="application/octet-stream",
        ),
    }
    ds = make_dataset(
        [Field("date", "FullDate", "date")],
        licence=Licence(
            "CC-BY-3.0-AU", "https://p.example/dataset/fuel", "NT, sourced {sourced}.", "cc-by"
        ),
        source=Source(
            adapter="ckan-stack",
            url="https://p.example/dataset/fuel",
            portal="https://p.example",
            package="fuel",
            package_match="^fuel-",
            header_match="FullDate",
        ),
    )
    data, m, s = run("ckan-stack", ds, routes, tmp_path, monkeypatch)
    assert data.decode().splitlines() == [
        "FullDate,Brand,Diesel",
        "2020-01-30,BP,0",
        "2020-01-31,Shell,150.5",
        "2020-02-01,BP,149",
    ]
    assert m.source["rows_repeated"] == 1 and [w["resource"] for w in m.source["workbooks"]] == [
        "r1",
        "r2",
    ]
    assert m.version == "2023-02-03" and f.STACK_NOTE in m.notes
    assert s.asked[1:] == ["https://p.example/jan.xlsx", "https://p.example/feb.xlsx"]


def test_kiwis_splits_a_batch_the_service_refuses_as_too_large(monkeypatch):
    calls = []

    def fake_query(s, base, request, **params):
        ids = params["ts_id"].split(",")
        calls.append(ids)
        if len(ids) > 1:
            msg = "KiWIS getTimeseriesValues refused: TooManyResults: narrow your request"
            raise f.FetchError(msg)
        return [{"station_no": ids[0], "ts_id": ids[0], "data": []}]

    monkeypatch.setattr(f, "_kiwis_query", fake_query)
    got = f._kiwis_values(None, "b", ["1", "2", "3"])
    assert [g["ts_id"] for g in got] == ["1", "2", "3"]
    assert calls == [["1", "2", "3"], ["1"], ["2", "3"], ["2"], ["3"]]
    with pytest.raises(f.FetchError, match="TooManyResults"):
        monkeypatch.setattr(
            f,
            "_kiwis_query",
            lambda *a, **k: (_ for _ in ()).throw(f.FetchError("x TooManyResults")),
        )
        f._kiwis_values(None, "b", ["9"])


def test_a_downloaded_file_keeps_its_headers_whatever_their_case():
    r = f.Fetched(b"x", {"etag": '"abc"', "last-modified": "Tue, 29 Sep 2026 23:00:37 GMT"})
    assert r.headers.get("ETag") == '"abc"' and r.headers["Last-Modified"].startswith("Tue")


def test_a_manual_stack_reads_a_folder_of_downloads_then_waits_for_the_records_to_change(
    tmp_path, monkeypatch
):
    from publicdata import store

    def portal(modified):
        res = [
            {"id": f"r{i}", "name": n, "format": "XLSX", "created": modified, "url": u}
            for i, (n, u) in enumerate(
                [
                    ("2019", "https://p.example/a/2019%20prod.xlsx"),
                    ("2020", "https://p.example/b/2020.xlsx"),
                ]
            )
        ]
        p = {
            "name": "prod-2019",
            "license_id": "cc-by",
            "license_title": "CC BY",
            "license_url": "",
        }
        return {
            "success": True,
            "result": {"results": [p | {"metadata_modified": modified, "resources": res}]},
        }

    ds = make_dataset(
        [Field("date", "FullDate", "date")],
        licence=Licence("CC-BY-4.0", "https://p.example/d", "NT, sourced {sourced}.", "cc-by"),
        source=Source(
            adapter="ckan-stack",
            url="https://p.example/d",
            portal="https://p.example",
            package="prod",
            package_match="^prod-",
            header_match="FullDate",
            manual=True,
        ),
    )
    search = "https://p.example/api/3/action/package_search"
    monkeypatch.setattr(f, "MANUAL", {})
    with pytest.raises(f.ManualDue, match="download https://p.example/d "):
        run("ckan-stack", ds, {search: Resp(portal("2023-02-03T00:00:00"))}, tmp_path, monkeypatch)

    got = tmp_path / "downloads"
    got.mkdir()
    (got / "2019 prod.xlsx").write_bytes(_stack_book(0, [["2019-06-30", "BP", 1]]))
    (got / "2020.xlsx").write_bytes(_stack_book(2, [["2020-06-30", "BP", 2]]))
    monkeypatch.setattr(f, "MANUAL", {ds.slug: got})
    data, m, s = run(
        "ckan-stack", ds, {search: Resp(portal("2023-02-03T00:00:00"))}, tmp_path, monkeypatch
    )
    assert data.decode().splitlines() == [
        "FullDate,Brand,Diesel",
        "2019-06-30,BP,1",
        "2020-06-30,BP,2",
    ]
    assert s.asked == [search]
    store.write(tmp_path, m, data)

    monkeypatch.setattr(f, "MANUAL", {})
    data, _, _ = run(
        "ckan-stack", ds, {search: Resp(portal("2023-02-03T00:00:00"))}, tmp_path, monkeypatch
    )
    assert data is None
    with pytest.raises(f.ManualDue):
        run("ckan-stack", ds, {search: Resp(portal("2024-01-01T00:00:00"))}, tmp_path, monkeypatch)
