import datetime as dt
import io
import re

import pytest
import yaml

from publicdata import fetch as f
from publicdata import store
from publicdata.normalise import XML_JOIN, read_xml
from publicdata.register import (
    GRANTS,
    Field,
    Licence,
    RegisterError,
    Source,
    load_grants,
    parse,
    parse_grant,
)

from .conftest import make_dataset, make_manifest
from .test_fetch_adapters import Portal, Resp

GRANT = {
    "id": "TEST-NOTICE",
    "title": "Test notice",
    "url": "https://example.gov.au/copyright",
    "kind": "notice",
    "read": "2026-10-01",
    "grants": {
        "reproduce": "may be reproduced",
        "adapt": "may be adapted",
        "commercial": "silent",
        "attribution": "Source: Example",
    },
}


def test_a_grant_is_admitted_only_when_it_quotes_all_four_tests(tmp_path):
    assert parse_grant(GRANT, "g", tmp_path).grants["commercial"] == "silent"
    for test in ("reproduce", "adapt", "commercial", "attribution"):
        words = {k: v for k, v in GRANT["grants"].items() if k != test}
        with pytest.raises(RegisterError, match=f"on '{test}'"):
            parse_grant(GRANT | {"grants": words}, "g", tmp_path)
    with pytest.raises(RegisterError, match="Creative Commons id"):
        parse_grant(GRANT | {"id": "CC-BY-4.0"}, "g", tmp_path)
    with pytest.raises(RegisterError, match="read is the date"):
        parse_grant(GRANT | {"read": "October 2026"}, "g", tmp_path)


def test_a_permission_names_its_agency_year_and_stored_letter(tmp_path):
    perm = GRANT | {"id": "WAPOL-PERMISSION-2026", "kind": "permission"}
    with pytest.raises(RegisterError, match="letter"):
        parse_grant(perm, "g", tmp_path)
    (tmp_path / "letters").mkdir()
    (tmp_path / "letters" / "wapol.pdf").write_bytes(b"%PDF")
    assert parse_grant(perm | {"letter": "letters/wapol.pdf"}, "g", tmp_path).kind == "permission"
    with pytest.raises(RegisterError, match="AGENCY"):
        parse_grant(perm | {"id": "WAPOL-2026", "letter": "letters/wapol.pdf"}, "g", tmp_path)


def test_a_grant_file_is_named_by_its_id(tmp_path):
    (tmp_path / "OTHER.yaml").write_text(yaml.safe_dump(GRANT))
    with pytest.raises(RegisterError, match="filename"):
        load_grants(tmp_path)


def test_the_committed_grants_pass_and_join_the_open_licences():
    from publicdata.register import LICENCE_CONDITIONS, OPEN_LICENCES

    assert {"RBA-COPYRIGHT-NOTICE", "OPEN-GNAF-EULA"} <= set(GRANTS)
    assert OPEN_LICENCES["RBA-COPYRIGHT-NOTICE"] == (
        "RBA copyright notice",
        "https://www.rba.gov.au/copyright/",
    )
    assert LICENCE_CONDITIONS["OPEN-GNAF-EULA"].startswith("You must not use the data")


def _live(**licence):
    return {
        "slug": "x-y",
        "title": "X",
        "status": "live",
        "publisher": {"name": "Agency", "jurisdiction": "Cth"},
        "licence": {"id": "CC-BY-4.0", "evidence": "https://e", "attribution": "a"} | licence,
        "source": {"adapter": "ckan-resource", "url": "https://e", "package": "p", "resource": "r"},
        "fields": [{"name": "a", "source": "A"}],
        "search_title": "s",
        "also_known_as": ["a", "b"],
        "keywords": ["a", "b", "c"],
        "faq": [{"q": "q", "a": "a"}],
        "topics": ["roads"],
    }


def test_a_live_entry_records_when_a_person_reviewed_its_licence():
    with pytest.raises(RegisterError, match=re.escape("licence.reviewed")):
        parse(_live(), "x")
    with pytest.raises(RegisterError, match="YYYY-MM-DD"):
        parse(_live(reviewed="1 Oct 2026"), "x")
    assert parse(_live(reviewed="2026-10-01"), "x").licence.reviewed == "2026-10-01"


