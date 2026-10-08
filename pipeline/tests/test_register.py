import copy
import re
import shutil
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from publicdata import __main__ as cli
from publicdata.register import (
    LICENCE_CONDITIONS,
    OPEN_LICENCES,
    RegisterError,
    draft_label,
    load,
    parse,
)

from .conftest import ROOT, present

if TYPE_CHECKING:
    from pathlib import Path


def test_real_register_loads(register_dir: Path) -> None:
    ds = load(register_dir)
    assert {d.slug for d in ds} >= {"qld-road-crash-locations", "qld-road-casualties"}
    live = [d for d in ds if d.status == "live"]
    assert live
    assert all((d.fields or d.tables) and d.licence.open for d in live)


def _raw(**over: object) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "slug": "x-y",
        "title": "X",
        "status": "backlog",
        "publisher": {"name": "Agency", "jurisdiction": "Cth"},
        "licence": {"id": "CC-BY-4.0"},
        "source": {"url": "https://example.gov.au/"},
    }
    raw.update(over)
    return raw


def test_nd_licence_must_be_blocked() -> None:
    with pytest.raises(RegisterError, match="blocked"):
        parse(_raw(licence={"id": "CC-BY-ND-4.0"}), "x")
    ds = parse(_raw(status="blocked", licence={"id": "CC-BY-ND-4.0"}, blocked_reason="ND"), "x")
    assert not ds.publishable


def test_live_needs_open_licence_evidence_and_fields() -> None:
    with pytest.raises(RegisterError, match="open licence"):
        parse(_raw(status="live", licence={"id": "not-specified"}), "x")
    with pytest.raises(RegisterError, match="evidence"):
        parse(_raw(status="live", licence={"id": "CC-BY-4.0", "attribution": "a"}), "x")
    with pytest.raises(RegisterError, match="allow-list"):
        parse(
            _raw(
                status="live",
                licence={
                    "id": "CC-BY-4.0",
                    "attribution": "a",
                    "evidence": "e",
                    "reviewed": "2026-10-01",
                },
                source={"url": "u", "adapter": "ckan-resource"},
            ),
            "x",
        )


def test_key_must_be_declared_field() -> None:
    with pytest.raises(RegisterError, match="not a declared field"):
        parse(_raw(fields=[{"name": "a", "source": "A"}], key=["b"]), "x")


def test_live_needs_the_search_fields(tmp_path: Path) -> None:
    live = _raw(
        status="live",
        licence={
            "id": "CC-BY-4.0",
            "evidence": "https://e",
            "attribution": "a",
            "reviewed": "2026-10-01",
        },
        source={
            "url": "https://example.gov.au/",
            "adapter": "ckan-resource",
            "package": "p",
            "resource": "r",
        },
        fields=[{"name": "a", "source": "A"}],
    )
    with pytest.raises(RegisterError, match="search_title"):
        parse(live, "x")
    live["search_title"] = "X data"
    with pytest.raises(RegisterError, match="also_known_as with at least 2"):
        parse(live, "x")
    live["also_known_as"] = ["X stats", "X figures"]
    with pytest.raises(RegisterError, match="keywords with at least 3"):
        parse(live, "x")
    live["keywords"] = ["x", "y", "z"]
    with pytest.raises(RegisterError, match="faq"):
        parse(live, "x")
    live["faq"] = [{"q": "Why?", "a": "Because."}]
    with pytest.raises(RegisterError, match="at least one topic"):
        parse(live, "x")
    live["topics"] = ["weather"]
    with pytest.raises(RegisterError, match="unknown topic"):
        parse(live, "x")
    live["topics"] = ["roads"]
    assert parse(live, "x").search_title == "X data"
    assert parse(live, "x").topics == ("roads",)
    # A live collection needs a description on one member.
    live.update(collection="c", collection_title="C")
    (tmp_path / "x-y.yaml").write_text(yaml.safe_dump(live), encoding="utf-8")
    with pytest.raises(RegisterError, match="collection_description"):
        load(tmp_path)
    live["collection_description"] = "About C."
    (tmp_path / "x-y.yaml").write_text(yaml.safe_dump(live), encoding="utf-8")
    assert load(tmp_path)[0].collection_description == "About C."


