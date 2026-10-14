import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from publicdata import __main__ as cli
from publicdata import catalogue
from publicdata.directory import plan, render
from publicdata.publishers import load_curated, resolve
from publicdata.register import load

if TYPE_CHECKING:
    import requests

    from publicdata.catalogue import AihwResource, Record
    from publicdata.jsontypes import JSONObject
    from publicdata.publishers import CatalogueRecord
    from publicdata.store import PortalStats

FIX = Path(__file__).parent / "fixtures" / "catalogue"
AIHW = catalogue.BY_CODE["aihw"]
CURATED = Path(__file__).parents[2] / "register" / "publishers"


def res(path: str, report: str = "", kind: str | None = "XLSX", **kw: str) -> AihwResource:
    """A listed file, shaped as the listing gives one."""
    x: AihwResource = {"resultTitle": f"Data tables: {path}", "resultUrl": path,
                       "resultFileType": kind, "resultDateTimeFormatted": "2026-01-01"}  # fmt: skip
    if report:
        x["relatedReportUrl"], x["relatedReportTitle"] = report, f"Report {report}"
    return cast("AihwResource", {**x, **kw})


class Resp:
    def __init__(self, body: object) -> None:
        self.body = body
        self.status_code = 200

    def json(self) -> object:
        return self.body

    def raise_for_status(self) -> None:
        pass


