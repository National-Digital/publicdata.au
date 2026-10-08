import pytest
import yaml

from publicdata.fetch import LicenceDrift, check_licence, normalise_licence_id, parse_as_at
from publicdata.register import Field

from .conftest import ROOT, make_dataset


def test_licence_drift_stops_the_run_even_with_unchanged_bytes():
    ds = make_dataset([Field("a", "A")])
    check_licence(ds, {"id": "CC-BY-4.0", "title": "Creative Commons Attribution 4.0"})
    check_licence(ds, {"id": "cc-by-4.0"})
    with pytest.raises(LicenceDrift):
        check_licence(ds, {"id": "CC-BY-NC-4.0", "title": "Attribution-NonCommercial"})
    with pytest.raises(LicenceDrift):
        check_licence(ds, {"id": "", "title": ""})


def test_a_generic_licence_code_means_what_its_portal_defines():
    assert normalise_licence_id("cc-by", "https://data.gov.au/data") == "CC-BY-3.0-AU"
    assert normalise_licence_id("cc-by-sa", "https://data.gov.au/data/") == "CC-BY-SA-3.0-AU"
    assert normalise_licence_id("cc-by", "https://catalogue.data.wa.gov.au") == "CC-BY-4.0"
    assert normalise_licence_id("cc-by", "https://data.sa.gov.au/data") == "CC-BY-4.0"
    assert normalise_licence_id("cc-by", "https://discover.data.vic.gov.au") == "CC-BY-4.0"
    assert normalise_licence_id("CC-BY-3.0", "https://www.data.qld.gov.au") == "CC-BY-3.0-AU"
    assert normalise_licence_id("cc-at-4", "https://data.nsw.gov.au/data") == "CC-BY-4.0"
    assert normalise_licence_id("CC-BY-4.0") == "CC-BY-4.0"
    assert normalise_licence_id("cc-zero") == "CC0-1.0"


def test_a_generic_code_no_portal_versions_is_refused():
    # NSW and NT point cc-by at the unversioned opendefinition page; no version may be guessed.
    assert normalise_licence_id("cc-by", "https://data.nsw.gov.au/data") == ""
    assert normalise_licence_id("cc-by", "https://data.nt.gov.au") == ""
    assert normalise_licence_id("cc-by") == ""


def test_as_at_from_publishers_words():
    rx = r"to:?[\s•]*(\d{1,2} [A-Za-z]+ \d{4})"
    assert parse_as_at("1 January 2001 to:\r\n•\t30 June 2025 for fatal", rx) == "2025-06-30"
    assert parse_as_at("crashes 1 January 2001 to 30 June 2025.", rx) == "2025-06-30"
    assert parse_as_at("no date here", rx) == ""


def test_an_empty_or_html_answer_is_refused_not_stored():
    import pytest

    from publicdata.fetch import FetchError, expect_page

    class D:
        slug = "x"

    class R:
        status_code = 200

        def __init__(self, ctype):
            self.headers = {"Content-Type": ctype} if ctype else {}

    with pytest.raises(FetchError, match="returned no bytes"):
        expect_page(D(), "https://portal.example/file.csv", R(""), b"")
    with pytest.raises(FetchError, match="HTML page"):
        expect_page(
            D(),
            "https://portal.example/file.csv",
            R("text/html; charset=utf-8"),
            b"<html>blocked</html>",
        )
    expect_page(D(), "https://portal.example/file.csv", R("text/csv"), b"a,b\n1,2\n")


def test_one_failing_dataset_does_not_stop_the_others(monkeypatch, capsys):
    from publicdata import __main__ as cli
    from publicdata import fetch as f

    def fake(ds, store_dir):
        if ds.slug == "qld-road-casualties":
            msg = "qld-road-casualties: returned no bytes"
            raise f.FetchError(msg)

    monkeypatch.setattr(f, "fetch", fake)
    assert cli.main(["fetch", "qld-road-casualties", "qld-road-crash-factors"]) == 0
    out = capsys.readouterr().out
    assert "qld-road-casualties: FAILED" in out and "qld-road-crash-factors: unchanged" in out
    assert "0 new version(s), 1 failed" in out


def test_every_workflow_file_parses():
    # GitHub drops a workflow it cannot parse without a failing check, and the schedule stops.
    for f in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert d.get("jobs"), f.name


