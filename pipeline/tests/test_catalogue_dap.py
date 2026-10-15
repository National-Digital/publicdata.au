import datetime as dt
import itertools
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import requests

from publicdata import catalogue
from publicdata.directory import plan, render
from publicdata.publishers import load_curated, resolve

from .test_catalogue import FakeSession, Resp, pkg

if TYPE_CHECKING:
    from publicdata.catalogue import Params, Record
    from publicdata.store import PortalStats

FIX = Path(__file__).parent / "fixtures" / "catalogue"
DAP = catalogue.BY_CODE["csiro"]
EMPTY: dict[str, object] = {"totalResults": "0", "next": None, "dataCollections": []}


def _fix(name: str) -> object:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _day(stamp: object) -> dt.date:
    return dt.date.fromisoformat(str(stamp)[:10])


class DapSession:
    """Serves recorded Data Access Portal answers (14 and 15 Oct 2026), trimmed to seven collections.

    A window wider than a year answers as the whole portal does, past the search limit. The
    window holding December 2025 answers the two recorded pages and every other window is empty.
    A full record answers as recorded for the collections that have one.
    """

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[Params] = []
        self.details: list[str] = []

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        assert headers == {"Accept": "application/json"}
        assert timeout == catalogue.DAP_TIMEOUT
        if url != "https://data.csiro.au/dap/ws/v2/collections":
            n = url.removeprefix("https://data.csiro.au/dap/ws/v2/collections/")
            self.details.append(n)
            return Resp(_fix(f"dap-detail-{n}.json"))
        assert params is not None
        self.calls.append(dict(params))
        first, last = _day(params["psd"]), _day(params["ped"])
        if (last - first).days > 365:
            return Resp(_fix("dap-over-limit.json"))
        if not first <= dt.date(2025, 12, 15) <= last:
            return Resp(EMPTY)
        return Resp(_fix(f"dap-page-{params['p']}.json"))


def _harvest(previous: list[Record] | None = None) -> tuple[list[Record], DapSession]:
    s = DapSession()
    recs, dropped = catalogue.dap(
        DAP, cast("requests.Session", s), log=lambda *_: None, previous=previous or []
    )
    assert dropped == 0
    return recs, s


def test_dap_splits_windows_past_the_search_limit_and_pages_each_in_full() -> None:
    recs, s = _harvest()
    assert len(recs) == 7
    assert all(c["rpp"] == 100 and c["sb"] == "RECENT" for c in s.calls)
    # A window ends at its last millisecond so a collection published at 23:59:59.5 is in it.
    assert all(str(c["ped"]).endswith("T23:59:59.999+10:00") for c in s.calls)
    # The pages of the window that holds the records follow its next link to the end.
    held = [c for c in s.calls if _day(c["psd"]) <= dt.date(2025, 12, 15) <= _day(c["ped"])]
    assert [c["p"] for c in held if (_day(c["ped"]) - _day(c["psd"])).days <= 365] == [1, 2]
    # The windows that were read cover every day from the first to tomorrow once.
    read = sorted(
        (_day(c["psd"]), _day(c["ped"]))
        for c in s.calls
        if c["p"] == 1 and (_day(c["ped"]) - _day(c["psd"])).days <= 365
    )
    assert read[0][0] == catalogue.DAP_FIRST
    assert read[-1][1] == dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)
    assert all(b[0] == a[1] + dt.timedelta(days=1) for a, b in itertools.pairwise(read))


def test_dap_stops_when_one_day_alone_is_past_the_limit() -> None:
    class Full(DapSession):
        def get(
            self,
            url: str,
            params: Params | None = None,
            timeout: float | None = None,
            headers: dict[str, str] | None = None,
        ) -> Resp:
            return Resp(_fix("dap-over-limit.json"))

    with pytest.raises(catalogue.PortalError, match="10000\\+ collections published on"):
        catalogue.dap(DAP, cast("requests.Session", Full()), log=lambda *_: None)


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        (
            {"errors": ["Search is unavailable"], "totalResults": "0", "dataCollections": []},
            "not a search answer",
        ),
        ({"dataCollections": []}, "not a search answer"),
        ({"totalResults": "5"}, "not a search answer"),
        ({"totalResults": "5", "next": None, "dataCollections": []}, "is empty of 5"),
        (EMPTY, "listed no collections"),
    ],
)
def test_dap_raises_on_an_answer_that_is_not_a_full_search(answer: object, says: str) -> None:
    class Odd(DapSession):
        def get(
            self,
            url: str,
            params: Params | None = None,
            timeout: float | None = None,
            headers: dict[str, str] | None = None,
        ) -> Resp:
            return Resp(answer)

    with pytest.raises(catalogue.PortalError, match=says):
        catalogue.dap(DAP, cast("requests.Session", Odd()), log=lambda *_: None)