def test_label_drafts_reuse_the_register_then_read_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = [
        "crash_year",
        "crash_severity",
        "crash_police_region",
        "involving_truck",
        "count_crashes",
    ]
    assert draft_label("crash_police_region", names) == "Police region"
    assert draft_label("involving_truck", names) == "Truck involved"
    assert draft_label("count_crashes", names) == "Crashes"

    reg = tmp_path / "register"
    shutil.copytree(cli.REGISTER, reg)
    path = reg / "qld-road-crash-factors.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    for f in raw["fields"]:
        f.pop("label", None)
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(cli, "REGISTER", reg)
    cli.main(["register", "labels", "qld-road-crash-factors", "--write"])
    ds = {d.slug: d for d in load(reg)}["qld-road-crash-factors"]
    # A field another entry already labels takes that label; one only this entry has is drafted.
    assert ds.field("crash_police_region").label == "Police region"
    assert ds.field("count_crashes").label == "Crashes"
    assert all(f.label for f in ds.fields)


def test_entries_load_from_folders_but_not_publishers(tmp_path: Path) -> None:
    (tmp_path / "qld").mkdir()
    (tmp_path / "publishers").mkdir()
    (tmp_path / "qld" / "x-y.yaml").write_text(yaml.safe_dump(_raw()))
    (tmp_path / "publishers" / "qld.yaml").write_text("- not: an entry\n")
    (tmp_path / "a-b.yaml").write_text(yaml.safe_dump(_raw(slug="a-b")))
    got = load(tmp_path)
    assert {d.slug for d in got} == {"x-y", "a-b"}
    assert next(d for d in got if d.slug == "x-y").path == str(tmp_path / "qld" / "x-y.yaml")
    (tmp_path / "x-y.yaml").write_text(yaml.safe_dump(_raw()))
    with pytest.raises(RegisterError, match=re.escape("duplicate slug, also in qld/x-y.yaml")):
        load(tmp_path)


def test_query_takes_true_or_false() -> None:
    assert parse(_raw(), "x").query is True
    assert parse(_raw(query=False), "x").query is False
    with pytest.raises(RegisterError, match="true or false"):
        parse(_raw(query="no"), "x")


def test_a_ckan_source_names_its_package_and_its_resource_or_a_pattern() -> None:
    base = yaml.safe_load((ROOT / "register" / "nsw-recorded-crime-by-lga.yaml").read_text())
    parse(base, "ok")
    raw = copy.deepcopy(base)
    del raw["source"]["resource"]
    with pytest.raises(RegisterError, match="resource or resource_match"):
        parse(raw, "x")
    raw["source"]["resource_match"] = "anything"
    parse(raw, "x")
    # A pattern alone would search the whole portal, so the package stays as the search text.
    raw["source"]["package_match"] = "anything"
    del raw["source"]["package"]
    with pytest.raises(RegisterError, match="search text for package_match"):
        parse(raw, "x")
    raw = copy.deepcopy(base)
    raw["source"]["resource_match"] = "("
    with pytest.raises(RegisterError, match="not a valid pattern"):
        parse(raw, "x")


def test_an_ala_source_needs_a_search_and_providers() -> None:
    with pytest.raises(RegisterError, match="search and providers"):
        parse(_raw(source={"adapter": "ala", "url": "https://api.example.org/"}), "x")
    ds = parse(
        _raw(
            source={
                "adapter": "ala",
                "url": "https://api.example.org/",
                "search": "genus:Eucalyptus",
                "providers": ["dr1"],
            }
        ),
        "x",
    )
    assert ds.source.search == "genus:Eucalyptus"
    assert ds.source.providers == ("dr1",)


