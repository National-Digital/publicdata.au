import json
from typing import TYPE_CHECKING, cast

import requests

from publicdata import __main__ as cli
from publicdata import catalogue, store
from publicdata.directory import plan, render
from publicdata.register import load

from .test_catalogue import FakeSession, Resp, pkg
from .test_contribute import FakeGitHub, FakeSite, Response, row, run

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import pytest

    from publicdata.catalogue import Params, Record
    from publicdata.contribute import Row
    from publicdata.store import PortalStats

GOV, NSW = catalogue.BY_CODE["gov"], catalogue.BY_CODE["nsw"]


def uuid(i: int) -> str:
    return f"0000000{i}-aaaa-bbbb-cccc-dddddddddddd"


def harvested(i: int, href: str) -> dict[str, object]:
    """A data.gov.au copy of an NSW record, harvested as data.gov.au states it."""
    origin = {"href": href, "site_url": "https://data.nsw.gov.au/data/", "title": "DataNSW"}
    return pkg(i, original_harvest_source=json.dumps(origin))


class Portals(FakeSession):
    """Each CKAN portal answers its own packages; a portal named in `down` cannot be read."""

    def __init__(self, by_api: Mapping[str, list[dict[str, object]]], down: str = "") -> None:
        super().__init__([])
        self.by_api, self.down = by_api, down

    def get(
        self,
        url: str,
        params: Params | None = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Resp:
        if self.down and url.startswith(self.down):
            msg = "down"
            raise requests.ConnectionError(msg)
        self.packages = next(v for k, v in self.by_api.items() if url.startswith(k))
        return super().get(url, params, timeout, headers)


def nsw_and_gov(gov: list[dict[str, object]], down: str = "") -> Portals:
    return Portals({NSW.api: [pkg(1), pkg(2)], GOV.api: gov}, down)


def test_a_dropped_copy_names_the_record_that_replaced_it(monkeypatch: pytest.MonkeyPatch) -> None:
    gov = [
        # CKAN's harvester keeps the original's package id and names its page.
        harvested(1, "https://data.nsw.gov.au/data/dataset/crash-data-1"),
        # Matched on the page alone.
        {**harvested(6, "https://data.nsw.gov.au/data/dataset/crash-data-2/"), "id": "copy-6"},
        # Matched on the package id alone.
        harvested(2, "https://data.nsw.gov.au/data/dataset/renamed"),
        # Nothing on the NSW portal matches, so it is dropped and maps to nothing.
        {**harvested(8, "https://data.nsw.gov.au/data/dataset/withdrawn"), "id": "copy-8"},
        pkg(5),
    ]
    monkeypatch.setattr(requests, "Session", lambda: nsw_and_gov(gov))
    recs, stats = catalogue.harvest((GOV, NSW), log=lambda *_: None)
    by_id = {r["id"]: r for r in recs}
    assert sorted(by_id) == [f"gov-{uuid(5)}", f"nsw-{uuid(1)}", f"nsw-{uuid(2)}"]
    assert by_id[f"nsw-{uuid(1)}"]["copies"] == [f"gov-{uuid(1)}"]
    assert by_id[f"nsw-{uuid(2)}"]["copies"] == [f"gov-{uuid(2)}", "gov-copy-6"]
    assert all("copy_of" not in r for r in recs)
    assert "copies" not in by_id[f"gov-{uuid(5)}"]
    assert stats["gov"] == {"records": 1, "dropped_duplicates": 4}
    assert not any("gov-copy-8" in r.get("copies", []) for r in recs)


def test_a_council_copy_maps_to_the_one_record_with_its_title() -> None:
    def one(portal: str, sid: str, title: str, org: str) -> Record:
        return catalogue._record(catalogue.BY_CODE[portal], source_id=sid, name=sid, title=title,
                                 org=org, org_title="", kind="dataset", licence="CC-BY-4.0",
                                 licence_title="", formats=["CSV"], created="", modified="",
                                 url="", summary="", harvested_from="")  # fmt: skip

    own = one("ballarat", "da_1", "Public Toilets", "ballarat")
    twin_a = one("ballarat", "da_2", "Trees", "ballarat")
    twin_b = one("ballarat", "da_3", "Trees", "ballarat")
    toilets = one("vic", "c1", "Public toilets", "city-of-ballarat")
    trees = one("vic", "c2", "Trees", "city-of-ballarat")
    moved: dict[str, str] = {}
    stats: dict[str, PortalStats] = {}
    kept = catalogue._drop_copies(
        [own, twin_a, twin_b, toilets, trees], catalogue.PORTALS, stats, moved
    )
    assert [r["id"] for r in kept] == [own["id"], twin_a["id"], twin_b["id"]]
    # Two council records share the title, so the second copy is dropped without a mapping.
    assert moved == {toilets["id"]: own["id"]}


def test_a_record_keeps_its_copies_while_their_portal_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    portals = (GOV, NSW)
    copy = harvested(1, "https://data.nsw.gov.au/data/dataset/crash-data-1")
    monkeypatch.setattr(requests, "Session", lambda: nsw_and_gov([copy, pkg(5)]))
    first = catalogue.fetch(tmp_path, log=lambda *_: None, portals=portals, today="2026-10-05")
    assert first is not None
    monkeypatch.setattr(requests, "Session", lambda: nsw_and_gov([], down=GOV.api))
    recs, stats = catalogue.harvest(
        portals,
        log=lambda *_: None,
        previous=catalogue.load(tmp_path),
        previous_version="2026-10-05",
    )
    assert stats["gov"]["carried_from"] == "2026-10-05"
    by_id = {r["id"]: r for r in recs}
    assert by_id[f"nsw-{uuid(1)}"]["copies"] == [f"gov-{uuid(1)}"]
    # So the snapshot is unchanged and makes no version.
    assert (
        catalogue.fetch(tmp_path, log=lambda *_: None, portals=portals, today="2026-10-12") is None
    )


def test_a_harvest_without_the_previous_snapshot_stops_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every portal can be read, but the copies carried in the missing snapshot would be lost.
    asked: list[str] = []

    class Counting(Portals):
        def get(
            self,
            url: str,
            params: Params | None = None,
            timeout: float | None = None,
            headers: dict[str, str] | None = None,
        ) -> Resp:
            asked.append(url)
            return super().get(url, params, timeout, headers)

    monkeypatch.setattr(requests, "Session", lambda: Counting({NSW.api: [pkg(1)], GOV.api: []}))
    with __import__("pytest").raises(catalogue.PortalError, match="store pull --only catalogue"):
        catalogue.harvest((GOV, NSW), log=lambda *_: None, previous_version="2026-10-05")
    assert asked == []


def test_copies_pass_along_a_chain_and_a_listed_record_is_never_a_copy() -> None:
    def r(rid: str, copies: list[str] | None = None) -> Record:
        out = {"id": rid, "portal": rid.split("-", maxsplit=1)[0], "url": ""}
        return cast("Record", {**out, "copies": copies} if copies else out)

    previous = [r("nsw-b", ["gov-a"]), r("nsw-c", ["gov-back"])]
    # nsw-b is now a copy of qld-c; gov-back is listed again.
    out = catalogue._carry(
        [r("qld-c"), r("gov-back"), r("nsw-c")], [], {"nsw-b": "qld-c"}, previous
    )
    by_id = {x["id"]: x for x in out}
    assert by_id["qld-c"]["copies"] == ["gov-a", "nsw-b"]
    assert "copies" not in by_id["nsw-c"]


def test_a_data_gov_au_copy_of_a_record_a_council_replaced_follows_it_to_the_council() -> None:
    u = "0d0b3f0e-1111-4222-8333-944455556666"
    melb = cast("Record", {"id": "melb-trees", "portal": "melb", "url": "https://melb/trees"})
    gov = cast(
        "Record",
        {
            "id": f"gov-{u}",
            "portal": "gov",
            "url": "",
            "copy_of": f"https://discover.data.vic.gov.au/dataset/{u}",
        },
    )
    out = catalogue._carry([melb], [gov], {f"vic-{u}": "melb-trees"}, [])
    assert out[0]["copies"] == [f"gov-{u}", f"vic-{u}"]


def test_a_snapshot_written_before_copies_were_kept_still_loads_and_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = catalogue._record(NSW, source_id=uuid(1), name="crash-data-1", title="Crash data 1",
                            org="tmr", org_title="TMR", kind="dataset", licence="CC-BY-4.0",
                            licence_title="", formats=["CSV"], created="", modified="",
                            url=NSW.landing("crash-data-1"), summary="", harvested_from="")  # fmt: skip
    data = catalogue.encode([old])
    assert b"copies" not in data
    m = store.Manifest(
        dataset=catalogue.SLUG,
        version="2026-09-28",
        as_at="2026-09-28",
        fetched_at="2026-09-28T00:00:00Z",
        sha256=__import__("hashlib").sha256(data).hexdigest(),
        bytes=len(data),
        filename=catalogue.FILENAME,
        encoding="utf-8",
        source={"portals": {}, "stats": {}},
        licence={"id": "various"},
    )
    store.write(tmp_path, m, data)
    recs = catalogue.load(tmp_path)
    assert recs[0].get("copies", []) == []
    written: dict[str, str] = {}
    render(plan([], recs, [], "2026-09-28"), lambda *a, **k: None, written.__setitem__, {},
           lambda c: {})  # fmt: skip
    assert not any(k.startswith("catalogue/copies/") for k in written)
    assert json.loads(written["catalogue/aliases.json"]) == {}
    # The next harvest reads it as the previous snapshot and adds the mapping.
    copy = harvested(1, "https://data.nsw.gov.au/data/dataset/crash-data-1")
    monkeypatch.setattr(requests, "Session", lambda: nsw_and_gov([copy]))
    assert catalogue.fetch(tmp_path, log=lambda *_: None, portals=(GOV, NSW), today="2026-10-05")
    assert {r["id"]: r.get("copies") for r in catalogue.load(tmp_path)}[f"nsw-{uuid(1)}"] == [
        f"gov-{uuid(1)}"
    ]


def test_the_site_publishes_each_copy_against_its_record_and_a_claimed_record_s_slug() -> None:
    reg = {d.slug: d for d in load(cli.REGISTER)}
    base = {"kind": "dataset", "licence": "CC-BY-4.0", "open": True, "downloadable": True,
            "formats": ["CSV"], "modified": "", "summary": "", "org": "abr",
            "org_title": "Australian Business Register", "portal": "gov"}  # fmt: skip
    records = cast(
        "list[Record]",
        [
            {**base, "id": "gov-1", "name": "abn-bulk-extract", "title": "ABN Bulk Extract",
             "url": "https://data.gov.au/data/dataset/abn-bulk-extract", "copies": ["vic-9"]},
            {**base, "id": "gov-2", "name": "other", "title": "Other",
             "url": "https://data.gov.au/data/dataset/other", "copies": ["nsw-7", "nsw-8"]},
            {**base, "id": "gov-3", "name": "shut", "title": "Shut", "open": False,
             "url": "https://data.gov.au/data/dataset/shut", "copies": ["qld-5"]},
        ],
    )  # fmt: skip
    d = plan([reg["abn-bulk-extract"]], records, [], "2026-10-12")
    written: dict[str, str] = {}
    render(d, lambda *a, **k: None, written.__setitem__, {}, lambda c: {})
    assert json.loads(written["catalogue/copies/nsw-7.json"]) == {"nsw-7": "gov-2"}
    assert json.loads(written["catalogue/copies/nsw-8.json"]) == {"nsw-8": "gov-2"}
    assert json.loads(written["catalogue/copies/vic-9.json"]) == {"vic-9": "gov-1"}
    assert json.loads(written["catalogue/votable/gov-2.json"])["gov-2"][4] == ["nsw-7", "nsw-8"]
    # A record that takes no votes lists its copies apart, and a claimed one through aliases.
    copied = {k: json.loads(v) for k, v in written.items() if k.startswith("catalogue/copied/")}
    assert copied == {"catalogue/copied/gov-3.json": {"gov-3": ["qld-5"]}}
    assert json.loads(written["catalogue/aliases.json"]) == {
        "gov-1": "abn-bulk-extract",
        "vic-9": "abn-bulk-extract",
    }


class MovedSite(FakeSite):
    """The catalogue API as it answers a dropped copy's id: with the record that replaced it."""

    def __init__(self, votes: Mapping[str, int], rows: list[Row], moved: dict[str, str]) -> None:
        super().__init__(votes, rows)
        self.moved = moved

    def get(
        self, url: str, params: Mapping[str, str] | None = None, timeout: float | None = None
    ) -> Response:
        got = super().get(url, params, timeout)
        if url.endswith("/api/v1/catalogue") and params:
            ids = params["ids"].split(",")
            moved = {k: v for k, v in self.moved.items() if k in ids}
            body = cast("dict[str, list[Row]]", got.body)
            rows = body["rows"] + [r for r in self.rows if r["id"] in moved.values()]
            return Response({"rows": rows, "moved": moved})
        return got


def test_an_open_issue_on_a_dropped_copy_moves_to_the_record_that_replaced_it() -> None:
    gh = FakeGitHub()
    run([], FakeSite({"gov-a": 3}, [row("gov-a")]), gh)
    assert gh.open() == ["gov-a"]
    site = MovedSite({"nsw-a": 3}, [row("nsw-a")], {"gov-a": "nsw-a"})
    run([], site, gh)
    assert gh.open() == ["nsw-a"]
    assert list(gh.store) == [1]
    assert gh.store[1]["body"].startswith("<!-- publicdata-contribute: nsw-a -->")
    assert gh.store[1]["title"] == "Add Title of nsw-a"
    # Without the mapping the same run closes it as gone.
    gh = FakeGitHub()
    run([], FakeSite({"gov-a": 3}, [row("gov-a")]), gh)
    run([], FakeSite({"nsw-a": 3}, [row("nsw-a")]), gh)
    assert gh.store[1]["state"] == "closed"


def test_a_copy_without_a_portal_page_matches_on_its_harvest_source_s_package_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vic = catalogue.BY_CODE["vic"]

    def origin(i: int, site: str, href: str | None) -> dict[str, object]:
        src = {"site_url": site, "title": "Victoria Government"} | ({"href": href} if href else {})
        return pkg(i, original_harvest_source=json.dumps(src))

    gov = [
        # No href at all.
        origin(1, "https://discover.data.vic.gov.au", None),
        # A host that is not the portal's own, though the harvest source is.
        origin(2, "https://discover.data.vic.gov.au", "https://data.vic.gov.au/data/dataset/x"),
    ]
    monkeypatch.setattr(
        requests, "Session", lambda: Portals({vic.api: [pkg(1), pkg(2)], GOV.api: gov})
    )
    recs, _stats = catalogue.harvest((GOV, vic), log=lambda *_: None)
    by_id = {r["id"]: r for r in recs}
    assert by_id[f"vic-{uuid(1)}"]["copies"] == [f"gov-{uuid(1)}"]
    assert by_id[f"vic-{uuid(2)}"]["copies"] == [f"gov-{uuid(2)}"]
    assert by_id[f"vic-{uuid(1)}"]["copy_pages"] == {
        f"gov-{uuid(1)}": "https://data.gov.au/data/dataset/crash-data-1"
    }


def test_an_entry_served_from_or_planned_on_a_dropped_copy_keeps_its_record() -> None:
    reg = {d.slug: d for d in load(cli.REGISTER)}
    live = next(
        d for d in reg.values() if d.status == "live" and d.source.package
        and "data.gov.au" in (d.source.portal or "")
    )  # fmt: skip
    planned = reg["abn-bulk-extract"]
    base = {"kind": "dataset", "licence": "CC-BY-4.0", "open": True, "downloadable": True,
            "formats": ["CSV"], "modified": "", "summary": "", "org": "abr",
            "org_title": "Australian Business Register", "portal": "nsw"}  # fmt: skip
    gov_page = catalogue.BY_CODE["gov"].landing
    records = cast(
        "list[Record]",
        [
            {**base, "id": "nsw-1", "name": "served-original", "title": "Served",
             "url": "https://data.nsw.gov.au/data/dataset/served-original",
             "copies": ["gov-9"], "copy_pages": {"gov-9": gov_page(live.source.package or "")}},
            {**base, "id": "nsw-2", "name": "planned-original", "title": "Planned",
             "url": "https://data.nsw.gov.au/data/dataset/planned-original",
             "copies": ["gov-8"], "copy_pages": {"gov-8": planned.source.url}},
        ],
    )  # fmt: skip
    d = plan([live, planned], records, [], "2026-10-12")
    assert "nsw-1" in d.served
    assert d.chosen == {"nsw-2": "abn-bulk-extract"}
    written: dict[str, str] = {}
    render(d, lambda *a, **k: None, written.__setitem__, {}, lambda c: {})
    votable = {
        k
        for rel, t in written.items()
        if rel.startswith("catalogue/votable/")
        for k in json.loads(t)
    }
    assert votable.isdisjoint({"nsw-1", "nsw-2"})
    assert json.loads(written["catalogue/aliases.json"])["gov-8"] == "abn-bulk-extract"


def test_a_catalogue_answer_other_than_200_stops_the_sync_before_it_closes_anything() -> None:
    class Down(Response):
        def raise_for_status(self) -> None:
            msg = "503 The catalogue cannot be read at the moment"
            raise requests.HTTPError(msg)

    class DownSite(FakeSite):
        def get(
            self, url: str, params: Mapping[str, str] | None = None, timeout: float | None = None
        ) -> Response:
            return Down({}) if url.endswith("/api/v1/catalogue") else super().get(url, params)

    gh = FakeGitHub()
    run([], FakeSite({"gov-a": 3}, [row("gov-a")]), gh)
    gh.writes.clear()
    with __import__("pytest").raises(requests.HTTPError):
        run([], DownSite({"nsw-a": 3}, []), gh)
    assert gh.writes == []
    assert gh.open() == ["gov-a"]
