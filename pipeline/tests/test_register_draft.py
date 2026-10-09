import io
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self, cast

import openpyxl
import pytest
import requests
import yaml

from publicdata import __main__ as cli
from publicdata import register_draft as rd
from publicdata.normalise import normalise
from publicdata.publishers import Publisher
from publicdata.register import RegisterError, parse

from .conftest import make_manifest

if TYPE_CHECKING:
    from collections.abc import Iterator

FIX = Path(__file__).parent / "fixtures" / "draft"
URL = "https://www.data.qld.gov.au/dataset/school-enrolments-by-region"


class Resp:
    def __init__(self, body: bytes) -> None:
        self.content, self.status_code = body, 200
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return json.loads(self.content)

    def iter_content(self, n: int) -> Iterator[bytes]:
        for i in range(0, len(self.content), n):
            yield self.content[i : i + n]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> Literal[False]:
        return False


class Portal:
    """Answers package_show and the CSV download from recorded files."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.asked: list[tuple[str, object]] = []

    def get(self, url: str, params: object = None, **kw: object) -> Resp:
        self.asked.append((url, params))
        if url.endswith("/package_show"):
            assert params == {"id": "school-enrolments-by-region"}
            return Resp((FIX / "package_show.json").read_bytes())
        if url.endswith("enrolments.csv"):
            return Resp((FIX / "enrolments.csv").read_bytes())
        raise AssertionError(url)


def portal() -> requests.Session:
    # The stand-in answers the calls a draft makes of a session.
    return cast("requests.Session", Portal())


def test_a_draft_types_each_field_as_the_normaliser_will_and_builds() -> None:
    slug, entry, notes = rd.draft(URL, [], portal())
    assert slug == "qld-school-enrolments-by-region"
    types = {f["name"]: (f["type"], f.get("date_format")) for f in entry["fields"]}
    assert types == {
        "year": ("integer", None),
        "region": ("string", None),
        "postcode": ("string", None),
        "census_date": ("date", "%d/%m/%Y"),
        "enrolments": ("integer", None),
        "share": ("number", None),
        "selective": ("boolean", None),
        "opened": ("datetime", "%Y-%m-%d %H:%M:%S"),
    }
    assert entry["suppression"] == ["<5", "np"]
    assert entry["status"] == "building"
    assert entry["licence"]["id"] == "CC-BY-4.0"
    assert entry["publisher"] == {
        "name": "Department of Education",
        "short": "Department of Education",
        "jurisdiction": "Qld",
    }
    assert entry["source"]["portal"] == "https://www.data.qld.gov.au"
    assert entry["source"]["resource"] == "aaaaaaaa-0000-4000-8000-000000000001"
    assert entry["summary"].startswith("Full-time enrolments at state schools by region")
    assert any("other tabular resources" in n and "workbook" in n for n in notes)
    text = rd.to_yaml(entry)
    assert "# TODO: write search title" in text
    ds = parse(yaml.safe_load(text), "draft")
    data = (FIX / "enrolments.csv").read_bytes()
    t = normalise(ds, make_manifest(data, dataset=ds.slug), data)
    assert t.rows == 4
    assert t.suppressed_cells == 2


def test_a_draft_cannot_go_live_until_its_search_copy_is_written() -> None:
    _, entry, _ = rd.draft(URL, [], portal())
    live = yaml.safe_load(rd.to_yaml(entry)) | {"status": "live"}
    # A person reads the licence before anything else: the draft leaves the date blank.
    with pytest.raises(RegisterError, match=re.escape("licence.reviewed")):
        parse(live, "draft")
    live["licence"]["reviewed"] = "2026-10-01"
    with pytest.raises(RegisterError, match="search_title"):
        parse(live, "draft")


def test_a_curated_publisher_and_a_named_resource_win() -> None:
    cur = Publisher(
        slug="education",
        name="Department of Education",
        short="Education",
        jurisdiction="Qld",
        level="state",
        url="https://education.qld.gov.au/",
        orgs=["qld:department-of-education"],
        curated=True,
    )
    with pytest.raises(rd.DraftError, match="not in"):
        rd.draft(URL, [cur], portal(), resource="bbbbbbbb-0000-4000-8000-000000000000")
    _, entry, notes = rd.draft(
        URL + "/resource/aaaaaaaa-0000-4000-8000-000000000001", [cur], portal()
    )
    assert entry["publisher"]["short"] == "Education"
    assert entry["publisher"]["url"] == "https://education.qld.gov.au/"
    assert not any("publisher.url" in n for n in notes)


def test_urls_that_do_not_name_a_ckan_dataset_are_refused() -> None:
    with pytest.raises(rd.DraftError, match="CKAN"):
        rd.portal_for("https://www.data.act.gov.au/dataset/abcd-1234")
    with pytest.raises(rd.DraftError, match="does not name"):
        rd.portal_for("https://www.data.qld.gov.au/")


def test_field_names_are_snake_case_and_unique() -> None:
    taken: set[str] = set()
    assert [rd.field_name(h, taken) for h in ["Crash Year", "crash-year", "2021 Count", ""]] == [
        "crash_year",
        "crash_year_2",
        "f_2021_count",
        "field",
    ]


def test_the_draft_command_writes_the_entry_and_refuses_to_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "REGISTER", tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path.parent)
    monkeypatch.setattr(requests, "Session", Portal)
    assert cli.main(["register", "draft", URL]) == 0
    written = tmp_path / "qld-school-enrolments-by-region.yaml"
    assert parse(yaml.safe_load(written.read_text()), "x").status == "building"
    assert "note: check the attribution" in capsys.readouterr().err
    assert cli.main(["register", "draft", URL]) == 1
    assert "already exists" in capsys.readouterr().err


def test_a_named_resource_that_is_not_a_table_is_refused() -> None:
    with pytest.raises(rd.DraftError, match="PDF, not CSV or Excel"):
        rd.draft(URL + "/resource/aaaaaaaa-0000-4000-8000-000000000002", [], portal())


def test_a_workbook_without_the_named_sheet_is_a_draft_error() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "data"
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(rd.DraftError, match="could not be read as XLSX"):
        rd.sample_table(buf.getvalue(), "xlsx", cut=False, sheet="missing")