def test_an_entry_that_misreads_a_portals_version_is_stopped():
    # data.sa.gov.au defines cc-by as CC BY 4.0; an entry that says 3.0 AU is wrong however the
    # portal answers.
    ds = make_dataset(
        [Field("a", "a")],
        licence=Licence("CC-BY-3.0-AU", "https://e", "a", portal_id="cc-by"),
        source=Source(
            adapter="ckan-resource", url="https://e", portal="https://data.sa.gov.au/data"
        ),
    )
    with pytest.raises(f.LicenceDrift, match=re.escape("defines 'cc-by' as CC-BY-4.0")):
        f.check_licence(ds, {"id": "cc-by"})
    ok = make_dataset(
        [Field("a", "a")],
        licence=Licence("CC-BY-4.0", "https://e", "a", portal_id="cc-by"),
        source=ds.source,
    )
    f.check_licence(ok, {"id": "cc-by"})


def test_an_unversioned_code_without_portal_id_is_refused():
    ds = make_dataset(
        [Field("a", "a")],
        licence=Licence("CC-BY-4.0", "https://e", "a"),
        source=Source(
            adapter="ckan-resource", url="https://e", portal="https://data.nsw.gov.au/data"
        ),
    )
    with pytest.raises(f.LicenceDrift, match="names no licence version"):
        f.check_licence(ds, {"id": "cc-by", "title": "Creative Commons Attribution"})


def test_a_fetch_refuses_a_licence_read_with_no_place_or_time(tmp_path, monkeypatch):
    ds = make_dataset([Field("a", "a")], source=Source(adapter="file", url="https://e/f.csv"))
    m = make_manifest(b"a\n1\n", dataset="t")
    monkeypatch.setitem(f.ADAPTERS, "file", lambda d, s: (b"a\n1\n", m, {"id": "CC-BY-4.0"}))
    with pytest.raises(f.FetchError, match="read_from"):
        f.fetch(ds, tmp_path)
    lic = {"id": "CC-BY-4.0", "read_from": "https://e", "read_at": "2026-10-01T00:00:00+00:00"}
    monkeypatch.setitem(f.ADAPTERS, "file", lambda d, s: (b"a\n1\n", m, lic))
    assert f.fetch(ds, tmp_path).version == m.version


def test_every_adapter_records_where_it_read_the_licence():
    import inspect

    for name, fn in f.ADAPTERS.items():
        src = inspect.getsource(fn)
        assert any(k in src for k in ("_licence(", "statement_licence(", "read_from")), name


def test_a_feed_is_one_version_per_day_and_a_day_is_never_rewritten():
    day = dt.date(2026, 10, 1)
    m = make_manifest(b"x", version="2024-01-01")
    got = f.feed_version(m, [], day)
    assert got.version == "2026-10-01"
    assert got.notes == [f.FEED_NOTE]
    again = make_manifest(b"y", version="2024-01-01")
    assert f.feed_version(again, [got], day) is None
    assert f.feed_version(again, [got], day + dt.timedelta(days=1)).version == "2026-10-02"


def test_the_feeds_flag_parses_and_must_be_a_boolean():
    raw = _live(reviewed="2026-10-01")
    raw["source"]["feed"] = True
    assert parse(raw, "x").source.feed
    raw["source"]["feed"] = "yes"
    with pytest.raises(RegisterError, match="true or false"):
        parse(raw, "x")


PAGE = b"""<html><a href="/files/fuel-2025-01.xlsx">Jan</a> <a href='fuel-2025-02.xlsx'>Feb</a>
<a href="/files/fuel-2025-01.xlsx">again</a> <a href="/about">About</a>
<a href="https://other.example/fuel%202025-03.csv">Mar</a></html>"""


def test_page_links_are_absolute_once_each_and_matched_decoded():
    got = f.page_links(PAGE, "https://pub.example/data/", r"fuel[- ]2025-\d\d\.(xlsx|csv)$")
    assert got == [
        "https://pub.example/files/fuel-2025-01.xlsx",
        "https://pub.example/data/fuel-2025-02.xlsx",
        "https://other.example/fuel%202025-03.csv",
    ]