def test_dap_records_carry_csiro_the_landing_page_and_the_stated_licence() -> None:
    recs, _ = _harvest()
    by = {r["name"]: r for r in recs}
    oat = by["csiro:72160"]
    assert oat["id"] == "csiro-72160"
    assert oat["org"] == "csiro"
    assert oat["org_title"] == "CSIRO"
    assert oat["url"] == "https://data.csiro.au/collection/csiro:72160"
    assert oat["kind"] == "dataset"
    assert oat["modified"] == "2025-12-31"
    assert oat["licence"] == "CC-BY-4.0"
    assert oat["open"] is True
    assert by["csiro:72235"]["licence"] == "CC-BY-SA-4.0"
    assert by["csiro:72235"]["open"] is True
    noncommercial = by["csiro:61066"]
    assert noncommercial["licence"] == "CC-BY-NC-4.0"
    assert noncommercial["open"] is False
    # A licence the portal leaves to the provider names no terms, so it is held as not open.
    provider = by["csiro:57058"]
    assert provider["licence"] == "Licence Defined by Data Provider"
    assert provider["open"] is False
    assert provider["kind"] == "service"
    assert set(recs[0]) - {"source_version", "file_count"} == set(
        catalogue.socrata(catalogue.BY_CODE["act"], cast("requests.Session", FakeSession([])))[0][0]
    )


def test_dap_is_downloadable_only_when_the_full_record_offers_the_public_files() -> None:
    recs, s = _harvest()
    by = {r["name"]: r for r in recs}
    # Full records are read for open, unrestricted data collections only, in id order.
    assert s.details == ["77136", "72752", "72235", "72744"]
    assert by["csiro:72160"]["downloadable"]
    assert by["csiro:72160"]["formats"] == ["API"]
    assert by["csiro:72160"]["file_count"] == 1
    assert by["csiro:72235"]["file_count"] == 4
    # Public with no files: the collection now points to another repository.
    assert by["csiro:60405"]["file_count"] == 0
    assert not by["csiro:60405"]["downloadable"]
    # The listing says unrestricted but the full record holds the files back until 2027.
    embargoed = by["csiro:72744"]
    assert not embargoed["downloadable"]
    assert (embargoed["file_count"], embargoed["public_from"]) == (0, "2027-09-30")
    # An open licence on files held back for request is still nothing to download.
    held = by["csiro:72198"]
    assert held["open"] is True
    assert not held["downloadable"]
    assert "file_count" not in held
    assert not by["csiro:57058"]["downloadable"]
    assert catalogue._embargo_end("restricted until 1 March 2030.") == "2030-03-01"
    assert catalogue._embargo_end("restricted until further notice") == ""


def test_dap_reads_a_full_record_again_only_when_its_version_or_embargo_changes() -> None:
    first, _ = _harvest()
    again, s = _harvest(first)
    assert s.details == []
    assert again == first
    changed: list[Record] = [
        {**r, "source_version": "0@2020-01-01"} if r["name"] == "csiro:72160" else r for r in first
    ]
    _, s = _harvest(changed)
    assert s.details == ["72752"]
    ended: list[Record] = [
        {**r, "public_from": "2026-01-01"} if r["name"] == "csiro:72744" else r for r in first
    ]
    _, s = _harvest(ended)
    assert s.details == ["72744"]


def test_dap_reads_at_most_its_cap_of_full_records_and_the_rest_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalogue, "DAP_DETAILS", 2)
    first, s = _harvest()
    assert s.details == ["77136", "72752"]
    by = {r["name"]: r for r in first}
    # Not read yet, so not downloadable yet.
    assert "file_count" not in by["csiro:72235"]
    assert not by["csiro:72235"]["downloadable"]
    second, s = _harvest(first)
    assert s.details == ["72235", "72744"]
    assert {r["name"]: r for r in second}["csiro:72235"]["downloadable"]


class Gone(Resp):
    def __init__(self) -> None:
        super().__init__({})
        self.status_code = 404

    def raise_for_status(self) -> None:
        msg = "404 Client Error"
        raise requests.HTTPError(msg)