def test_a_delimiter_is_tab_or_one_character() -> None:
    parse(
        _raw(
            source={
                "adapter": "ckan-resource",
                "url": "u",
                "package": "p",
                "resource": "r",
                "delimiter": "tab",
            }
        ),
        "x",
    )
    parse(
        _raw(
            source={
                "adapter": "ckan-resource",
                "url": "u",
                "package": "p",
                "resource": "r",
                "delimiter": ";",
            }
        ),
        "x",
    )
    with pytest.raises(RegisterError, match="delimiter"):
        parse(
            _raw(
                source={
                    "adapter": "ckan-resource",
                    "url": "u",
                    "package": "p",
                    "resource": "r",
                    "delimiter": "; ",
                }
            ),
            "x",
        )


def test_a_live_file_source_quotes_the_licence_pages_words() -> None:
    live: dict[str, Any] = {
        "status": "live",
        "licence": {
            "id": "CC-BY-4.0",
            "evidence": "https://e",
            "attribution": "a",
            "reviewed": "2026-10-01",
        },
        "source": {"adapter": "file", "url": "https://example.gov.au/f.csv"},
        "fields": [{"name": "a", "source": "a"}],
        "topics": ["roads"],
        "search_title": "t",
        "also_known_as": ["a", "b"],
        "keywords": ["a", "b", "c"],
        "faq": [{"q": "q", "a": "a"}],
    }
    with pytest.raises(RegisterError, match=re.escape("licence.statement")):
        parse(_raw(**live), "x")
    live["licence"] = {**live["licence"], "statement": "licensed  under\n CC BY 4.0"}
    assert parse(_raw(**live), "x").licence.statement == "licensed under CC BY 4.0"


def test_two_entries_cannot_target_one_search_phrase(tmp_path: Path) -> None:
    src = ROOT / "register"
    for name in ("qld-road-crash-locations.yaml", "qld-road-casualties.yaml"):
        shutil.copy(src / name, tmp_path / name)
    text = (tmp_path / "qld-road-casualties.yaml").read_text(encoding="utf-8")
    text = re.sub(
        r"^search_title: .*$",
        "search_title: Queensland road crash locations",
        text,
        flags=re.MULTILINE,
    )
    (tmp_path / "qld-road-casualties.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(
        RegisterError, match="search title 'Queensland road crash locations' is also used by"
    ):
        load(tmp_path)


def test_place_field_must_be_a_partition_field(tmp_path: Path) -> None:
    text = (ROOT / "register" / "qld-road-crash-locations.yaml").read_text(encoding="utf-8")
    text = text.replace("place_field: loc_local_government_area", "place_field: crash_severity")
    (tmp_path / "qld-road-crash-locations.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(
        RegisterError, match="place_field 'crash_severity' is not a partition_by field"
    ):
        load(tmp_path)


def test_omit_needs_a_reason_and_cannot_name_a_read_column() -> None:
    ok = parse(_raw(omit={"Vendor": "third party"}), "x")
    assert ok.omit == {"Vendor": "third party"}
    with pytest.raises(RegisterError, match="reason"):
        parse(_raw(omit={"Vendor": ""}), "x")
    with pytest.raises(RegisterError, match="which a field reads"):
        parse(_raw(fields=[{"name": "a", "source": "Vendor"}], omit={"Vendor": "x"}), "x")


def _database(**over: object) -> dict[str, Any]:
    raw = _raw(
        kind="database",
        status="live",
        topics=["housing"],
        search_title="X database",
        also_known_as=["X", "Y"],
        keywords=["a", "b", "c"],
        faq=[{"q": "Q?", "a": "A."}],
        licence={
            "id": "CC-BY-4.0",
            "evidence": "https://example.gov.au/",
            "attribution": "A.",
            "reviewed": "2026-10-01",
        },
        source={
            "adapter": "ckan-resource",
            "url": "https://example.gov.au/",
            "portal": "https://example.gov.au/",
            "package": "x",
            "resource_match": "zip$",
        },
        database={"member_match": r"(?P<table>[A-Z_]+)\.psv$", "delimiter": "|"},
        tables=[
            {
                "name": "thing",
                "source": "THING",
                "key": ["thing_pid"],
                "fields": [
                    {"name": "thing_pid", "source": "THING_PID"},
                    {"name": "kind_code", "source": "KIND_CODE", "references": "kind_aut.code"},
                ],
            },
            {
                "name": "kind_aut",
                "source": "KIND_AUT",
                "key": ["code"],
                "fields": [{"name": "code", "source": "CODE"}, {"name": "name", "source": "NAME"}],
            },
        ],
        views=[{"name": "thing_view", "sql": "SELECT * FROM thing", "example": "kind_code"}],
    )
    raw.update(over)
    return raw