def _xlsx(rows):
    import openpyxl

    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_a_file_stack_reads_every_listed_file_as_one_table(tmp_path, monkeypatch):
    ds = make_dataset(
        [Field("site", "Site"), Field("price", "Price", "number")],
        licence=Licence(
            "CC-BY-4.0", "https://pub.example/copyright", "a", statement="licensed under CC BY 4.0"
        ),
        source=Source(
            adapter="file-stack",
            url="https://pub.example/data/",
            resource_match=r"fuel-2025-\d\d\.(xlsx|csv)$",
            header_match="Site",
        ),
    )
    jan = _xlsx([["Fuel prices, January"], [], ["Site", "Price"], ["A", 1.9], ["B", 2.05]])
    feb = b"Site,Price\nB,2.05\nC,2.1\n"
    lm = {"Last-Modified": "Mon, 03 Mar 2025 00:00:00 GMT"}
    s = Portal(
        {
            "https://pub.example/copyright": Resp(
                b"<p>This is licensed under CC BY 4.0.</p>", ctype="text/html"
            ),
            "https://pub.example/data/": Resp(
                b'<a href="/f/fuel-2025-01.xlsx">J</a><a href="/f/fuel-2025-02.csv">F</a>',
                ctype="text/html",
            ),
            "https://pub.example/f/fuel-2025-01.xlsx": Resp(jan, ctype="application/vnd.ms-excel"),
            "https://pub.example/f/fuel-2025-02.csv": Resp(feb, headers=lm),
        }
    )
    s.head = lambda url, **kw: type(
        "H", (), {"ok": True, "headers": lm if url.endswith(".csv") else {}}
    )()
    monkeypatch.setattr(f, "_session", lambda session: s)
    data, m, lic = f.file_stack(ds, tmp_path)
    assert data.decode().splitlines() == ["Site,Price", "A,1.9", "B,2.05", "C,2.1"]
    assert m.version == "2025-03-03"
    assert m.source["rows_repeated"] == 1
    assert [x["rows"] for x in m.source["files"]] == [2, 2]
    assert lic["read_from"] == "https://pub.example/copyright"
    assert lic["read_at"]
    assert f.FILE_NOTE not in m.notes
    s.routes["https://pub.example/copyright"] = Resp(
        b"<p>All rights reserved.</p>", ctype="text/html"
    )
    with pytest.raises(f.LicenceDrift):
        f.file_stack(ds, tmp_path)


def test_a_file_stack_with_no_dated_file_says_it_is_dated_by_the_fetch(tmp_path, monkeypatch):
    ds = make_dataset(
        [Field("site", "Site")],
        licence=Licence("CC-BY-4.0", "https://p/c", "a", statement="CC BY 4.0"),
        source=Source(
            adapter="file-stack", url="https://p/d/", resource_match=r"\.csv$", header_match="Site"
        ),
    )
    s = Portal(
        {
            "https://p/c": Resp(b"CC BY 4.0", ctype="text/html"),
            "https://p/d/a.csv": Resp(b"Site\nA\n"),
            "https://p/d/": Resp(b'<a href="a.csv">a</a>', ctype="text/html"),
        }
    )
    monkeypatch.setattr(f, "_session", lambda session: s)
    _, m, _ = f.file_stack(ds, tmp_path)
    assert f.FILE_NOTE in m.notes
    assert m.source["newest_file"] is None


def test_a_file_stack_entry_names_its_link_pattern_and_header():
    raw = _live(reviewed="2026-10-01", statement="words")
    raw["source"] = {"adapter": "file-stack", "url": "https://e", "header_match": "Site"}
    with pytest.raises(RegisterError, match="resource_match"):
        parse(raw, "x")
    raw["source"]["resource_match"] = r"\.xlsx$"
    assert parse(raw, "x").source.adapter == "file-stack"


XML = """﻿<?xml version="1.0" encoding="utf-8"?>
<places xmlns="https://example.gov.au/ns">
  <place type="State" site_id="22"><ref>1</ref><name>Silos</name>
    <criterion type="A">first</criterion><criterion type="D">second</criterion></place>
  <place type="State"><ref>2</ref><name>Hall</name><alias>Old hall</alias></place>
</places>"""


def test_an_xml_record_becomes_one_row_with_attributes_and_repeats_joined():
    t = read_xml(XML.encode("utf-8"), "place")
    assert t.column_names == [
        "@type",
        "@site_id",
        "ref",
        "name",
        "criterion",
        "criterion@type",
        "alias",
    ]
    rows = t.to_pylist()
    assert rows[0]["criterion"] == f"first{XML_JOIN}second"
    assert rows[0]["criterion@type"] == f"A{XML_JOIN}D"
    assert rows[1]["@site_id"] == ""
    assert rows[1]["alias"] == "Old hall"
    from publicdata.normalise import NormaliseError

    with pytest.raises(NormaliseError, match="no <site>"):
        read_xml(XML.encode("utf-8"), "site")
    nested = "<r><place><ref>1</ref><place><ref>2</ref></place></place></r>"
    with pytest.raises(NormaliseError, match="holds another"):
        read_xml(nested.encode("utf-8"), "place")