class GoneDetail(DapSession):
    """The full record of collection 72752 (csiro:72160) answers 404."""

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        if url.endswith("/collections/72752"):
            self.details.append("72752")
            return Gone()
        return super().get(url, params, timeout, headers)


def test_a_full_record_that_cannot_be_read_leaves_its_collection_unread() -> None:
    lines: list[str] = []
    s = GoneDetail()
    recs, _ = catalogue.dap(DAP, cast("requests.Session", s), lines.append)
    assert s.details == ["77136", "72752", "72235", "72744"]
    by = {r["name"]: r for r in recs}
    assert "file_count" not in by["csiro:72160"]
    assert not by["csiro:72160"]["downloadable"]
    assert by["csiro:72235"]["file_count"] == 4
    assert any("csiro-72160" in x and "could not be read" in x for x in lines)
    # A collection read before keeps its answer, under the version it was read at.
    first, _ = _harvest()
    old = "0@2020-01-01"
    changed: list[Record] = [
        {**r, "source_version": old} if r["name"] == "csiro:72160" else r for r in first
    ]
    s = GoneDetail()
    again = {
        r["name"]: r
        for r in catalogue.dap(DAP, cast("requests.Session", s), lambda *_: None, changed)[0]
    }
    assert s.details == ["72752"]
    assert (again["csiro:72160"]["file_count"], again["csiro:72160"]["source_version"]) == (1, old)
    assert again["csiro:72160"]["downloadable"]


def test_a_collection_past_the_cap_keeps_its_earlier_answer_until_it_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _ = _harvest()
    old = "0@2020-01-01"
    changed: list[Record] = [
        {**r, "source_version": old} if r["name"] in ("csiro:72160", "csiro:72235") else r
        for r in first
    ]
    monkeypatch.setattr(catalogue, "DAP_DETAILS", 1)
    second, s = _harvest(changed)
    by = {r["name"]: r for r in second}
    assert len(s.details) == 1
    waiting = next(by[n] for n in ("csiro:72160", "csiro:72235") if by[n]["source_version"] == old)
    assert waiting["downloadable"]
    assert waiting["file_count"] > 0
    _, s = _harvest(second)
    assert len(s.details) == 1


def test_licence_ids_close_non_commercial_and_no_derivatives_however_spelt() -> None:
    lid = catalogue.licence_id
    assert lid("Creative Commons Attribution Non Commercial 4.0") == "CC-BY-NC-4.0"
    assert (
        lid("Creative Commons Attribution Noncommercial-Share Alike 4.0 Licence")
        == "CC-BY-NC-SA-4.0"
    )
    assert lid("Creative Commons Attribution No Derivatives 4.0") == "CC-BY-ND-4.0"
    assert lid("Creative Commons Attribution-NoDerivs 3.0 Australia") == "CC-BY-ND-3.0-AU"
    assert (
        lid("Creative Commons Attribution No Derivative Works 2.5 Australia") == "CC-BY-ND-2.5-AU"
    )
    assert (
        lid("Creative Commons Attribution-Noncommercial-No Derivatives 4.0 International Licence")
        == "CC-BY-NC-ND-4.0"
    )
    assert not any(
        catalogue.is_open(lid(t))
        for t in ("Creative Commons Attribution Non Commercial 4.0", "CC BY No Derivatives 4.0")
    )


def _gov(name: str, title: str, org: str = "csiro-data-access-portal") -> Record:
    return catalogue._record(catalogue.BY_CODE["gov"], source_id=name, name=name, title=title, org=org,
                             org_title="", kind="dataset", licence="", licence_title="notspecified",
                             formats=[], created="", modified="", url="", summary="", harvested_from="")  # fmt: skip


def test_data_gov_au_names_each_dap_copy_after_the_collection_s_pid() -> None:
    assert catalogue.named_copy(DAP, "fedora-pid_csiro-72160") == "csiro-72160"
    # A PID need not be a number, and both sides shape it into the same id.
    pid = "csiro:MDBSY_Reporting_Regions"
    assert (
        catalogue.named_copy(DAP, "fedora-pid_csiro-mdbsy_reporting_regions")
        == "csiro-mdbsy-reporting-regions"
    )
    assert (
        catalogue.record_id("csiro", pid.removeprefix("csiro:")) == "csiro-mdbsy-reporting-regions"
    )
    assert catalogue.named_copy(DAP, "some-other-name") == ""
    assert catalogue.named_copy(catalogue.BY_CODE["gov"], "fedora-pid_csiro-72160") == ""