def test_a_database_entry_names_its_tables_keys_references_and_views() -> None:
    ds = parse(_database(), "x")
    assert ds.kind == "database"
    assert ds.fields == ()
    assert ds.field_count == 4
    assert [t.name for t in ds.tables] == ["thing", "kind_aut"]
    assert ds.table("thing").key == ("thing_pid",)
    assert ds.table("thing").field("kind_code").references == "kind_aut.code"
    assert ds.views[0].example == "kind_code"
    assert present(ds.database).delimiter == "|"
    # A database never goes to the query API.
    assert ds.query is False


def test_a_database_entry_is_checked_for_its_pattern_references_and_shape() -> None:
    for over, why in (
        ({"database": {"member_match": "x"}}, "table"),
        ({"database": {"member_match": "("}}, "valid pattern"),
        ({"fields": [{"name": "a", "source": "A"}]}, "no top-level fields"),
        ({"omit": {"X": "a third party's series"}}, "no top-level omit"),
        ({"suppression": ["<5"]}, "does not support suppression"),
        ({"source_withheld": "terms"}, "does not support source_withheld"),
    ):
        with pytest.raises(RegisterError, match=why):
            parse(_database(**over), "x")
    bad = _database()
    bad["tables"][0]["fields"][1]["references"] = "nowhere.code"
    with pytest.raises(RegisterError, match=re.escape("not a table.field")):
        parse(bad, "x")
    bad = _database()
    bad["tables"][0]["key"] = ["missing"]
    with pytest.raises(RegisterError, match="not a declared field"):
        parse(bad, "x")
    bad = _database()
    bad["tables"][1]["name"] = "thing"
    with pytest.raises(RegisterError, match="duplicate table"):
        parse(bad, "x")
    bad = _database()
    bad["tables"] = []
    with pytest.raises(RegisterError, match="field allow-list"):
        parse(bad, "x")


def test_a_licence_with_a_condition_is_open_and_states_it() -> None:
    assert set(LICENCE_CONDITIONS) <= set(OPEN_LICENCES)
    ds = parse(_raw(licence={"id": "OPEN-GNAF-EULA"}), "x")
    assert ds.licence.open
    assert "sending of mail" in ds.licence.condition
    assert parse(_raw(), "x").licence.condition == ""


def _fielded(**over: object) -> dict[str, Any]:
    return _raw(
        fields=[
            {"name": "year", "source": "Year", "type": "integer"},
            {"name": "state", "source": "State", "type": "string"},
            {"name": "unit", "source": "Unit", "type": "string"},
            {"name": "value", "source": "Value", "type": "number"},
            {"name": "drink", "source": "Drink", "type": "boolean"},
        ],
        **over,
    )


def test_an_example_reads_exact_matches_operators_and_newest() -> None:
    ds = parse(
        _fielded(
            example={
                "where": {
                    "unit": "Number",
                    "year": "newest",
                    "value": {"gte": 1, "lt": 9},
                    "drink": True,
                },
                "group": "state",
                "metric": "sum.value",
                "label": "Victims",
            }
        ),
        "x",
    )
    assert ds.example == {
        "filters": (
            {"field": "unit", "op": "eq", "value": "Number"},
            {"field": "year", "op": "eq", "value": "newest"},
            {"field": "value", "op": "gte", "value": "1"},
            {"field": "value", "op": "lt", "value": "9"},
            {"field": "drink", "op": "eq", "value": "true"},
        ),
        "group": ("state",),
        "metric": "sum.value",
        "label": "Victims",
    }
    assert present(parse(_fielded(example={"group": "state"}), "x").example)["metric"] == "count"