def test_a_second_file_on_a_taken_day_gets_the_next_free_day_and_says_why():
    import datetime as dt

    from publicdata.fetch import free_version

    assert free_version("2026-04-24", {"2026-01-01"}, dt.date(2026, 9, 30)) == ("2026-04-24", "")
    v, note = free_version("2026-04-24", {"2026-04-24"}, dt.date(2026, 9, 30))
    assert v == "2026-09-30" and "2026-04-24" in note
    v, _ = free_version("2026-09-30", {"2026-09-30", "2026-10-01"}, dt.date(2026, 9, 30))
    assert v == "2026-10-02"


def test_new_versions_are_grouped_by_government_for_their_own_pull_requests(monkeypatch, tmp_path):
    import json

    from publicdata import __main__ as cli
    from publicdata import fetch as f

    def fake(ds, store_dir):
        if ds.slug == "qld-road-crash-factors":
            msg = "qld-road-crash-factors: returned no bytes"
            raise f.FetchError(msg)

        class M:
            version, bytes, encoding, sha256 = "2026-10-01", 1, "utf-8", "0" * 64

        return M()

    monkeypatch.setattr(f, "fetch", fake)
    out = tmp_path / "groups.json"
    slugs = ["au-road-deaths", "qld-road-casualties", "qld-road-crash-factors"]
    cli.main(["fetch", *slugs, "--store", "store", "--groups", str(out)])
    assert json.loads(out.read_text()) == {
        "cth": ["store/au-road-deaths/2026-10-01/manifest.json"],
        "qld": ["store/qld-road-casualties/2026-10-01/manifest.json"],
    }


def test_the_newest_matching_resource_is_fetched():
    from publicdata.fetch import FetchError, pick_resource
    from publicdata.register import Source

    ds = make_dataset(
        [Field("a", "A")],
        source=Source(adapter="ckan-resource", url="u", package="p", resource_match=r"by LGA"),
    )
    resources = [
        {"id": "old", "name": "Recorded offences by LGA - Dec 2019", "created": "2020-05-06"},
        {"id": "state", "name": "Recorded offences - Jun 2026", "created": "2026-09-24"},
        {"id": "reimport", "name": "Recorded offences by LGA - Sep 2019", "created": "2020-05-06"},
        {"id": "new", "name": " Recorded offences by LGA - Jun 2026", "created": "2026-09-24"},
        {"id": "manual", "name": "Data manual by LGA", "created": "2021-01-10"},
    ]
    assert pick_resource(ds, resources)["id"] == "new"
    # A re-import that gives old files one creation date falls back to the order listed.
    assert pick_resource(ds, resources[:3])["id"] == "reimport"
    with pytest.raises(FetchError, match="no resource matches"):
        pick_resource(ds, resources[1:2])


def test_the_newest_matching_package_is_fetched():
    from publicdata.fetch import FetchError, pick_package
    from publicdata.register import Source

    ds = make_dataset(
        [Field("a", "A")],
        source=Source(
            adapter="ckan-resource",
            url="u",
            package_match=r"^current-nt-crime-statistics-",
            resource_match=r"\.csv$",
        ),
    )
    packages = [
        {"name": "current-nt-crime-statistics-june-2026", "metadata_created": "2026-08-17"},
        {"name": "current-nt-crime-statistics-july-2026", "metadata_created": "2026-09-14"},
        {"name": "nt-crime-statistics-archive", "metadata_created": "2026-09-20"},
    ]
    assert pick_package(ds, packages) == "current-nt-crime-statistics-july-2026"
    with pytest.raises(FetchError, match="no package matches"):
        pick_package(ds, packages[2:])


def test_a_package_pattern_searches_with_the_package_text():
    from publicdata.fetch import _package
    from publicdata.register import Source

    ds = make_dataset(
        [Field("a", "A")],
        source=Source(
            adapter="ckan-resource",
            url="u",
            package="current-nt-crime-statistics",
            package_match=r"^current-nt-crime-statistics-",
            resource_match=r"\.csv$",
        ),
    )
    calls = []

    class R:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    class S:
        def get(self, url, params=None, timeout=None):
            calls.append((url.rsplit("/", 1)[-1], params))
            if url.endswith("package_search"):
                names = [
                    "current-nt-crime-statistics-june-2026",
                    "current-nt-crime-statistics-july-2026",
                ]
                results = [
                    {"name": n, "metadata_created": f"2026-0{i + 8}-01"}
                    for i, n in enumerate(names)
                ]
                return R({"success": True, "result": {"results": results}})
            return R({"success": True, "result": {"name": params["id"], "resources": []}})

    assert _package(ds, S(), "https://portal/api/3/action")["name"].endswith("july-2026")
    assert calls[0] == (
        "package_search",
        {"q": "current-nt-crime-statistics", "sort": "metadata_created desc", "rows": 100},
    )
    assert calls[1] == ("package_show", {"id": "current-nt-crime-statistics-july-2026"})