def test_stored_manifests_are_read_with_their_licence_record(tmp_path):
    m = make_manifest(b"a\n", licence={"id": "CC-BY-4.0", "read_from": "https://e", "read_at": "t"})
    store.write(tmp_path, m, b"a\n")
    assert store.manifests(tmp_path, "t")[-1].licence["read_from"] == "https://e"


def test_a_file_url_with_no_extension_is_named_by_its_declared_format(tmp_path, monkeypatch):
    ds = make_dataset(
        [Field("a", "a")],
        licence=Licence("CC-BY-4.0", "https://p/c", "a", statement="CC BY 4.0"),
        source=Source(
            adapter="file", url="https://p/geoserver/wfs?request=GetFeature", format="geojson"
        ),
    )
    s = Portal(
        {
            "https://p/c": Resp(b"CC BY 4.0", ctype="text/html"),
            "https://p/geoserver/wfs": Resp(
                b'{"type":"FeatureCollection","features":[]}', ctype="application/json"
            ),
        }
    )
    monkeypatch.setattr(f, "_session", lambda session: s)
    _, m, _ = f.http_file(ds, tmp_path)
    assert m.filename == "t.geojson"
    assert m.ext == "geojson"


def test_a_capped_wfs_is_read_page_by_page_into_one_file(tmp_path, monkeypatch):
    import json as _json

    ds = make_dataset(
        [Field("a", "a")],
        licence=Licence("CC-BY-4.0", "https://p/c", "a", statement="CC BY 4.0"),
        source=Source(
            adapter="file",
            url="https://p/wfs?request=GetFeature&sortBy=id",
            format="geojson",
            page_size=2,
        ),
    )

    def page(ids):
        feats = [{"type": "Feature", "properties": {"a": i}, "geometry": None} for i in ids]
        return Resp(_json.dumps({"features": feats}).encode(), ctype="application/json")

    s = Portal(
        {
            "https://p/c": Resp(b"CC BY 4.0", ctype="text/html"),
            "https://p/wfs?request=GetFeature&sortBy=id&count=2&startIndex=0": page([1, 2]),
            "https://p/wfs?request=GetFeature&sortBy=id&count=2&startIndex=2": page([3, 4]),
            "https://p/wfs?request=GetFeature&sortBy=id&count=2&startIndex=4": page([5]),
        }
    )
    monkeypatch.setattr(f, "_session", lambda session: s)
    data, m, _ = f.http_file(ds, tmp_path)
    assert [x["properties"]["a"] for x in _json.loads(data)["features"]] == [1, 2, 3, 4, 5]
    assert m.filename == "t.geojson"


def test_a_resource_served_through_a_page_script_is_named_by_its_file():
    assert (
        f.resource_filename({"url": "https://x/a/CFA-times.xlsx.aspx", "format": "XLSX"})
        == "CFA-times.xlsx"
    )
    assert (
        f.resource_filename({"url": "https://x/a/report.aspx", "format": "CSV"})
        == "report.aspx.csv"
    )


def test_a_stack_can_name_each_rows_file_and_a_repeated_header_stays_distinct(
    tmp_path, monkeypatch
):
    from publicdata.normalise import read_xlsx

    t = read_xlsx(_xlsx([["Rank", "Name", "Rank", "Name"], [1, "Ava", 1, "Leo"]]), "", 1)
    assert t.column_names == ["Rank", "Name", "Rank (2)", "Name (2)"]
    ds = make_dataset(
        [Field("site", "Site"), Field("year_file", "(file)")],
        licence=Licence("CC-BY-4.0", "https://p/c", "a", statement="CC BY 4.0"),
        source=Source(
            adapter="file-stack", url="https://p/d/", resource_match=r"\.csv$", header_match="Site"
        ),
    )
    s = Portal(
        {
            "https://p/c": Resp(b"CC BY 4.0", ctype="text/html"),
            "https://p/d/names-2023.csv": Resp(b"Site\nA\n"),
            "https://p/d/names-2024.csv": Resp(b"Site\nA\n"),
            "https://p/d/": Resp(
                b'<a href="names-2023.csv">a</a><a href="names-2024.csv">b</a>', ctype="text/html"
            ),
        }
    )
    monkeypatch.setattr(f, "_session", lambda session: s)
    data, _, _ = f.file_stack(ds, tmp_path)
    assert data.decode().splitlines() == ["Site,(file)", "A,names-2023.csv", "A,names-2024.csv"]