@pytest.mark.parametrize(
    ("example", "message"),
    [
        ({"where": {"unit": "Number"}}, "group must name"),
        ({"group": "nope"}, "group must name"),
        ({"group": "state", "where": {"nope": 1}}, "not a declared field"),
        ({"group": "state", "where": {"unit": {"like": "*N*"}}}, "operator 'like'"),
        ({"group": "state", "metric": "sum.state"}, "numeric field"),
        ({"group": "state", "metric": "median.value"}, "metric is count"),
        ({"group": "state", "order": "desc"}, "takes where, group"),
    ],
)
def test_an_example_is_checked_against_the_fields(example: dict[str, object], message: str) -> None:
    with pytest.raises(RegisterError, match=message):
        parse(_fielded(example=example), "x")


def test_a_chart_keeps_its_rows_and_names_its_split_and_measure() -> None:
    ds = parse(
        _fielded(
            chart={
                "where": {"unit": "Number", "state": {"neq": "Australia"}},
                "split": "none",
                "metric": "avg.value",
                "label": "Average value",
            }
        ),
        "x",
    )
    assert ds.chart == {
        "where": (
            {"field": "unit", "op": "=", "value": "Number"},
            {"field": "state", "op": "!=", "value": "Australia"},
        ),
        "split": "",
        "metric": "avg.value",
        "label": "Average value",
        "year": "",
    }
    assert present(parse(_fielded(chart="none"), "x").chart)["off"] is True
    assert present(parse(_fielded(chart={"year": "year"}), "x").chart)["year"] == "year"
    # A text year is a financial year, such as 2018-19.
    assert present(parse(_fielded(chart={"year": "state"}), "x").chart)["year"] == "state"
    with pytest.raises(RegisterError, match="year must name"):
        parse(_fielded(chart={"year": "value"}), "x")
    assert present(parse(_fielded(chart={"split": "state"}), "x").chart)["split"] == "state"
    assert present(parse(_fielded(chart={}), "x").chart)["split"] is None
    with pytest.raises(RegisterError, match="cannot use newest"):
        parse(_fielded(chart={"where": {"year": "newest"}}), "x")
    with pytest.raises(RegisterError, match="split must name"):
        parse(_fielded(chart={"split": "nope"}), "x")
    with pytest.raises(RegisterError, match=re.escape("chart_where is now chart.where")):
        parse(_fielded(chart_where={"field": "unit", "op": "=", "value": "Number"}), "x")


def test_a_sample_reads_its_order_and_spread_and_needs_words_for_a_pick() -> None:
    ds = parse(
        _fielded(
            sample={
                "where": {"year": "newest", "state": {"neq": "Total"}},
                "order": ["value.desc", "state"],
                "spread": "unit",
                "label": "The largest values in the newest year.",
            }
        ),
        "x",
    )
    assert ds.sample == {
        "where": (
            {"field": "year", "op": "=", "value": "newest"},
            {"field": "state", "op": "!=", "value": "Total"},
        ),
        "order": (("value", True), ("state", False)),
        "spread": "unit",
        "label": "The largest values in the newest year.",
    }
    assert present(parse(_fielded(sample={"spread": "none"}), "x").sample)["spread"] == ""
    assert parse(_fielded(), "x").sample is None
    with pytest.raises(RegisterError, match="needs a label"):
        parse(_fielded(sample={"order": ["value"]}), "x")
    with pytest.raises(RegisterError, match=re.escape("order term 'value.up'")):
        parse(_fielded(sample={"order": ["value.up"], "label": "x"}), "x")
    with pytest.raises(RegisterError, match="spread must name"):
        parse(_fielded(sample={"spread": "nope"}), "x")
    with pytest.raises(RegisterError, match="takes where, order"):
        parse(_fielded(sample={"limit": 5}), "x")