def test_a_file_with_no_extension_is_named_from_the_resource_format():
    from publicdata.fetch import resource_filename

    page = "https://www.dffh.vic.gov.au/moving-annual-rent-suburb-september-quarter-2025-excel"
    assert resource_filename({"url": page, "format": "XLSX"}).endswith("-excel.xlsx")
    assert resource_filename({"url": "https://x/a/data.csv", "format": "XLSX"}) == "data.csv"
    script = "https://ftp-dlrm.nt.gov.au/main.html?download&weblink=aac4&realfilename=Wildlife_CrocCaptureZones.zip"
    assert resource_filename({"url": script, "format": "ZIP"}) == "Wildlife_CrocCaptureZones.zip"
    assert resource_filename({"url": "https://x/a/", "format": ".xlsx"}) == "source.xlsx"
    assert resource_filename({"url": "https://x/a/data", "format": ""}) == "data"


def test_the_user_agent_names_the_site_in_a_form_firewalls_accept():
    from publicdata.fetch import UA

    # dffh.vic.gov.au resets the connection for either of these.
    assert "publicdata.au/about" in UA
    assert "fetcher" not in UA.lower() and "://" not in UA


class _Resp:
    def __init__(self, body, ctype="application/json", status=200):
        self._body = body
        self.status_code = status
        self.headers = {"Content-Type": ctype}

    def json(self):
        return self._body

    @property
    def content(self):
        return self._body if isinstance(self._body, bytes) else str(self._body).encode()

    def raise_for_status(self):
        pass


class _Session:
    """Answers each URL from a table and records what was asked, so an adapter's reads are
    checked without a network.
    """

    def __init__(self, table):
        self.table, self.calls, self.headers = table, [], {}

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        self.calls.append((url, params or {}))
        for key, fn in self.table.items():
            if url.startswith(key):
                return fn(params or {})
        msg = f"unexpected url {url}"
        raise AssertionError(msg)


def test_arcgis_feature_pages_the_layer_in_id_order_into_one_geojson(tmp_path):
    from publicdata.fetch import arcgis_feature
    from publicdata.register import Source

    ds = make_dataset(
        [Field("id", "ID", "integer")],
        source=Source(
            adapter="arcgis-feature",
            url="https://gis.example.gov.au/rest/services/CRASH/FeatureServer/0",
            portal="https://data.gov.au/data",
            package="tasmanian",
        ),
    )
    pkg = {
        "success": True,
        "result": {
            "name": "tasmanian",
            "id": "p1",
            "license_id": "cc-by-4.0",
            "license_title": "CC BY 4.0",
            "resources": [],
            "notes": "",
            "metadata_modified": "2023-08-11T14:39:25",
        },
    }
    info = {
        "name": "CDM_CRASH",
        "objectIdField": "ID",
        "maxRecordCount": 2,
        "fields": [
            {"name": "ID", "type": "esriFieldTypeOID"},
            {"name": "WHEN", "type": "esriFieldTypeDate"},
        ],
    }

    def feats(offset):
        rows = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [147.3, -42.9]},
                "properties": {"ID": i, "WHEN": 1364602560000},
            }
            for i in range(1, 4)
        ]
        return {"type": "FeatureCollection", "features": rows[offset : offset + 2]}

    s = _Session(
        {
            "https://data.gov.au/data/api/3/action/package_show": lambda p: _Resp(pkg),
            "https://gis.example.gov.au/rest/services/CRASH/FeatureServer/0/query": lambda p: _Resp(
                feats(int(p["resultOffset"]))
            ),
            "https://gis.example.gov.au/rest/services/CRASH/FeatureServer/0": lambda p: _Resp(info),
        }
    )
    data, m, licence = arcgis_feature(ds, tmp_path, s)
    check_licence(ds, licence)
    queries = [p for u, p in s.calls if u.endswith("/query")]
    assert [q["resultOffset"] for q in queries] == [0, 2]
    assert all(q["orderByFields"] == "ID" and q["outSR"] == 4326 for q in queries)
    import json

    fc = json.loads(data)
    assert [f["properties"]["ID"] for f in fc["features"]] == [1, 2, 3]
    assert m.filename == "t.geojson" and m.source["features"] == 3
    assert m.source["date_fields"] == ["WHEN"] and m.source["last_edit_date"] is None
    # No edit date on the layer, so the newest record dates the version: 1364602560000 ms is
    # 2013-03-30 in Brisbane.
    assert m.version == "2013-03-30" and m.source["newest_record"] == 1364602560000
    assert any("newest record" in n for n in m.notes)
    assert any("no file" in n for n in m.notes)
    # The same layer gives the same bytes, so an unchanged layer is no version.
    from publicdata import store

    store.write(tmp_path, m, data)
    again, m2, _ = arcgis_feature(ds, tmp_path, s)
    assert again is None and m2.version == m.version