class AihwSession:
    """Serves the recorded AIHW resource list (13 Oct 2026) in pages of the size asked for."""

    def __init__(
        self, extra: list[AihwResource] | None = None, *, fixture: bool = True, short: int = 0
    ) -> None:
        self.headers: dict[str, str] = {}
        self.bodies: list[JSONObject] = []
        page = json.loads((FIX / "aihw-resources.json").read_text(encoding="utf-8"))
        self.rows: list[AihwResource] = (page["results"] if fixture else []) + (extra or [])
        # Rows the listing counts in totalResults but never serves.
        self.short = short

    def post(
        self,
        url: str,
        json: JSONObject | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        assert url == catalogue.AIHW_LISTING
        assert json is not None
        self.bodies.append(json)
        size, page = cast("int", json["itemsPerPage"]), cast("int", json["page"])
        rows = self.rows[(page - 1) * size : page * size]
        return Resp({"totalResults": len(self.rows) + self.short, "results": rows})


class AbsSession:
    """One ABS dataflow, to compare record shapes with."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: object = None, **kw: object) -> Resp:
        return Resp({"data": {"dataflows": [{"id": "X", "name": "X"}]}})


def harvest(s: AihwSession) -> dict[str, Record]:
    recs, dropped = catalogue.aihw(AIHW, cast("requests.Session", s), log=lambda *_: None)
    assert dropped == 0
    return {r["name"]: r for r in recs}


def test_aihw_files_are_grouped_into_one_record_per_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalogue, "AIHW_PAGE_SIZE", 4)
    s = AihwSession()
    by = harvest(s)
    # Six listed resources are four reports and one file that names no report. The 2018 report
    # lists only a link to its data page, so it has nothing to download and is left out.
    assert len(by) == 4
    assert "breastscreen-australia-monitoring-report-2018" not in by
    assert [b["page"] for b in s.bodies] == [1, 2]
    assert all(b["showReportLink"] is True for b in s.bodies)
    bs20 = by["breastscreen-australia-monitoring-report-2020"]
    assert bs20["id"] == "aihw-breastscreen-australia-monitoring-report-2020"
    assert bs20["title"] == "BreastScreen Australia monitoring report 2020"
    assert (
        bs20["url"]
        == "https://www.aihw.gov.au/reports/cancer-screening/breastscreen-australia-monitoring-report-2020"
    )
    assert bs20["formats"] == ["XLS"]
    assert bs20["downloadable"]
    assert bs20["org"] == "aihw"
    assert bs20["org_title"] == "Australian Institute of Health and Welfare"
    assert bs20["licence"] == "CC-BY-4.0"
    assert bs20["open"] is True
    assert bs20["modified"] == "2020-10-08"
    assert "Indicator 1" in bs20["summary"]
    assert "Indicator 2" in bs20["summary"]
    assert len(bs20["files"]) == 2
    assert all(u.startswith("https://www.aihw.gov.au/getmedia/") for u in bs20["files"])
    # A file that names no report is listed at its own address under its media id.
    lone = by["52828fc4-6b20-44e8-a43e-c3573dafc998"]
    assert lone["title"] == "Data tables: Burden of Disease—Suicide and self-inflicted injuries"
    assert lone["url"].startswith("https://www.aihw.gov.au/getmedia/52828fc4-")
    assert lone["formats"] == ["XLSX"]
    abs_ = catalogue.sdmx(catalogue.BY_CODE["abs"], cast("requests.Session", AbsSession()))
    assert set(bs20) - {"files"} == set(abs_[0][0])


def test_a_resource_stating_other_terms_holds_its_report_back_from_open() -> None:
    third: AihwResource = {
        "resultTitle": "Data tables: BreastScreen Australia monitoring report 2020 - Indicator 9",
        "resultUrl": "/getmedia/00000000-0000-0000-0000-000000000009/indicator9.xls",
        "resultDateTimeFormatted": "2020-10-08",
        "resultDescription": "Contains third-party material reproduced with permission.",
        "resultFileType": "XLS",
        "relatedReportTitle": "BreastScreen Australia monitoring report 2020",
        "relatedReportUrl": "/reports/cancer-screening/breastscreen-australia-monitoring-report-2020",
    }
    nc: AihwResource = {
        "resultTitle": "Data tables: Burden of avoidable deaths extra",
        "resultUrl": "/getmedia/00000000-0000-0000-0000-00000000000a/extra.xlsx",
        "resultFileType": "XLSX",
        "schemaLicenseUrl": "https://creativecommons.org/licenses/by-nc/4.0/",
        "relatedReportTitle": "Burden of avoidable deaths among Aboriginal and Torres Strait Islander people 2018",
        "relatedReportUrl": "/reports/burden-of-disease/burden-of-deaths-first-nations-people-2018",
    }
    by = harvest(AihwSession([third, nc]))
    bs20 = by["breastscreen-australia-monitoring-report-2020"]
    assert bs20["open"] is False
    assert "third-party" in bs20["licence_title"]
    assert by["burden-of-deaths-first-nations-people-2018"]["licence"] == "CC-BY-NC-4.0"
    assert by["burden-of-deaths-first-nations-people-2018"]["open"] is False
    # The other reports keep the copyright page's licence.
    assert by["breastscreen-australia-monitoring-report-2022"]["open"] is True
    assert catalogue.aihw_terms({"resultUrl": "/x", "schemaLicenseUrl": " "}) == (
        "CC-BY-4.0",
        "Creative Commons Attribution 4.0 International",
    )
    for words in ("© Example Pty Ltd", "Terms of use apply", "Restricted access"):
        assert catalogue.aihw_terms({"resultUrl": "/x", "resultDescription": words})[0] == summary(
            words
        )


def summary(text: str) -> str:
    return catalogue.summary(text)


def test_a_report_takes_its_least_open_term_whatever_order_its_files_come_in() -> None:
    by_cc = "https://creativecommons.org/licenses/by/3.0/au/"
    by_nc = "https://creativecommons.org/licenses/by-nc/4.0/"
    rows = [
        res("/getmedia/a/open.xlsx", "/reports/x/mixed", schemaLicenseUrl=by_cc),
        res("/getmedia/b/nc.xlsx", "/reports/x/mixed", schemaLicenseUrl=by_nc),
        res("/getmedia/c/plain.xlsx", "/reports/x/older", schemaLicenseUrl=by_cc),
        res("/getmedia/d/plain.xlsx", "/reports/x/older"),
        res("https://example.org/elsewhere.csv", "/reports/x/hosted", "CSV"),
    ]
    for order in (rows, rows[::-1]):
        by = harvest(AihwSession(order, fixture=False))
        assert (by["mixed"]["licence"], by["mixed"]["open"]) == ("CC-BY-NC-4.0", False)
        # A stated licence that is open still names the report's licence over the default.
        assert (by["older"]["licence"], by["older"]["open"]) == ("CC-BY-3.0-AU", True)
        # The copyright page covers only the Institute's own site.
        assert (by["hosted"]["licence"], by["hosted"]["open"]) == ("", None)


def test_a_generic_slug_takes_its_parent_and_an_alias_merges_into_one_record() -> None:
    rows = [
        res("/getmedia/1/h.xlsx", "/reports/hospitals/data"),
        res("/getmedia/2/c.xlsx", "/reports/cancer/data"),
        res("/getmedia/3/s.xlsx", "/suicide-self-harm-monitoring/resources/download-data-tables"),
        res("/getmedia/4/m.xlsx", "/reports/medicare/mbs-funded-services-data"),
        res("/getmedia/5/m.xls", "/reports/health-welfare-expenditure/mbs-funded-services-data", "XLS"),
    ]  # fmt: skip
    first = harvest(AihwSession(rows, fixture=False))
    assert sorted(first) == [
        "cancer-data",
        "hospitals-data",
        "mbs-funded-services-data",
        "suicide-self-harm-monitoring-resources-download-data-tables",
    ]
    mbs = first["mbs-funded-services-data"]
    assert mbs["formats"] == ["XLS", "XLSX"]
    assert (
        mbs["url"]
        == "https://www.aihw.gov.au/reports/health-welfare-expenditure/mbs-funded-services-data"
    )
    # The merged record is the same whichever alias the listing gives first.
    assert harvest(AihwSession(rows[::-1], fixture=False)) == first


def test_a_report_with_nothing_to_download_is_left_out() -> None:
    rows = [res("/getmedia/1/r.pdf", "/reports/x/publication", "PDF"),
            res("/getmedia/2/t.xlsx", "/reports/x/tables")]  # fmt: skip
    assert list(harvest(AihwSession(rows, fixture=False))) == ["tables"]


def test_an_empty_page_before_the_count_is_reached_fails_the_read() -> None:
    with pytest.raises(catalogue.PortalError, match="page 2 is empty at 6 of 7"):
        harvest(AihwSession(short=1))


def test_a_request_never_sends_query_parameters_and_a_body_together() -> None:
    with pytest.raises(TypeError, match="not both"):
        catalogue._get(cast("requests.Session", None), "https://x", {"a": 1}, body={"b": 2})


def test_a_live_entry_fetching_an_aihw_file_or_data_page_marks_its_report_served() -> None:
    reg = {d.slug: d for d in load(cli.REGISTER)}
    suicide, shs = reg["au-suicide-deaths-by-state"], reg["au-homelessness-services-monthly"]
    assert (suicide.status, shs.status) == ("live", "live")
    rows = [
        res(suicide.source.url.removeprefix("https://www.aihw.gov.au"),
            "/suicide-self-harm-monitoring/data/data-downloads"),
        res("/getmedia/9/shs.xlsx", shs.source.url.removeprefix("https://www.aihw.gov.au").removesuffix("/data")),
        res("/getmedia/8/other.xlsx", "/reports/x/unserved"),
    ]  # fmt: skip
    recs = list(harvest(AihwSession(rows, fixture=False)).values())
    d = plan([suicide, shs], recs, load_curated(CURATED), "2026-10-14")
    assert sorted(d.served) == [
        "aihw-specialist-homelessness-services-monthly-data",
        "aihw-suicide-self-harm-monitoring-data-data-downloads",
    ]
    assert d.served["aihw-specialist-homelessness-services-monthly-data"] == f"/d/{shs.slug}/"


def test_a_planned_entry_fetching_an_aihw_file_or_data_page_claims_its_report() -> None:
    reg = {d.slug: d for d in load(cli.REGISTER)}
    shs = dataclasses.replace(reg["au-homelessness-services-monthly"], status="backlog")
    path = shs.source.url.removeprefix("https://www.aihw.gov.au").removesuffix("/data")
    rows = [res("/getmedia/9/shs.xlsx", path)]
    recs = list(harvest(AihwSession(rows, fixture=False)).values())
    d = plan([shs], recs, load_curated(CURATED), "2026-10-14")
    assert d.chosen == {"aihw-specialist-homelessness-services-monthly-data": shs.slug}


def test_a_long_report_path_keeps_a_distinct_vote_key() -> None:
    base = "/reports/x/" + "a" * 70
    a, b = catalogue._aihw_id(base + "-2020"), catalogue._aihw_id(base + "-2021")
    assert a != b
    assert len(catalogue.record_id("aihw", a)) <= 64
    assert catalogue.record_id("aihw", a).endswith(a[-8:])


def test_keys_that_fold_to_one_record_id_are_one_record() -> None:
    long = "b" * 70
    rows = [
        res("/getmedia/1/a.xlsx", "/reports/x/Mental_Health"),
        res("/getmedia/2/b.xlsx", "/reports/y/mental-health"),
        # Topic aliases of a long slug, one of them in capitals.
        res("/getmedia/3/c.xlsx", f"/reports/topic-a/{long}"),
        res("/getmedia/4/d.xlsx", f"/reports/topic-b/{long.upper()}"),
    ]
    by = {r["id"]: r for r in harvest(AihwSession(rows, fixture=False)).values()}
    assert len(by) == 2
    assert len(by["aihw-mental-health"]["files"]) == 2
    (other,) = (r for i, r in by.items() if i != "aihw-mental-health")
    assert len(other["files"]) == 2
    assert other["id"] == catalogue.record_id("aihw", other["name"])
    assert {r["id"]: r for r in harvest(AihwSession(rows[::-1], fixture=False)).values()} == by


def test_a_file_on_another_site_keeps_that_site_in_its_key() -> None:
    rows = [
        res("https://example.org/data/tables.csv", kind="CSV"),
        res("https://other.example.net/data/tables.csv", kind="CSV"),
        res("/getmedia/7/tables.csv", "/reports/x/tables", "CSV"),
    ]
    ids = sorted(r["id"] for r in harvest(AihwSession(rows, fixture=False)).values())
    assert ids == [
        "aihw-example-org-tables-csv",
        "aihw-other-example-net-tables-csv",
        "aihw-tables",
    ]


def test_aihw_replaces_only_the_data_gov_au_copies_whose_titles_it_lists() -> None:
    own = harvest(AihwSession())["breastscreen-australia-monitoring-report-2022"]

    def copy(i: int, title: str, org: str = "aihw") -> Record:
        return catalogue._record(catalogue.BY_CODE["gov"], source_id=f"c{i}", name=f"c{i}", title=title,
                                 org=org, org_title="AIHW", kind="dataset", licence="CC-BY-3.0-AU",
                                 licence_title="", formats=["CSV"], created="", modified="", url="",
                                 summary="", harvested_from="")  # fmt: skip

    same = copy(1, "BreastScreen Australia Monitoring Report 2022")
    other = copy(2, "Health expenditure in Australia")
    aurin = copy(3, "BreastScreen Australia monitoring report 2022", "aurin-copy")
    stats: dict[str, PortalStats] = {"gov": {}}
    kept = catalogue._drop_copies([own, same, other, aurin], catalogue.PORTALS, stats)
    assert [r["id"] for r in kept] == [own["id"], other["id"], aurin["id"]]
    assert stats["gov"]["dropped_duplicates"] == 1


def test_aihw_records_sit_under_the_curated_aihw_publisher() -> None:
    curated = load_curated(CURATED)
    recs: list[CatalogueRecord] = [
        {"portal": "aihw", "org": "aihw", "org_title": AIHW.publisher},
        {"portal": "gov", "org": "aihw", "org_title": "Australian Institute of Health and Welfare"},
    ]
    _pubs, by_org = resolve(recs, curated, {p.code: p.jurisdiction for p in catalogue.PORTALS})
    assert by_org["aihw:aihw"] is by_org["gov:aihw"]
    assert by_org["aihw:aihw"].slug == "australian-institute-of-health-and-welfare"


def test_a_vote_on_a_replaced_data_gov_au_aihw_record_follows_to_the_aihw_report() -> None:
    own = harvest(AihwSession())["breastscreen-australia-monitoring-report-2022"]
    copy = catalogue._record(catalogue.BY_CODE["gov"], source_id="00000001-aaaa-bbbb-cccc-dddddddddddd",
                             name="breastscreen-2022", title="BreastScreen Australia monitoring report 2022",
                             org="aihw", org_title="AIHW", kind="dataset", licence="CC-BY-3.0-AU",
                             licence_title="", formats=["CSV"], created="", modified="",
                             url="https://data.gov.au/data/dataset/breastscreen-2022", summary="",
                             harvested_from="")  # fmt: skip
    moved: dict[str, str] = {}
    kept = catalogue._drop_copies([own, copy], catalogue.PORTALS, {}, moved)
    assert moved == {copy["id"]: own["id"]}
    recs = catalogue._carry(kept, [], moved, [])
    assert [(r["id"], r.get("copies")) for r in recs] == [(own["id"], [copy["id"]])]
    d = plan([], recs, load_curated(CURATED), "2026-10-14")
    written: dict[str, str] = {}
    render(d, lambda *a, **k: None, written.__setitem__, {}, lambda c: {})
    shard = next(v for k, v in written.items() if k.startswith("catalogue/copies/gov-"))
    assert json.loads(shard) == {copy["id"]: own["id"]}
    votable = next(
        json.loads(v) for k, v in written.items() if k.startswith("catalogue/votable/aihw-")
    )
    assert votable[own["id"]][4] == [copy["id"]]


def test_a_served_data_gov_au_aihw_record_dropped_by_title_stays_served_on_the_aihw_record() -> (
    None
):
    youth = {d.slug: d for d in load(cli.REGISTER)}["au-youth-detention-population-quarterly"]
    assert youth.status == "live"
    assert youth.source.package == "youth-justice-detention-data"
    row = res("/getmedia/7/yj.xlsx", "/reports/youth-justice/youth-justice-detention-data-2026",
              relatedReportTitle="Youth Justice Detention Data")  # fmt: skip
    own = harvest(AihwSession([row], fixture=False))["youth-justice-detention-data-2026"]
    copy = catalogue._record(catalogue.BY_CODE["gov"], source_id="469ea5ba-2ab3-4972-a045-52bfc87fc512",
                             name=youth.source.package, title="Youth Justice Detention Data", org="aihw",
                             org_title="AIHW", kind="dataset", licence="CC-BY-3.0-AU", licence_title="",
                             formats=["CSV"], created="", modified="",
                             url=catalogue.BY_CODE["gov"].landing(youth.source.package), summary="",
                             harvested_from="")  # fmt: skip
    moved: dict[str, str] = {}
    kept = catalogue._drop_copies([own, copy], catalogue.PORTALS, {}, moved)
    pages = {r["id"]: r["url"] for r in (own, copy)}
    recs = catalogue._carry(kept, [], moved, [], pages)
    assert [r["id"] for r in recs] == [own["id"]]
    assert recs[0]["copy_pages"] == {copy["id"]: copy["url"]}
    d = plan([youth], recs, load_curated(CURATED), "2026-10-15")
    assert d.served == {own["id"]: f"/d/{youth.slug}/"}