def test_dates_in_the_ways_publishers_write_them():
    assert f.parse_as_at("Current as at 23/09/2026", r"as at (\S+)") == "2026-09-23"


def test_a_tie_on_creation_date_goes_to_the_newest_file():
    ds = make_dataset(
        [Field("a", "a")],
        source=Source(adapter="ckan-resource", url="u", resource_match="Elective"),
    )
    old = {
        "name": "Elective surgery Sep 2024",
        "created": "2023-01-01",
        "last_modified": "2024-10-01",
    }
    new = {
        "name": "Elective surgery Sep 2025",
        "created": "2023-01-01",
        "last_modified": "2025-10-01",
    }
    assert f.pick_resource(ds, [new, old]) is new


def test_a_stack_keeps_a_repeated_header_as_two_columns(tmp_path, monkeypatch):
    ds = make_dataset(
        [Field("rank", "Rank"), Field("girl", "Name"), Field("boy", "Name (2)")],
        licence=Licence("CC-BY-4.0", "https://p/c", "a", statement="CC BY 4.0"),
        source=Source(
            adapter="file-stack", url="https://p/d/", resource_match=r"\.xlsx$", header_match="Rank"
        ),
    )
    s = Portal(
        {
            "https://p/c": Resp(b"CC BY 4.0", ctype="text/html"),
            "https://p/d/names.xlsx": Resp(
                _xlsx([["Rank", "Name", "Name"], [1, "Ava", "Leo"]]),
                ctype="application/vnd.ms-excel",
            ),
            "https://p/d/": Resp(b'<a href="names.xlsx">n</a>', ctype="text/html"),
        }
    )
    monkeypatch.setattr(f, "_session", lambda session: s)
    data, _, _ = f.file_stack(ds, tmp_path)
    assert data.decode().splitlines() == ["Rank,Name,Name (2)", "1,Ava,Leo"]


def test_a_reordered_export_with_the_same_rows_is_no_new_version(tmp_path, monkeypatch):
    from publicdata import store

    ds = make_dataset(
        [Field("a", "a"), Field("b", "b")], source=Source(adapter="file", url="https://e/f.csv")
    )
    lic = {"id": "CC-BY-4.0", "read_from": "https://e", "read_at": "2026-10-01T00:00:00+00:00"}
    first, reordered, changed = b"a,b\n1,x\n2,y\n", b"a,b\n2,y\n1,x\n", b"a,b\n2,y\n1,z\n"

    def adapter(data, version):
        m = make_manifest(data, dataset="t", version=version, encoding="utf-8")
        return lambda d, s: (data, m, lic)

    monkeypatch.setitem(f.ADAPTERS, "file", adapter(first, "2026-10-01"))
    assert f.fetch(ds, tmp_path).rows_sha256
    monkeypatch.setitem(f.ADAPTERS, "file", adapter(reordered, "2026-10-02"))
    assert f.fetch(ds, tmp_path) is None
    monkeypatch.setitem(f.ADAPTERS, "file", adapter(changed, "2026-10-02"))
    assert f.fetch(ds, tmp_path).version == "2026-10-02"
    assert [m.version for m in store.manifests(tmp_path, "t")] == ["2026-10-01", "2026-10-02"]
    # Every manifest the fetch writes carries the caps stamp the build keys the formats on; one
    # written without it keeps the old set, and its bytes, since the field is left out at 0.
    assert {m.caps for m in store.manifests(tmp_path, "t")} == {store.CAPS_VERSION}
    assert '"caps": 1' in (tmp_path / "t" / "2026-10-02" / "manifest.json").read_text()
    assert '"caps"' not in make_manifest(b"x", dataset="t").to_json()