def _ala_session(rows, lat_of=lambda r: r[2]):
    """A fake Atlas: rows per provider as (uuid, loaded, latitude, year, extra), filtered by the
    fq conditions the adapter sends.
    """
    from publicdata.fetch import ALA_DEEP, ALA_PAGE

    def rng(f):
        lo, hi = f.split("[", 1)[1].rstrip("]}").split(" TO ")
        return lo, hi, f.endswith("}")

    def keep(r, f):
        if f.startswith("first_loaded_date:"):
            lo, hi, ex = rng(f)
            t = r[1][:19] + "Z"
            return lo <= t < hi if ex else lo <= t <= hi
        if f == "-decimalLatitude:*":
            return lat_of(r) is None
        if f == "decimalLatitude:*":
            return lat_of(r) is not None
        if f.startswith("decimalLatitude:[") or f.startswith("decimalLongitude:["):
            lo, hi, ex = rng(f)
            v = lat_of(r) if f.startswith("decimalLat") else 150.0
            return v is not None and (
                float(lo) <= v < float(hi) if ex else float(lo) <= v <= float(hi)
            )
        if f.startswith("year:["):
            lo, hi, ex = rng(f)
            y = r[3]
            return int(lo) <= y < int(hi) if ex else int(lo) <= y <= int(hi)
        return True

    def search(params):
        fqs = params.get("fq") or []
        uid = next(f.split(":", 1)[1] for f in fqs if f.startswith("dataResourceUid:"))
        out = [r for r in rows[uid] if all(keep(r, f) for f in fqs)]
        if params.get("pageSize") == 0:
            return _Resp({"totalRecords": len(out)})
        assert params["pageSize"] == ALA_PAGE and params["startIndex"] < ALA_DEEP
        # Pages sorted by load date skip rows across a tie; only the unique id is stable.
        assert params["sort"] == "id"
        assert "catalogNumber" in params["fl"] and "raw_" not in params["fl"]
        page = out[params["startIndex"] : params["startIndex"] + ALA_PAGE]
        return _Resp(
            {
                "occurrences": [
                    {
                        "uuid": k,
                        "decimalLatitude": lat,
                        "year": y,
                        **extra,
                        "otherProperties": {"firstLoadedDate": t, "locality": "near a road"},
                    }
                    for k, t, lat, y, extra in page
                ]
            }
        )

    return _Session({"https://api.example.org/occurrences/search": search})


def _ala_dataset(providers=("dr1", "dr2")):
    from publicdata.register import Source

    return make_dataset(
        [Field("record_id", "uuid", "string")],
        source=Source(
            adapter="ala",
            url="https://api.example.org/occurrences/search",
            search="genus:Eucalyptus",
            providers=providers,
        ),
    )