def test_data_gov_au_copies_of_the_dap_are_dropped_and_its_other_csiro_records_kept() -> None:
    recs, _ = _harvest()
    title = recs[0]["title"]
    by_title = _gov("a-copy-named-otherwise", title.upper())
    by_id = _gov("fedora-pid_csiro-72744", "A title the portal has since changed")
    # A copy whose name gives a PID the portal does not list stays, whatever its title.
    gone = _gov("fedora-pid_csiro-1", title)
    marlin = _gov("fedora-pid_csiro-72160", title, "csiro-marlin-data-catalogue")
    stats: dict[str, PortalStats] = {}
    moved: dict[str, str] = {}
    records = [*recs, by_title, by_id, gone, marlin]
    kept = catalogue._drop_copies(records, catalogue.PORTALS, stats, moved)
    assert by_title not in kept
    assert by_id not in kept
    assert gone in kept
    assert marlin in kept
    assert stats["gov"]["dropped_duplicates"] == 2
    # The id in a copy's name decides where it moves, even when its title has changed.
    assert moved == {by_title["id"]: recs[0]["id"], by_id["id"]: "csiro-72744"}


class GovAndDap(DapSession):
    """data.gov.au answers its CSIRO copies with no origin; the DAP answers its recorded pages."""

    def __init__(self, gov: list[dict[str, object]]) -> None:
        super().__init__()
        self.gov = FakeSession(gov)

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        if url.startswith(DAP.api):
            return super().get(url, params, timeout, headers)
        return self.gov.get(url, params, timeout, headers)


def test_a_vote_on_a_dap_copy_follows_it_to_the_dap_record(monkeypatch: pytest.MonkeyPatch) -> None:
    copy = pkg(
        3,
        name="fedora-pid_csiro-72235",
        title="Renamed since",
        organization="csiro-data-access-portal",
    )
    monkeypatch.setattr(requests, "Session", lambda: GovAndDap([copy, pkg(4)]))
    recs, stats = catalogue.harvest((catalogue.BY_CODE["gov"], DAP), log=lambda *_: None)
    by_id = {r["id"]: r for r in recs}
    copy_id = catalogue.record_id("gov", str(copy["id"]))
    assert copy_id not in by_id
    assert by_id["csiro-72235"]["copies"] == [copy_id]
    assert stats["gov"]["dropped_duplicates"] == 1
    written: dict[str, str] = {}
    render(
        plan([], recs, [], "2026-10-15"),
        lambda *a, **k: None,
        written.__setitem__,
        {},
        lambda c: {},
    )
    # The vote endpoint reads these shards to count a vote under the copy's id for the record.
    shards = {k: json.loads(v) for k, v in written.items() if k.startswith("catalogue/copies/")}
    assert {c: r for m in shards.values() for c, r in m.items()} == {copy_id: "csiro-72235"}
    votable = {
        k: v
        for s in written
        for k, v in (json.loads(written[s]).items() if s.startswith("catalogue/votable/") else [])
    }
    assert votable["csiro-72235"][4] == [copy_id]


def test_a_dap_copy_naming_its_origin_is_marked_as_a_copy_by_host() -> None:
    assert "data.csiro.au" in catalogue.DIRECT_HOSTS
    named = pkg(1, original_harvest_source={"site_url": "https://data.csiro.au"})
    # data.gov.au's search answers its DAP copies with no origin, as it did on 14 Oct 2026.
    unnamed = pkg(2, organization="csiro-data-access-portal")
    recs, dropped = catalogue.ckan(
        catalogue.BY_CODE["gov"],
        cast("requests.Session", FakeSession([named, unnamed])),
        log=lambda *_: None,
    )
    assert dropped == 1
    # A copy that names its origin is marked for harvest to drop; one that does not is not.
    assert [("copy_of" in r, r["org"]) for r in recs] == [
        (True, "tmr"),
        (False, "csiro-data-access-portal"),
    ]


def test_csiro_is_placed_as_a_federal_publisher() -> None:
    curated = load_curated(Path(__file__).parents[2] / "register" / "publishers")
    _pubs, by_org = resolve(
        [{"portal": "csiro", "org": "csiro", "org_title": "CSIRO"}],
        curated,
        {p.code: p.jurisdiction for p in catalogue.PORTALS},
    )
    pub = by_org["csiro:csiro"]
    assert (pub.jurisdiction, pub.level, pub.name) == ("Cth", "federal", "CSIRO")