def test_an_export_with_no_rows_never_replaces_one_that_had_some(tmp_path, monkeypatch):
    from publicdata import store

    ds = make_dataset(
        [Field("a", "a"), Field("b", "b")], source=Source(adapter="file", url="https://e/f.csv")
    )
    lic = {"id": "CC-BY-4.0", "read_from": "https://e", "read_at": "2026-10-01T00:00:00+00:00"}

    def adapter(data, version):
        m = make_manifest(data, dataset="t", version=version, encoding="utf-8")
        return lambda d, s: (data, m, lic)

    monkeypatch.setitem(f.ADAPTERS, "file", adapter(b"a,b\n", "2026-10-01"))
    assert f.fetch(ds, tmp_path).version == "2026-10-01"  # a first version may be empty
    monkeypatch.setitem(f.ADAPTERS, "file", adapter(b"a,b\n1,x\n", "2026-10-02"))
    assert f.fetch(ds, tmp_path).version == "2026-10-02"
    monkeypatch.setitem(f.ADAPTERS, "file", adapter(b"a,b\n", "2026-10-03"))
    with pytest.raises(f.FetchError, match="no rows"):
        f.fetch(ds, tmp_path)
    assert [m.version for m in store.manifests(tmp_path, "t")] == ["2026-10-01", "2026-10-02"]


def test_a_manifest_without_a_rows_digest_keeps_its_bytes():
    m = make_manifest(b"a\n1\n")
    assert "rows_sha256" not in m.to_json()
    m.rows_sha256 = "ab"
    assert '"rows_sha256": "ab"' in m.to_json()


def test_a_polygon_layers_row_digest_follows_each_rows_own_shape():
    import json

    ds = make_dataset(
        [Field("code", "CODE"), Field("name", "NAME")],
        geometry={"kind": "polygon", "crs": "EPSG:7844"},
    )

    def layer(*features):
        fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"CODE": c, "NAME": n},
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                }
                for c, n, ring in features
            ],
        }
        return json.dumps(fc).encode()

    a = ("1", "A", [[150, -30], [151, -30], [151, -29], [150, -30]])
    b = ("2", "B", [[140, -20], [141, -20], [141, -19], [140, -20]])
    m = make_manifest(layer(a, b), filename="layer.geojson")
    first = f.rows_digest(ds, m, layer(a, b))
    assert first
    assert f.rows_digest(ds, m, layer(b, a)) == first
    # The same attributes with the shapes swapped between rows is a change.
    swapped = (("1", "A", b[2]), ("2", "B", a[2]))
    assert f.rows_digest(ds, m, layer(*swapped)) != first


def test_a_stack_reads_each_titled_table_in_a_file_as_one_row_per_cell():
    data = (
        b"Wait Times as at 30 June 2020,,,\n"
        b"Region,1 bedroom,2 bedroom,\n"
        b"Darwin,6 to 8 years,2 to 4 years*,\n"
        b'"*Calculated by hand.",,,\n'
        b",,,\n"
        b"Wait List Summary as at 30 June 2020,,,\n"
        b"Region,1 bedroom,3+ bedroom,Total\n"
        b"Darwin,1391,277,1668\n"
    )
    h, rows = f._stack_rows(data, "a.csv", "Region", r"^(?P<section>Wait Times|Wait List)\b")
    assert h == ["Section", "Region", "Column", "Value"]
    assert rows == [
        ["Wait Times", "Darwin", "1 bedroom", "6 to 8 years"],
        ["Wait Times", "Darwin", "2 bedroom", "2 to 4 years*"],
        ["Wait List", "Darwin", "1 bedroom", "1391"],
        ["Wait List", "Darwin", "3+ bedroom", "277"],
        ["Wait List", "Darwin", "Total", "1668"],
    ]


def test_a_stack_reads_a_two_row_header_groups_and_footnote_numbers():
    data = (
        b"2019 Mining Production,,,\n"
        b"Commodity ,Unit,2018-19,\n"
        b",,Produced,Sold\n"
        b"Metallic Minerals,,,\n"
        b"Gold 7,Grams,857,0\n"
        b"Bauxite,Tonnes,12,11\n"
        b"Energy Minerals,,,\n"
        b"Uranium Oxide,Tonnes,2094,1398\n"
        b"Explanatory Notes,,,\n"
        b"7. Pure gold.,,,\n"
    )
    h, rows = f._stack_rows(
        data,
        "a.csv",
        "Commodity",
        header_depth=2,
        group_match=r"^(Metallic|Energy) Minerals$",
        footnote_marks=True,
    )
    assert h == ["Group", "Commodity", "Unit", "Produced", "Sold", "Note"]
    assert rows == [
        ["Metallic Minerals", "Gold", "Grams", "857", "0", "7"],
        ["Metallic Minerals", "Bauxite", "Tonnes", "12", "11", ""],
        ["Energy Minerals", "Uranium Oxide", "Tonnes", "2094", "1398", ""],
    ]