def test_ala_reads_each_provider_in_slices_and_dates_the_version_by_the_newest_load(tmp_path):
    from publicdata import store
    from publicdata.fetch import ALA_DEEP, ala

    ds = _ala_dataset()
    # dr1 has 3 rows, one a specimen. dr2 has more than the paging limit, half of them loaded in
    # one second and one without coordinates, so the reads split by load time, then by latitude.
    big = 2 * ALA_DEEP + 2
    spec = {
        "raw_catalogNumber": "MEL 1",
        "raw_institutionCode": "MEL",
        "license": "CC-BY 4.0 (Int)",
    }
    rows = {
        "dr1": [
            ("a1", "2020-01-01T00:00:00.000+00:00", -30.0, 2020, spec),
            ("a2", "2021-06-01T00:00:00.000+00:00", -31.0, 2021, {"license": "CC0"}),
            ("a3", "2019-01-01T00:00:00.000+00:00", -32.0, 2019, {"license": "CC0"}),
        ],
        "dr2": [
            (
                f"b{i:05d}",
                ("2010-01-01" if i % 2 else "2024-03-01") + "T00:00:00.000+00:00",
                None if i == 7 else -10.0 - (i % 300) * 0.1,
                2000,
                {"license": "CC0"},
            )
            for i in range(big)
        ],
    }
    s = _ala_session(rows)
    data, m, licence = ala(ds, tmp_path, s)
    lines = data.decode().splitlines()
    assert lines[0].startswith("uuid,occurrenceID,raw_catalogNumber,raw_institutionCode,")
    assert lines[0].endswith(",locality,occurrenceStatus,firstLoadedDate")
    assert len(lines) == 1 + 3 + big
    assert lines[1].startswith("a1,,MEL 1,MEL,") and lines[1].endswith(
        ",near a road,,2020-01-01T00:00:00Z"
    )
    assert m.version == "2024-03-01"
    assert m.source["providers"] == {"dr1": {"open": 3, "all": 3}, "dr2": {"open": big, "all": big}}
    assert m.source["licences"] == {"CC-BY 4.0 (Int)": 1, "CC0": 2 + big}
    assert licence["id"] == "CC-BY-4.0" and m.filename == f"{ds.slug}.csv"
    assert any("government providers" in n for n in m.notes)
    assert all("license:(" in str(c[1].get("fq")) for c in s.calls if c[1].get("pageSize"))
    store.write(tmp_path, m, data)
    assert ala(ds, tmp_path, s)[0] is None


def test_ala_splits_one_place_and_second_by_year_and_refuses_what_it_cannot_read(tmp_path):
    from publicdata.fetch import ALA_DEEP, FetchError, ala

    ds = _ala_dataset(providers=("dr1",))
    same = ("2013-04-09T10:58:31.000+00:00", -33.0)
    rows = {
        "dr1": [
            (f"c{i:05d}", same[0], same[1], 1990 + (i % 30), {"license": "CC0"})
            for i in range(ALA_DEEP + 500)
        ]
    }
    data, m, _ = ala(ds, tmp_path, _ala_session(rows))
    assert len(data.decode().splitlines()) == 1 + ALA_DEEP + 500
    rows["dr1"] = [
        (f"d{i:05d}", same[0], same[1], 1999, {"license": "CC0"}) for i in range(ALA_DEEP + 1)
    ]
    with pytest.raises(FetchError, match="cannot be read"):
        ala(ds, tmp_path, _ala_session(rows))


def test_ala_stops_when_a_provider_loses_open_rows_but_not_rows(tmp_path):
    from publicdata import store
    from publicdata.fetch import LicenceDrift, ala

    ds = _ala_dataset(providers=("dr1",))
    rows = {
        "dr1": [
            (f"e{i}", "2020-01-01T00:00:00.000+00:00", -30.0, 2020, {"license": "CC0"})
            for i in range(4)
        ]
    }
    data, m, _ = ala(ds, tmp_path, _ala_session(rows))
    store.write(tmp_path, m, data)
    # The provider still has four rows, but the licence filter now finds three.

    class Narrow(_Session):
        def get(self, url, params=None, timeout=None, allow_redirects=True):
            fqs = (params or {}).get("fq") or []
            if params.get("pageSize") == 0 and any(f.startswith("license:") for f in fqs):
                return _Resp({"totalRecords": 3})
            return super().get(url, params, timeout, allow_redirects)

    s = _ala_session(rows)
    with pytest.raises(LicenceDrift, match="licence has changed"):
        ala(ds, tmp_path, Narrow(s.table))


def test_an_etag_loses_its_quotes_and_keeps_its_weak_mark():
    from publicdata.fetch import etag

    assert etag({"ETag": '"abc-1"'}) == "abc-1"
    assert (
        etag({"ETag": 'W/"1501050059.0-253909-3591704692"'}) == "W/1501050059.0-253909-3591704692"
    )
    assert etag({}) == ""
