import json
import re
from pathlib import Path

import pytest
import yaml

from publicdata import hubs
from publicdata.cadence import kaggle_frequency
from publicdata.register import load

from .conftest import ROOT

RECORD = {
    "identifier": "qld-road-crash-factors",
    "title": "Factors in road crashes, Queensland, every year from 2001 to the latest release",
    "description": "Crashes by the factors police recorded.",
    "license": "https://creativecommons.org/licenses/by/4.0/",
    "publisher": {
        "name": "Department of Transport and Main Roads",
        "homepage": "https://www.tmr.qld.gov.au/",
    },
    "versionInfo": "2026-09-30",
    "keyword": [
        "road crashes",
        "Queensland road crash factors by year and severity",
        "drink driving",
    ],
    "publicdata:attribution": "Department of Transport and Main Roads, Road crash factors, licensed under CC BY 4.0.",
    "publicdata:cite": "Department of Transport and Main Roads, Road crash factors, licensed under CC BY 4.0. Serialised and versioned by National Digital at publicdata.au, https://publicdata.au/d/qld-road-crash-factors/v/2026-09-30/",
    "distribution": [
        {
            "format": "parquet",
            "downloadURL": "https://publicdata.au/d/qld-road-crash-factors/v/2026-09-30/data.parquet",
        },
        {
            "format": "csv",
            "downloadURL": "https://publicdata.au/d/qld-road-crash-factors/v/2026-09-30/data.csv",
        },
        {
            "format": "json",
            "downloadURL": "https://publicdata.au/d/qld-road-crash-factors/v/2026-09-30/data.json",
        },
    ],
}
VERSIONS = {
    "versions": [{"version": "2026-09-30", "rows": 1234}, {"version": "2025-01-01", "rows": 1000}]
}
SCHEMA = {
    "fields": [
        {"name": "year", "type": "integer", "title": "Year"},
        {
            "name": "factor",
            "type": "string",
            "title": "(cell: Factor)",
            "description": "Drink | drug driving and others.",
        },
        {"name": "region", "type": "string", "title": "(row header 1)"},
    ]
}
MANIFEST = {
    "sha256": "ab" * 32,
    "source": {
        "portal": "https://www.data.qld.gov.au",
        "package": "crash-data-from-queensland-roads",
    },
}


def make(**over):
    return hubs.entry({**RECORD, **over}, VERSIONS, SCHEMA, MANIFEST, queryable=True)


def text_of(e):
    return [
        hubs.hf_card(e, "publicdata-au/x"),
        json.dumps(hubs.kaggle_metadata(e, "publicdataau")),
        json.dumps(hubs.zenodo_metadata(e)),
    ]


def test_every_copy_links_back_to_the_version_and_carries_the_attribution():
    e = make()
    for t in text_of(e):
        assert "https://publicdata.au/d/qld-road-crash-factors/v/2026-09-30/" in t
        assert "licensed under CC BY 4.0" in t
        assert "has not endorsed" in t
    p = hubs.provenance(e)
    assert p["version_url"] == e.version_url
    assert p["manifest"]["sha256"] == "ab" * 32


def test_hub_copy_has_no_em_dashes():
    for t in text_of(make()):
        assert "—" not in t


def test_every_open_licence_the_register_allows_has_a_hub_id():
    from publicdata.register import LICENCE_CONDITIONS, OPEN_LICENCES

    # A licence with a condition of use is open here and never copied: no hub can state it.
    plain = set(OPEN_LICENCES) - set(LICENCE_CONDITIONS)
    assert set(hubs.HUB_IDS) == plain
    assert {lic.id for lic in hubs.LICENCES.values()} == plain
    sa = hubs.LICENCES["https://creativecommons.org/licenses/by-sa/3.0/au/"]
    assert (sa.huggingface, sa.zenodo, sa.kaggle) == ("other", "other-at", "other")


def test_no_licence_is_ever_published_as_a_different_licence():
    # Each hub id is the same licence or the hub's "other", with the licence named in the text.
    same = {
        "CC-BY-4.0": {"cc-by-4.0", "CC-BY-4.0"},
        "CC0-1.0": {"cc0-1.0", "CC0-1.0"},
        "CC-BY-SA-4.0": {"cc-by-sa-4.0", "CC-BY-SA-4.0"},
    }
    other = {"other", "other-at"}
    for lid, ids in hubs.HUB_IDS.items():
        for hub_id in ids:
            assert hub_id in same.get(lid, set()) | other, f"{lid} would go up as {hub_id}"
    for lic in hubs.LICENCES.values():
        if "other" in (lic.huggingface, lic.kaggle):
            e = make(license=lic.url)
            for text in (hubs.hf_card(e, "r"), hubs.kaggle_metadata(e, "o")["description"]):
                assert lic.title in text
                assert lic.url in text


def test_an_unmapped_licence_is_refused():
    with pytest.raises(hubs.Refused, match="licence"):
        make(license="https://creativecommons.org/licenses/by-nd/4.0/")


def test_a_version_missing_from_versions_json_is_refused():
    with pytest.raises(hubs.Refused, match=re.escape("versions.json")):
        make(versionInfo="2030-01-01")


def test_an_australian_port_is_named_where_the_hub_has_no_id():
    e = make(license="https://creativecommons.org/licenses/by/3.0/au/")
    card = hubs.hf_card(e, "publicdata-au/x")
    assert 'license: "other"' in card
    assert 'license_name: "cc-by-3.0-au"' in card
    assert hubs.kaggle_metadata(e, "o")["licenses"] == [{"name": "other"}]
    z = hubs.zenodo_metadata(e)
    assert z["license"] == "other-at"
    assert "CC BY 3.0 AU" in z["description"]
    assert "https://creativecommons.org/licenses/by/3.0/au/" in z["description"]


def test_hugging_face_card_frontmatter_points_at_the_parquet_and_sizes_the_rows():
    card = hubs.hf_card(make(), "publicdata-au/qld-road-crash-factors")
    front = card.split("---\n")[1]
    assert 'data_files: "data.parquet"' in front
    assert '- "1K<n<10K"' in front
    assert '- "road-crashes"' in front
    assert "queensland-road-crash-factors" not in front
    assert 'revision="v2026-09-30"' in card
    assert "publicdata-au/qld-road-crash-factors" in card


def test_field_table_escapes_pipes_and_skips_source_cell_notes():
    card = hubs.hf_card(make(), "r")
    assert "| `factor` | string | Drink \\| drug driving and others. |" in card
    assert "| `region` | string | Region |" in card
    assert "| `year` | integer | Year |" in card


def test_query_api_is_named_only_when_it_serves_the_dataset():
    assert "/api/v1/datasets/qld-road-crash-factors/rows" in hubs.hf_card(make(), "r")
    quiet = hubs.entry(RECORD, VERSIONS, SCHEMA, MANIFEST, queryable=False)
    for t in text_of(quiet):
        assert "/api/v1/" not in t
        assert "query API" not in t


def test_kaggle_limits():
    m = hubs.kaggle_metadata(make(), "publicdataau")
    assert len(m["title"]) <= 50
    assert m["title"] == "Factors in road crashes, Queensland"
    assert 20 <= len(m["subtitle"]) <= 80
    assert m["id"] == "publicdataau/qld-road-crash-factors"
    types = {f["name"]: f["type"] for f in m["resources"][0]["schema"]["fields"]}
    assert types == {"year": "numeric", "factor": "string", "region": "string"}
    with pytest.raises(hubs.Refused, match="Kaggle"):
        hubs.kaggle_metadata(make(identifier="x" * 51), "o")


def test_zenodo_record_relates_the_version_the_dataset_and_the_source():
    m = hubs.zenodo_metadata(make(), community="publicdata-au")
    rel = {r["relation"]: r["identifier"] for r in m["related_identifiers"]}
    assert rel == {
        "isIdenticalTo": "https://publicdata.au/d/qld-road-crash-factors/v/2026-09-30/",
        "isVersionOf": "https://publicdata.au/d/qld-road-crash-factors/",
        "isDerivedFrom": "https://www.data.qld.gov.au/dataset/crash-data-from-queensland-roads",
    }
    assert m["version"] == m["publication_date"] == "2026-09-30"
    assert m["creators"] == [{"name": "Department of Transport and Main Roads"}]
    assert m["communities"] == [{"identifier": "publicdata-au"}]
    assert "<script" not in m["description"]


class FakeHub:
    name = "fake"

    def __init__(self, held=(), fail=False):
        self._held = set(held)
        self.fail = fail
        self.published = []

    def held(self, e):
        return self._held

    def publish(self, e, work, fetch):
        if self.fail:
            msg = "boom"
            raise RuntimeError(msg)
        fetch(e.files["parquet"], work / "data.parquet")
        assert (work / "data.parquet").read_bytes() == b"PAR1"
        self.published.append(e.version)
        return "https://hub.example/x"


def fake_fetch(url, dest):
    if dest.name == "card.png":
        from PIL import Image

        Image.new("RGB", (1200, 630), (10, 80, 160)).save(dest)
        return
    dest.write_bytes(b"PAR1")


def test_run_publishes_only_what_a_hub_lacks(tmp_path):
    e = make()
    fresh, current, ahead = FakeHub(), FakeHub(held={"2026-09-30"}), FakeHub(held={"2027-01-01"})
    lines = []
    fails = hubs.run(
        {"a": fresh, "b": current, "c": ahead},
        [(e.slug, e)],
        fake_fetch,
        tmp_path,
        lines.append,
    )
    assert fails == 0
    assert fresh.published == ["2026-09-30"]
    assert current.published == ahead.published == []
    assert any("holds 2026-09-30" in ln for ln in lines)
    assert any("newer than the site's" in ln for ln in lines)
    assert list(tmp_path.iterdir()) == []


def test_one_failure_is_counted_and_the_rest_carry_on(tmp_path):
    e = make()
    bad, good = FakeHub(fail=True), FakeHub()
    refused = ("x", hubs.Refused("no licence"))
    lines = []
    fails = hubs.run(
        {"bad": bad, "good": good}, [refused, (e.slug, e)], fake_fetch, tmp_path, lines.append
    )
    assert fails == 3
    assert good.published == ["2026-09-30"]
    assert any(
        re.search(r"bad qld-road-crash-factors: FAILED RuntimeError: boom", ln) for ln in lines
    )


def test_configured_skips_a_hub_without_credentials():
    hubs_, skipped = hubs.configured({"HF_TOKEN": "t", "KAGGLE_API_TOKEN": "k"})
    assert list(hubs_) == ["huggingface"]
    assert len(skipped) == 2
    assert all("not" in s for s in skipped)
    legacy, _ = hubs.configured({"KAGGLE_USERNAME": "u", "KAGGLE_KEY": "k"})
    assert "kaggle" not in legacy
    both, _ = hubs.configured({"KAGGLE_USERNAME": "u", "KAGGLE_API_TOKEN": "k"})
    k = both["kaggle"]()
    assert (k.owner, k.token) == ("u", "k")


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeZenodoHttp:
    """Enough of Zenodo's deposit API to follow a new version through to publish."""

    def __init__(self, records, fail_metadata=False, found=()):
        self.records = records
        self.found = list(found)
        self.fail_metadata = fail_metadata
        self.calls = []
        self.headers = {}

    def request(self, method, url, timeout=None, **kw):
        path = url.split("/api", 1)[-1] if "/api/" in url else url
        self.calls.append((method, path))
        if method == "GET" and path == "/deposit/depositions":
            if "q" in kw["params"]:
                return FakeResponse(200, self.found)
            return FakeResponse(200, self.records if kw["params"]["page"] == 1 else [])
        if method == "POST" and path.endswith("/actions/newversion"):
            return FakeResponse(
                201, {"links": {"latest_draft": "https://z/api/deposit/depositions/9"}}
            )
        if method == "GET" and path == "/deposit/depositions/9":
            return FakeResponse(
                200, {"id": 9, "files": [{"id": "f1"}], "links": {"bucket": "https://z/b/9"}}
            )
        if method == "POST" and path == "/deposit/depositions":
            return FakeResponse(201, {"id": 5, "links": {"bucket": "https://z/b/5"}})
        if method == "PUT" and path.startswith("/deposit/depositions/") and self.fail_metadata:
            return FakeResponse(400, {"errors": [{"field": "metadata.license"}]})
        if method == "POST" and path.endswith("/actions/publish"):
            id_ = int(path.split("/")[3])
            return FakeResponse(
                202,
                {
                    **record(id_, "2026-09-30"),
                    "conceptdoi": "10.5281/zenodo.8",
                    "doi_url": "https://doi.org/10.5281/zenodo.9",
                },
            )
        return FakeResponse(200, {})


def zenodo(records, **kw):
    z = hubs.Zenodo("t", base="https://z")
    z.http = FakeZenodoHttp(records, **kw)
    return z


def record(id_, version, submitted=True, concept=None):
    return {
        "id": id_,
        "submitted": submitted,
        "conceptdoi": f"10.5281/zenodo.{concept or id_ - 1}",
        "metadata": {
            "version": version,
            "related_identifiers": [
                {
                    "identifier": "https://publicdata.au/d/qld-road-crash-factors/",
                    "relation": "isVersionOf",
                }
            ],
        },
    }


def test_zenodo_holds_the_submitted_versions_of_this_dataset_only():
    other = record(3, "2026-09-30")
    other["metadata"]["related_identifiers"][0]["identifier"] = "https://publicdata.au/d/other/"
    z = zenodo([record(1, "2025-01-01"), record(2, "2026-09-30", submitted=False), other])
    assert z.held(make()) == {"2025-01-01"}


def test_zenodo_new_version_discards_a_stray_draft_and_replaces_the_files(tmp_path):
    z = zenodo([record(1, "2025-01-01"), record(2, "2026-09-30", submitted=False)])
    doi = z.publish(make(), tmp_path, fake_fetch)
    calls = z.http.calls
    assert doi == "https://doi.org/10.5281/zenodo.9"
    assert ("DELETE", "/deposit/depositions/2") in calls
    assert ("POST", "/deposit/depositions/1/actions/newversion") in calls
    assert ("DELETE", "/deposit/depositions/9/files/f1") in calls
    puts = [p for m, p in calls if m == "PUT" and p.startswith("https://z/b/9/")]
    assert sorted(puts) == [
        f"https://z/b/9/{n}" for n in ("data.csv", "data.parquet", "publicdata.json", "schema.json")
    ]
    assert calls[-1] == ("POST", "/deposit/depositions/9/actions/publish")


def test_zenodo_checks_metadata_before_files_and_deletes_a_draft_it_rejects(tmp_path):
    z = zenodo([], fail_metadata=True)
    with pytest.raises(RuntimeError, match="400"):
        z.publish(make(), tmp_path, fake_fetch)
    calls = z.http.calls
    assert ("DELETE", "/deposit/depositions/5") in calls
    assert not any(m == "PUT" and p.startswith("https://z/b/") for m, p in calls)
    assert not any(p.endswith("/actions/publish") for _, p in calls)


def test_a_failed_draft_cleanup_never_hides_the_original_error(tmp_path):
    z = zenodo([], fail_metadata=True)
    real = z.http.request

    def flaky(method, url, timeout=None, **kw):
        if method == "DELETE":
            msg = "network down"
            raise ConnectionError(msg)
        return real(method, url, timeout=timeout, **kw)

    z.http.request = flaky
    with pytest.raises(RuntimeError, match="400"):
        z.publish(make(), tmp_path, fake_fetch)


def test_zenodo_first_version_creates_a_deposition(tmp_path):
    z = zenodo([])
    z.publish(make(), tmp_path, fake_fetch)
    assert ("POST", "/deposit/depositions") in z.http.calls
    assert z.http.calls[-1] == ("POST", "/deposit/depositions/5/actions/publish")


def test_zenodo_keeps_its_listing_in_step_after_a_publish_instead_of_reading_it_again(tmp_path):
    z = zenodo([record(1, "2025-01-01"), record(2, "2026-09-30", submitted=False)])
    e = make()
    assert z.held(e) == {"2025-01-01"}
    z.publish(e, tmp_path, fake_fetch)
    listings = [c for c in z.http.calls if c == ("GET", "/deposit/depositions")]
    assert len(listings) == 1
    assert z.held(e) == {"2025-01-01", "2026-09-30"}
    assert z.location(e) == "10.5281/zenodo.0"
    assert len(listings) == 1
    assert [r["id"] for r in z.records()] == [1, 9]


def test_zenodo_asks_for_the_dataset_by_name_before_making_a_new_record(tmp_path):
    z = zenodo([], found=[record(1, "2025-01-01")])
    z.publish(make(), tmp_path, fake_fetch)
    calls = z.http.calls
    assert ("POST", "/deposit/depositions") not in calls
    assert ("POST", "/deposit/depositions/1/actions/newversion") in calls
    assert z.location(make()) == "10.5281/zenodo.0"


def test_zenodo_grows_the_oldest_record_when_a_dataset_has_two(tmp_path):
    z = zenodo([record(40, "2025-01-01", concept=39), record(10, "2025-01-01", concept=9)])
    e = make()
    assert z.location(e) == "10.5281/zenodo.9"
    z.publish(e, tmp_path, fake_fetch)
    assert ("POST", "/deposit/depositions/10/actions/newversion") in z.http.calls


def fake_cli(tmp_path, script):
    cli = tmp_path / "kaggle"
    cli.write_text("#!/bin/sh\n" + script)
    cli.chmod(0o755)
    return str(cli)


KAGGLE_LIST = (
    'if [ "$2" = "list" ]; then\n'
    '  [ "$KAGGLE_API_TOKEN" = "tok" ] || { echo "403 Client Error: Forbidden"; exit 1; }\n'
    "  printf 'ref,title,size\\no/qld-road-crash-factors,T,1\\no/other,U,2\\n'; exit 0\n"
    "fi\n"
)


def test_kaggle_reads_the_version_from_publicdata_json(tmp_path):
    cli = fake_cli(
        tmp_path,
        KAGGLE_LIST
        + 'while [ "$1" != "-p" ]; do shift; done; echo \'{"version": "2025-01-01"}\' > "$2/publicdata.json"\n',
    )
    assert hubs.Kaggle("o", "tok", cli).held(make()) == {"2025-01-01"}


def test_kaggle_dataset_missing_from_the_accounts_list_holds_nothing(tmp_path):
    # Kaggle answers 403 for a dataset that does not exist, so a download is never tried.
    cli = fake_cli(tmp_path, KAGGLE_LIST + 'echo "403 Client Error: Forbidden"; exit 1\n')
    assert hubs.Kaggle("o", "tok", cli).held(make(identifier="not-there-yet")) == set()


def test_kaggle_bad_credentials_fail_at_the_list_and_never_read_as_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    cli = fake_cli(tmp_path, KAGGLE_LIST)
    with pytest.raises(RuntimeError, match="credentials"):
        hubs.Kaggle("o", "wrong", cli).held(make())


def test_kaggle_retries_a_throttled_call_that_exits_zero(tmp_path):
    # The CLI prints Kaggle's 429 and still exits 0; the third call gets through.
    n = tmp_path / "n"
    cli = fake_cli(
        tmp_path,
        f'echo x >> {n}; [ "$(wc -l < {n})" -lt 3 ] && {{ echo "429 Client Error: Too Many Requests for url"; exit 0; }}\n'
        + KAGGLE_LIST,
    )
    k = hubs.Kaggle("o", "tok", cli)
    k.pause = 0
    assert "qld-road-crash-factors" in k.mine()


def test_kaggle_never_reads_a_throttled_answer_as_absent(tmp_path):
    cli = fake_cli(tmp_path, 'echo "429 Client Error: Too Many Requests for url"; exit 0\n')
    k = hubs.Kaggle("o", "tok", cli)
    k.pause = 0
    with pytest.raises(RuntimeError, match="kept refusing the dataset list"):
        k.held(make())
    k._mine = set()
    with pytest.raises(RuntimeError, match="kept refusing the status"):
        k.location(make())


def test_kaggle_lists_every_page_of_the_accounts_datasets(tmp_path):
    rows = "".join(f"o/d{i},T,1\\n" for i in range(200))
    cli = fake_cli(
        tmp_path,
        'if [ "$7" = "-p" ] && [ "$8" = "1" ]; then printf \'ref,title,size\\n'
        + rows
        + "'; else printf 'ref,title,size\\no/last,T,1\\n'; fi\n",
    )
    k = hubs.Kaggle("o", "tok", cli)
    assert len(k.mine()) == 201
    assert "last" in k.mine()


def kaggle_cli(tmp_path, status="ready", extra=""):
    """A fake CLI that logs each call and answers status with `status`.

    It keeps what metadata --update and kernels push were given.
    """
    log, kept = tmp_path / "args", tmp_path / "kept"
    kept.mkdir(exist_ok=True)
    return (
        fake_cli(
            tmp_path,
            KAGGLE_LIST
            + extra
            + f'echo "$@" >> {log}\n'
            + f'if [ "$1" = "kernels" ] && [ "$2" = "status" ]; then [ -f {tmp_path}/pushed ] && {{ echo complete; exit 0; }}; echo "404 Not Found"; exit 1; fi\n'
            + f'if [ "$1" = "kernels" ] && [ "$2" = "push" ]; then touch {tmp_path}/pushed; fi\n'
            + f'if [ "$2" = "status" ]; then echo "{status}"; exit 0; fi\n'
            + 'if [ "$2" = "metadata" ] || [ "$2" = "push" ]; then\n'
            + '  while [ "$1" != "-p" ]; do shift; done\n'
            + f'  cp -r "$2" {kept}/$(basename "$2"); fi\n',
        ),
        log,
        kept,
    )


def test_kaggle_first_upload_creates_and_later_ones_version(tmp_path):
    cli, log, _ = kaggle_cli(tmp_path)
    k = hubs.Kaggle("o", "tok", cli)
    for first in (True, False):
        work = tmp_path / f"w{first}"
        work.mkdir()
        k.publish(make(), work, fake_fetch, first=first)
        meta = json.loads((work / "dataset-metadata.json").read_text())
        assert meta["id"] == "o/qld-road-crash-factors"
        assert {p.name for p in work.iterdir()} == {
            "dataset-metadata.json",
            "publicdata.json",
            "data.parquet",
            "data.csv",
        }
    calls = [c for c in log.read_text().splitlines() if c.split()[1] in ("create", "version")]
    created, settled, versioned = calls
    assert created.startswith("datasets create")
    assert " -u " in created
    assert " -t" in created
    assert settled.startswith("datasets version")
    assert "Page settings" in settled
    assert versioned.startswith("datasets version")
    assert "publicdata.au version 2026-09-30" in versioned


def test_kaggle_publish_then_sets_every_usability_item(tmp_path):
    cli, log, kept = kaggle_cli(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    import dataclasses

    roads = dataclasses.replace(make(), topics=("roads",))
    hubs.Kaggle("o", "tok", cli).publish(roads, work, fake_fetch, first=True)
    order = [c.split()[1] for c in log.read_text().splitlines()]
    assert order == [
        "create",
        "status",
        "metadata",
        "version",
        "status",
        "metadata",
        "status",
        "push",
    ]
    (meta_dir,) = [p for p in kept.iterdir() if p.name.endswith("-meta")]
    meta = json.loads((meta_dir / "dataset-metadata.json").read_text())
    assert "australia" in meta["keywords"]
    assert "transportation" in meta["keywords"]
    assert meta["expectedUpdateFrequency"] == "annually"
    assert "SHA-256" in meta["userSpecifiedSources"]
    assert "/v/2026-09-30/" in meta["userSpecifiedSources"]
    assert all(f["description"] for f in meta["resources"][0]["schema"]["fields"])
    assert all(r["description"] for r in meta["resources"])
    from PIL import Image

    with Image.open(meta_dir / "dataset-cover-image.png") as im:
        assert im.size == (560, 280)
    nb_meta = json.loads((kept / "notebook" / "kernel-metadata.json").read_text())
    assert nb_meta["id"] == "o/qld-road-crash-factors-quick-start"
    assert nb_meta["is_private"] == "false"
    assert nb_meta["dataset_sources"] == ["o/qld-road-crash-factors"]
    assert not (work.parent / "w-meta").exists()


def test_kaggle_drops_the_tags_it_names_as_invalid_and_remembers_them(tmp_path):
    import dataclasses

    sent = tmp_path / "sent"
    cli, _, _ = kaggle_cli(
        tmp_path,
        extra='if [ "$2" = "metadata" ]; then while [ "$1" != "-p" ]; do shift; done; '
        f'cat "$2/dataset-metadata.json" >> {sent}; echo @@ >> {sent}; '
        'if grep -q "drink driving" "$2/dataset-metadata.json"; then '
        'echo \'The following keywords are invalid: "drink driving", "public data"\'; exit 1; fi; '
        "echo ok; exit 0; fi\n",
    )
    k = hubs.Kaggle("o", "tok", cli)
    e = dataclasses.replace(make(), topics=("roads",))
    for name in ("a", "b"):
        k.refresh(e, tmp_path / name, fake_fetch)
    tags = [json.loads(m)["keywords"] for m in sent.read_text().split("@@") if m.strip()]
    assert len(tags) == 5
    assert "drink driving" in tags[0]
    assert "public data" in tags[0]
    assert "drink driving" not in tags[1]
    assert "public data" not in tags[1]
    assert "australia" in tags[1]
    assert all(t == tags[1] for t in tags[2:])


def test_a_notebook_refused_for_an_unverified_account_says_so(tmp_path):
    cli, _, _ = kaggle_cli(
        tmp_path,
        extra='if [ "$2" = "push" ]; then echo "403 Client Error: Forbidden for url: '
        'https://api.kaggle.com/v1/kernels.KernelsApiService/SaveKernel"; exit 1; fi\n',
    )
    with pytest.raises(RuntimeError, match="phone verified"):
        hubs.Kaggle("o", "tok", cli).refresh(make(), tmp_path / "w", fake_fetch)


def test_kaggle_retries_a_throttled_settings_update(tmp_path, monkeypatch):
    monkeypatch.setattr(hubs.time, "sleep", lambda s: None)
    n = tmp_path / "n"
    cli, _, _kept = kaggle_cli(
        tmp_path,
        extra=f'if [ "$2" = "metadata" ] && [ "$3" != "o/qld-road-crash-factors" -o "$4" = "--update" ]; then '
        f"c=$(cat {n} 2>/dev/null || echo 0); echo $((c+1)) > {n}; "
        '[ $c -lt 2 ] && { echo "Expecting value: line 1 column 1 (char 0)"; exit 1; }; '
        "echo ok; exit 0; fi\n",
    )
    hubs.Kaggle("o", "tok", cli).refresh(make(), tmp_path / "w", fake_fetch)
    assert n.read_text().strip() == "4"


def test_kaggle_waits_for_processing_and_stops_on_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(hubs.time, "sleep", lambda s: None)
    count = tmp_path / "n"
    cli, _, _ = kaggle_cli(
        tmp_path,
        extra=f'if [ "$2" = "status" ]; then n=$(cat {count} 2>/dev/null || echo 0); '
        f"echo $((n+1)) > {count}; [ $n -ge 2 ] && echo ready || echo running; exit 0; fi\n",
    )
    hubs.Kaggle("o", "tok", cli).wait_ready("o/x")
    assert count.read_text().strip() == "3"
    (tmp_path / "kaggle").unlink()
    bad, _, _ = kaggle_cli(tmp_path, status="error: invalid file")
    with pytest.raises(RuntimeError, match="could not process"):
        hubs.Kaggle("o", "tok", bad).wait_ready("o/x")


def test_a_later_version_gets_its_settings_before_its_upload(tmp_path):
    cli, log, _ = kaggle_cli(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    hubs.Kaggle("o", "tok", cli).publish(make(), work, fake_fetch, first=False)
    order = [c.split()[1] for c in log.read_text().splitlines()]
    assert order == ["status", "metadata", "version", "status", "metadata", "status", "push"]


def test_refresh_uploads_the_files_again_after_the_settings(tmp_path):
    cli, log, _ = kaggle_cli(tmp_path)
    hubs.Kaggle("o", "tok", cli).refresh(make(), tmp_path / "w", fake_fetch)
    calls = log.read_text().splitlines()
    order = [c.split()[1] for c in calls]
    assert order[:7] == ["status", "metadata", "version", "status", "metadata", "status", "push"]
    assert "Page settings for publicdata.au version 2026-09-30" in calls[2]


def test_kaggle_publish_adds_the_slug_to_the_cached_list(tmp_path):
    made = tmp_path / "made"
    cli, _, _ = kaggle_cli(
        tmp_path,
        extra=f'if [ "$2" = "create" ]; then touch {made}; exit 0; fi\n'
        f'if [ "$2" = "status" ]; then [ -f {made} ] && {{ echo ready; exit 0; }}; echo "403 Forbidden"; exit 1; fi\n',
    )
    k = hubs.Kaggle("o", "tok", cli)
    e = make(identifier="new-one")
    assert k.held(e) == set()
    work = tmp_path / "w"
    work.mkdir()
    k.publish(e, work, fake_fetch, first=True)
    assert "new-one" in k.mine()


def test_kaggle_error_output_fails_even_with_a_zero_exit(tmp_path):
    cli = fake_cli(tmp_path, 'echo "Dataset creation error: title too long"\n')
    with pytest.raises(RuntimeError, match="title too long"):
        hubs.Kaggle("o", "tok", cli).publish(make(), tmp_path, fake_fetch, first=True)


def test_refresh_reapplies_settings_to_a_held_version_and_holds_otherwise(tmp_path):
    class Refreshable(FakeHub):
        def __init__(self):
            super().__init__(held={"2026-09-30"})
            self.refreshed = []

        def refresh(self, e, work, fetch):
            self.refreshed.append(e.version)
            return "https://hub.example/x"

    e, lines = make(), []
    hub = Refreshable()
    hubs.run({"h": hub}, [(e.slug, e)], fake_fetch, tmp_path, lines.append)
    assert hub.refreshed == []
    assert "holds" in lines[-1]
    hubs.run({"h": hub}, [(e.slug, e)], fake_fetch, tmp_path, lines.append, refresh=True)
    assert hub.refreshed == ["2026-09-30"]
    assert "refreshed" in lines[-1]
    assert hub.published == []


def test_every_field_gets_a_description_from_the_register_or_its_name():
    class F:
        def __init__(self, name, label="", description=""):
            self.name, self.label, self.description = name, label, description

    class Reg:
        fields = (F("region", label="Region"),)
        topics = ("roads",)

    e = hubs.entry(RECORD, VERSIONS, SCHEMA, MANIFEST, registered=Reg())
    got = {f["name"]: f["description"] for f in e.fields}
    assert got == {"year": "Year", "factor": "Drink | drug driving and others.", "region": "Region"}
    assert e.topics == ("roads",)
    bare = hubs.entry(
        RECORD, VERSIONS, {"fields": [{"name": "crash_severity", "title": "(cell)"}]}, MANIFEST
    )
    assert bare.fields[0]["description"]


def test_notebook_titles_are_unique_per_slug_and_short_enough():
    a = make(
        identifier="au-migration-program-by-citizenship",
        title="Migration Program outcome by stream",
    )
    b = make(identifier="au-migration-program-outcome", title="Migration Program outcome by stream")
    ia, ib = (hubs.kaggle_notebook(x, "o")[0] for x in (a, b))
    assert ia["id"] != ib["id"]
    for slug in ("x" * 45, "a" * 40 + "-one", "a" * 40 + "-two"):
        title = hubs.kaggle_notebook(make(identifier=slug), "o")[0]["title"]
        assert len(title) <= 50
        assert title.endswith(" quick start")
    one, two = (
        hubs.kaggle_notebook(make(identifier="a" * 40 + s), "o")[0]["id"] for s in ("-one", "-two")
    )
    assert one != two


def test_cover_image_is_cut_to_two_by_one(tmp_path):
    from PIL import Image

    for size in ((1200, 630), (800, 800), (2000, 500)):
        src = tmp_path / "c.png"
        Image.new("RGB", size).save(src)
        with Image.open(hubs.cover_image(src, tmp_path / "out.png")) as im:
            assert im.size == (560, 280)


def test_every_catalogue_cadence_maps_to_a_kaggle_choice():
    allowed = {"never", "annually", "quarterly", "monthly", "weekly", "daily", "hourly"}
    cadences = {d.source.cadence for d in load(ROOT / "register")}
    assert {kaggle_frequency(c) for c in cadences} <= allowed
    assert hubs.kaggle_metadata(make(), "o")["expectedUpdateFrequency"] in allowed


def test_hubs_render_command_writes_every_hubs_files(tmp_path, monkeypatch):
    from publicdata.__main__ import main

    monkeypatch.setattr(
        hubs, "read_site", lambda site, only, register=None: iter([(RECORD["identifier"], make())])
    )
    assert main(["hubs", "--render", str(tmp_path)]) == 0
    got = sorted(p.name for p in (tmp_path / "qld-road-crash-factors").iterdir())
    assert got == [
        "huggingface-README.md",
        "kaggle-dataset-metadata.json",
        "kaggle-kernel-metadata.json",
        "kaggle-notebook.ipynb",
        "publicdata.json",
        "zenodo-metadata.json",
    ]


def test_hubs_command_fails_when_a_hub_fails(monkeypatch, capsys):
    from publicdata.__main__ import main

    monkeypatch.setattr(
        hubs, "read_site", lambda site, only, register=None: iter([("x", hubs.Refused("nope"))])
    )
    monkeypatch.setattr(hubs, "configured", lambda: ({"fake": FakeHub}, []))
    assert main(["hubs", "--hub", "fake"]) == 1
    assert "REFUSED nope" in capsys.readouterr().out


def test_workflow_passes_each_credential_the_module_reads():
    wf = (Path(__file__).resolve().parents[2] / ".github/workflows/hubs.yml").read_text()
    for name in (
        "HF_TOKEN",
        "ZENODO_TOKEN",
        "KAGGLE_API_TOKEN",
        "KAGGLE_USERNAME",
        "HF_NAMESPACE",
        "ZENODO_URL",
        "ZENODO_COMMUNITY",
    ):
        assert name in wf
    assert yaml.safe_load(wf)["jobs"]["publish"]


class FakeSiteHttp:
    """The live site's catalogue files, with one dataset whose schema cannot be read."""

    def __init__(self):
        broken = {**RECORD, "identifier": "broken"}
        self.files = {
            "https://publicdata.au/catalog.json": {
                "dataset": [RECORD, broken, {**RECORD, "identifier": "skipped"}]
            },
            "https://publicdata.au/d/qld-road-crash-factors/versions.json": VERSIONS,
            "https://publicdata.au/d/qld-road-crash-factors/v/2026-09-30/schema.json": SCHEMA,
            "https://publicdata.au/d/qld-road-crash-factors/v/2026-09-30/manifest.json": MANIFEST,
            "https://publicdata.au/d/broken/versions.json": VERSIONS,
        }
        self.served = {"https://publicdata.au/api/v1/datasets/qld-road-crash-factors/versions"}
        self.asked = []

    def get(self, url, timeout=None):
        import requests

        self.asked.append(url)
        body = self.files.get(url)
        status = 200 if body is not None or url in self.served else 404

        class R:
            status_code = status

            def raise_for_status(self):
                if status >= 400:
                    msg = f"{status} for {url}"
                    raise requests.HTTPError(msg)

            def json(self):
                return body

        return R()


def test_read_site_builds_entries_and_turns_a_failed_read_into_a_refusal():
    http = FakeSiteHttp()
    got = dict(hubs.read_site("https://publicdata.au", {"qld-road-crash-factors", "broken"}, http))
    assert set(got) == {"qld-road-crash-factors", "broken"}
    e = got["qld-road-crash-factors"]
    assert isinstance(e, hubs.Entry)
    assert e.queryable
    assert e.rows == 1234
    assert e.files["csv"].endswith("/v/2026-09-30/data.csv")
    assert "json" in e.files
    assert e.source_page == "https://www.data.qld.gov.au/dataset/crash-data-from-queensland-roads"
    assert isinstance(got["broken"], hubs.Refused)
    assert "404" in str(got["broken"])
    assert not any("/skipped/" in u for u in http.asked)


def test_read_site_marks_a_dataset_the_query_api_lacks():
    http = FakeSiteHttp()
    http.served = set()
    (slug, e), *_ = hubs.read_site("https://publicdata.au", {"qld-road-crash-factors"}, http)
    assert slug == "qld-road-crash-factors"
    assert not e.queryable


def test_the_installed_kaggle_sdk_reads_the_token_from_kaggle_api_token(monkeypatch):
    env = pytest.importorskip("kagglesdk.kaggle_env")
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok-123")
    assert env.get_access_token_from_env() == ("tok-123", "KAGGLE_API_TOKEN")


def test_the_installed_hugging_face_library_has_the_calls_the_job_makes():
    hf = pytest.importorskip("huggingface_hub")
    for name in ("create_repo", "upload_folder", "create_tag", "list_repo_refs"):
        assert callable(getattr(hf.HfApi, name))
    from huggingface_hub.errors import RepositoryNotFoundError  # noqa: F401


def test_every_kaggle_column_type_is_one_kaggle_documents():
    documented = {"string", "numeric", "boolean", "datetime", "latitude", "longitude"}
    assert set(hubs.KAGGLE_TYPES.values()) <= documented
    fields = hubs.kaggle_metadata(make(), "o")["resources"][0]["schema"]["fields"]
    assert {f["type"] for f in fields} <= documented


class Locating(FakeHub):
    account = "https://hub.example/org"

    def location(self, e):
        return f"https://hub.example/{e.slug}"


def test_run_records_where_each_copy_is_held_published_or_not_reached(tmp_path):
    e, other = make(), make(identifier="other-one")
    record: dict = {}
    hubs.run(
        {"h": Locating(held={"2026-09-30"}), "bad": FakeHub(fail=True)},
        [(e.slug, e), (other.slug, other)],
        fake_fetch,
        tmp_path,
        lambda line: None,
        record=record,
    )
    assert record == {
        "datasets": {
            "qld-road-crash-factors": {"h": "https://hub.example/qld-road-crash-factors"},
            "other-one": {"h": "https://hub.example/other-one"},
        },
        "accounts": {"h": "https://hub.example/org"},
    }


def test_merge_record_keeps_what_a_partial_run_did_not_reach():
    old = {
        "accounts": {"kaggle": "k"},
        "datasets": {"a": {"kaggle": "ka", "zenodo": "10.1/old"}, "b": {"kaggle": "kb"}},
    }
    new = {"accounts": {"huggingface": "h"}, "datasets": {"a": {"zenodo": "10.1/new"}}}
    assert hubs.merge_record(old, new) == {
        "accounts": {"huggingface": "h", "kaggle": "k"},
        "datasets": {"a": {"kaggle": "ka", "zenodo": "10.1/new"}, "b": {"kaggle": "kb"}},
    }


def test_copies_are_listed_in_a_fixed_order_with_the_doi_last():
    from publicdata.site import copies_of

    rec = {
        "datasets": {
            "x": {
                "zenodo": "10.5281/zenodo.9",
                "kaggle": "https://www.kaggle.com/datasets/o/x",
                "huggingface": "https://huggingface.co/datasets/O/x",
            }
        }
    }
    got = copies_of(rec, "x")
    assert [c["hub"] for c in got] == ["Hugging Face", "Kaggle", "Zenodo"]
    assert got[2]["url"] == "https://doi.org/10.5281/zenodo.9"
    assert got[0]["note"] == 'load_dataset("O/x")'
    assert copies_of(rec, "y") == []
    assert copies_of(None, "x") == []


def test_zenodo_location_is_the_concept_doi_of_a_submitted_record():
    z = zenodo(
        [
            {**record(1, "2025-01-01"), "conceptdoi": "10.5281/zenodo.7"},
            {**record(2, "2026-01-01", submitted=False), "conceptdoi": "10.5281/zenodo.8"},
        ]
    )
    assert z.location(make()) == "10.5281/zenodo.7"
    assert zenodo([]).location(make()) is None


def test_coordinates_are_typed_as_coordinates_and_tag_the_dataset():
    import dataclasses

    fields = (
        {"name": "crash_latitude", "type": "number"},
        {"name": "lng", "type": "number"},
        {"name": "lat_band", "type": "number"},
        {"name": "latitude", "type": "string"},
    )
    e = dataclasses.replace(make(), fields=fields)
    assert [hubs.kaggle_type(f) for f in fields] == ["latitude", "longitude", "numeric", "string"]
    assert "geospatial analysis" in hubs.kaggle_tags(e)
    assert "geospatial analysis" not in hubs.kaggle_tags(make())


def test_kaggle_carries_json_and_sqlite_unless_they_are_too_large(tmp_path):
    import dataclasses

    v = "https://publicdata.au/d/qld-road-crash-factors/v/2026-09-30/"
    files = {f: f"{v}data.{f}" for f in ("parquet", "csv", "json", "sqlite")}
    small = dataclasses.replace(make(), files=files, sizes={"json": 10, "sqlite": 10})
    big = dataclasses.replace(small, sizes={"json": hubs.KAGGLE_EXTRA_MAX + 1, "sqlite": 10})
    assert hubs.kaggle_formats(small) == ["parquet", "csv", "json", "sqlite"]
    assert hubs.kaggle_formats(big) == ["parquet", "csv", "sqlite"]
    meta = hubs.kaggle_metadata(small, "o")
    paths = [r["path"] for r in meta["resources"]]
    assert paths == ["data.parquet", "data.csv", "data.json", "data.sqlite", "publicdata.json"]
    assert all(r["description"] for r in meta["resources"])
    got = hubs.Kaggle("o", "tok").files(small, tmp_path, fake_fetch)
    assert sorted(p.name for p in got) == [
        "data.csv",
        "data.json",
        "data.parquet",
        "data.sqlite",
        "publicdata.json",
    ]


def test_the_search_title_leads_on_kaggle_when_it_fits():
    import dataclasses

    fits = dataclasses.replace(make(), search_title="Queensland road crash factors by year")
    long = dataclasses.replace(make(), search_title="x" * 51)
    assert hubs.kaggle_metadata(fits, "o")["title"] == "Queensland road crash factors by year"
    assert hubs.kaggle_metadata(long, "o")["title"] == "Factors in road crashes, Queensland"


def test_every_dataset_is_tagged_tabular_and_by_its_topics():
    import dataclasses

    tags = hubs.kaggle_tags(dataclasses.replace(make(), topics=("crime",)))
    assert tags[:3] == ["australia", "government", "tabular"]
    assert "crime" in tags
    assert "criminology" in tags
    assert len(tags) <= 20


class Reg:
    def __init__(self, status="live", licence="CC-BY-4.0", kind="table"):
        self.status = status
        self.licence = type("L", (), {"id": licence})()
        self.fields = ()
        self.topics = ()
        self.search_title = ""
        self.kind = kind


EXCLUDED_REASONS = ("condition of use", "not copied to the hubs")


def test_only_a_live_register_entry_with_the_same_open_licence_is_authorised():
    hubs.authorised(RECORD, Reg())
    for reg, why in (
        (None, "not in the register"),
        (Reg(status="building"), "not live"),
        (Reg(status="blocked", licence="CC-BY-NC-4.0"), "not live"),
        (Reg(licence="CC-BY-3.0-AU"), "the catalogue states"),
        (Reg(licence="OPEN-GNAF-EULA"), "condition of use"),
        (Reg(kind="database"), "not copied to the hubs"),
    ):
        with pytest.raises(hubs.Refused, match=why) as got:
            hubs.authorised(RECORD, reg)
        assert isinstance(got.value, hubs.Excluded) == (why in EXCLUDED_REASONS)


def test_read_site_refuses_what_the_register_does_not_authorise_before_reading_it():
    http = FakeSiteHttp()
    got = dict(
        hubs.read_site(
            "https://publicdata.au",
            {"qld-road-crash-factors", "broken"},
            http,
            register={"qld-road-crash-factors": Reg(licence="CC-BY-3.0-AU")},
        )
    )
    assert all(isinstance(v, hubs.Refused) for v in got.values())
    assert not any(isinstance(v, hubs.Excluded) for v in got.values())
    assert "not in the register" in str(got["broken"])
    assert http.asked == ["https://publicdata.au/catalog.json"]


def _with_formats(**sizes):
    import dataclasses

    v = "https://publicdata.au/d/qld-road-crash-factors/v/2026-09-30/"
    files = {f: f"{v}data.{f}" for f in ("parquet", "csv", "json", "sqlite", "xlsx")}
    return dataclasses.replace(make(), files=files, sizes=sizes)


def test_hugging_face_carries_only_the_parquet_and_clears_other_formats(tmp_path):
    # The Hub loads a repository with one builder; a CSV beside the Parquet broke its viewer.
    e = _with_formats(json=10, xlsx=10)
    front = yaml.safe_load(hubs.hf_card(e, "r").split("---\n")[1])
    assert front["configs"] == [{"config_name": "default", "data_files": "data.parquet"}]

    class Api:
        def __init__(self):
            self.calls = []

        def create_repo(self, *a, **k):
            pass

        def create_tag(self, *a, **k):
            pass

        def upload_folder(self, **k):
            self.calls.append(
                (sorted(p.name for p in k["folder_path"].iterdir()), k["delete_patterns"])
            )

    hub = object.__new__(hubs.HuggingFace)
    hub.namespace, hub.api = "o", Api()
    for name in ("p", "r"):
        (tmp_path / name).mkdir()
    hub.publish(e, tmp_path / "p", fake_fetch)
    hub.refresh(e, tmp_path / "r", fake_fetch)
    for names, stray in hub.api.calls:
        assert names == ["README.md", "data.parquet", "publicdata.json"]
        assert {"data.csv", "data.json"} <= set(stray)


def test_zenodo_carries_json_and_excel_and_names_every_file(tmp_path):
    e = _with_formats(json=10, xlsx=10, sqlite=10)
    got = [p.name for p in hubs.Zenodo("t").files(e, tmp_path, fake_fetch)]
    assert got == [
        "data.parquet",
        "data.csv",
        "data.json",
        "data.xlsx",
        "schema.json",
        "publicdata.json",
    ]
    desc = hubs.zenodo_metadata(e)["description"]
    assert "data.parquet, data.csv, data.json and data.xlsx" in desc
    big = _with_formats(json=hubs.EXTRA_MAX + 1, xlsx=10)
    (tmp_path / "b").mkdir()
    assert "data.json" not in [
        p.name for p in hubs.Zenodo("t").files(big, tmp_path / "b", fake_fetch)
    ]


def test_the_record_is_saved_after_every_dataset(tmp_path):
    e, other = make(), make(identifier="other-one")
    saved = []
    hubs.run(
        {"h": Locating()},
        [(e.slug, e), (other.slug, other)],
        fake_fetch,
        tmp_path,
        lambda line: None,
        record={},
        on_record=lambda r: saved.append(sorted(r["datasets"])),
    )
    assert saved == [["qld-road-crash-factors"], ["other-one", "qld-road-crash-factors"]]


def test_the_hubs_command_writes_the_record_as_it_goes(tmp_path, monkeypatch):
    from publicdata.__main__ import main

    rec = tmp_path / "hubs.json"
    rec.write_text('{"accounts": {}, "datasets": {"kept": {"h": "x"}}}')
    e = make()
    monkeypatch.setattr(hubs, "read_site", lambda site, only, register=None: iter([(e.slug, e)]))
    monkeypatch.setattr(
        hubs, "configured", lambda: ({"h": lambda: Locating(held={"2026-09-30"})}, [])
    )
    assert main(["hubs", "--hub", "h", "--record", str(rec)]) == 0
    got = json.loads(rec.read_text())
    assert not rec.with_name("hubs.json.tmp").exists()
    assert got["datasets"]["kept"] == {"h": "x"}
    assert got["datasets"]["qld-road-crash-factors"] == {
        "h": "https://hub.example/qld-road-crash-factors"
    }


def test_kaggle_uploads_carry_no_tags_and_the_settings_carry_them(tmp_path):
    import dataclasses

    e = dataclasses.replace(make(), topics=("roads",))
    hubs.Kaggle("o", "tok").files(e, tmp_path, fake_fetch)
    assert json.loads((tmp_path / "dataset-metadata.json").read_text())["keywords"] == []
    assert hubs.kaggle_metadata(e, "o")["keywords"]


def test_kaggle_trims_tags_until_the_category_limit_is_met(tmp_path):
    import dataclasses

    sent = tmp_path / "sent"
    cli, _, _ = kaggle_cli(
        tmp_path,
        extra='if [ "$2" = "metadata" ] && [ "$4" = "--update" ]; then while [ "$1" != "-p" ]; do shift; done; '
        f'n=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))[\\"keywords\\"]))" "$2/dataset-metadata.json"); echo $n >> {sent}; '
        '[ $n -gt 5 ] && { echo "Dataset update error: You have exceeded the max category limit"; exit 1; }; echo ok; exit 0; fi\n',
    )
    e = dataclasses.replace(make(), topics=("roads", "crime"))
    hubs.Kaggle("o", "tok", cli).settings(e, tmp_path / "m", fake_fetch)
    counts = [int(x) for x in sent.read_text().split()]
    assert counts[0] == hubs.TAG_LIMIT
    assert counts[-1] <= 5
    assert counts == sorted(counts, reverse=True)


def test_kaggle_waits_out_403s_while_a_new_dataset_registers(tmp_path, monkeypatch):
    monkeypatch.setattr(hubs.time, "sleep", lambda s: None)
    count = tmp_path / "n"
    cli, _, _ = kaggle_cli(
        tmp_path,
        extra=f'if [ "$2" = "status" ]; then n=$(cat {count} 2>/dev/null || echo 0); echo $((n+1)) > {count}; '
        '[ $n -ge 3 ] && { echo ready; exit 0; }; echo "403 Client Error: Forbidden"; exit 1; fi\n',
    )
    hubs.Kaggle("o", "tok", cli).wait_ready("o/x")
    assert count.read_text().strip() == "4"
    (tmp_path / "kaggle").unlink()
    always, _, _ = kaggle_cli(
        tmp_path, extra='if [ "$2" = "status" ]; then echo "403 Forbidden"; exit 1; fi\n'
    )
    with pytest.raises(RuntimeError, match="could not process"):
        hubs.Kaggle("o", "tok", always).wait_ready("o/x")


def test_zenodo_sends_a_file_again_after_a_gateway_error(tmp_path, monkeypatch):
    z = zenodo([])
    z.pause = 0
    real = z.http.request
    tries = {"n": 0}

    def flaky(method, url, timeout=None, **kw):
        if method == "PUT" and url.startswith("https://z/b/") and url.endswith("data.parquet"):
            tries["n"] += 1
            if tries["n"] < 3:
                return FakeResponse(502, {"error": "bad gateway"})
        return real(method, url, timeout=timeout, **kw)

    z.http.request = flaky
    z.publish(make(), tmp_path, fake_fetch)
    assert tries["n"] == 3
    assert z.http.calls[-1] == ("POST", "/deposit/depositions/5/actions/publish")


def test_a_dataset_the_accounts_list_has_not_caught_up_with_is_still_held(tmp_path):
    cli = fake_cli(
        tmp_path,
        'if [ "$2" = "list" ]; then printf "ref,title,size\\no/other,U,2\\n"; exit 0; fi\n'
        'if [ "$2" = "status" ]; then echo ready; exit 0; fi\n'
        'while [ "$1" != "-p" ]; do shift; done; echo \'{"version": "2026-09-30"}\' > "$2/publicdata.json"\n',
    )
    k = hubs.Kaggle("o", "tok", cli)
    assert "qld-road-crash-factors" not in k.mine()
    assert k.held(make()) == {"2026-09-30"}
    assert k.location(make()) == "https://www.kaggle.com/datasets/o/qld-road-crash-factors"


def test_kaggle_settings_name_the_licence_the_way_an_update_accepts_it(tmp_path):
    sent = tmp_path / "sent"
    cli, _, _ = kaggle_cli(
        tmp_path,
        extra='if [ "$2" = "metadata" ] && [ "$4" = "--update" ]; then while [ "$1" != "-p" ]; do shift; done; '
        f'cat "$2/dataset-metadata.json" > {sent}; echo ok; exit 0; fi\n',
    )
    hubs.Kaggle("o", "tok", cli).settings(make(), tmp_path / "m", fake_fetch)
    assert json.loads(sent.read_text())["licenses"] == [{"name": "CC BY 4.0"}]
    au = make(license="https://creativecommons.org/licenses/by/3.0/au/")
    assert hubs.kaggle_settings_licence(au) == "other"
    assert hubs.kaggle_metadata(make(), "o")["licenses"] == [{"name": "CC-BY-4.0"}]


def test_every_kaggle_licence_id_has_a_verified_settings_name():
    assert {lic.kaggle for lic in hubs.LICENCES.values()} <= set(hubs.KAGGLE_SETTINGS_LICENCE)
    cc0 = make(license="https://creativecommons.org/publicdomain/zero/1.0/")
    assert hubs.kaggle_settings_licence(cc0) == "CC0 1.0"


def test_a_licence_without_a_settings_name_is_refused_before_anything_is_uploaded(
    tmp_path, monkeypatch
):
    monkeypatch.delitem(hubs.KAGGLE_SETTINGS_LICENCE, "CC-BY-4.0")
    cli, log, _ = kaggle_cli(tmp_path)
    fetched = []
    (tmp_path / "w").mkdir()
    with pytest.raises(hubs.Refused, match="verified"):
        hubs.Kaggle("o", "tok", cli).publish(
            make(), tmp_path / "w", lambda u, d: fetched.append(u), first=True
        )
    assert fetched == []
    assert not log.exists()


def test_a_notebook_is_pushed_only_when_the_dataset_has_none(tmp_path):
    cli, log, _ = kaggle_cli(tmp_path)
    k = hubs.Kaggle("o", "tok", cli)
    for name in ("a", "b"):
        k.notebook(make(), tmp_path / name)
    pushes = [c for c in log.read_text().splitlines() if c.startswith("kernels push")]
    assert len(pushes) == 1


def test_a_held_dataset_without_its_notebook_gets_one_on_an_ordinary_run(tmp_path):
    cli = fake_cli(
        tmp_path,
        KAGGLE_LIST
        + f'echo "$@" >> {tmp_path}/args\n'
        + 'if [ "$1" = "kernels" ] && [ "$2" = "status" ]; then echo "404 Not Found"; exit 1; fi\n'
        + 'if [ "$1" = "kernels" ]; then exit 0; fi\n'
        + 'while [ "$1" != "-p" ]; do shift; done; echo \'{"version": "2026-09-30"}\' > "$2/publicdata.json"\n',
    )
    lines = []
    hubs.run(
        {"kaggle": hubs.Kaggle("o", "tok", cli)},
        [("qld-road-crash-factors", make())],
        fake_fetch,
        tmp_path,
        lines.append,
    )
    assert lines[-1] == "kaggle qld-road-crash-factors: holds 2026-09-30, added its notebook"
    assert any(c.startswith("kernels push") for c in (tmp_path / "args").read_text().splitlines())


def test_a_failed_notebook_check_is_raised_and_never_spends_a_push(tmp_path, monkeypatch):
    monkeypatch.setattr(hubs.time, "sleep", lambda s: None)
    log = tmp_path / "args"
    cli = fake_cli(
        tmp_path,
        f'echo "$@" >> {log}\n'
        'if [ "$1" = "kernels" ] && [ "$2" = "status" ]; then echo "Expecting value: line 1 column 1 (char 0)"; exit 1; fi\n',
    )
    with pytest.raises(RuntimeError, match="could not say"):
        hubs.Kaggle("o", "tok", cli).ensure(make(), tmp_path, fake_fetch)
    calls = log.read_text().splitlines()
    assert len([c for c in calls if c.startswith("kernels status")]) == 4
    assert not any(c.startswith("kernels push") for c in calls)


def test_a_missing_notebook_is_checked_once_before_its_push(tmp_path):
    cli, log, _ = kaggle_cli(tmp_path)
    hubs.Kaggle("o", "tok", cli).ensure(make(), tmp_path / "w", fake_fetch)
    calls = [c.split()[:2] for c in log.read_text().splitlines()]
    assert calls.count(["kernels", "status"]) == 1
    assert ["kernels", "push"] in calls


def test_kaggle_answering_kernels_get_denied_means_no_notebook_yet(tmp_path):
    cli = fake_cli(
        tmp_path,
        "echo \"Cannot access kernel 'o/x-quick-start' (Permission 'kernels.get' was denied).\"; exit 1\n",
    )
    assert hubs.Kaggle("o", "tok", cli).has_notebook(make()) is False


def test_kaggles_notebook_limit_defers_the_rest_of_the_run_without_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(hubs.time, "sleep", lambda s: None)
    cli = fake_cli(
        tmp_path,
        KAGGLE_LIST
        + f'echo "$@" >> {tmp_path}/args\n'
        + 'if [ "$1" = "kernels" ] && [ "$2" = "status" ]; then echo "404 Not Found"; exit 1; fi\n'
        + 'if [ "$1" = "kernels" ] && [ "$2" = "push" ]; then echo "429 Client Error: Too Many '
        'Requests for url: https://api.kaggle.com/v1/kernels.KernelsApiService/SaveKernel"; exit 1; fi\n'
        + 'while [ "$1" != "-p" ]; do shift; done; echo \'{"version": "2026-09-30"}\' > "$2/publicdata.json"\n',
    )
    lines = []
    a, b = make(), make(identifier="other")
    failures = hubs.run(
        {"kaggle": hubs.Kaggle("o", "tok", cli)},
        [(a.slug, a), (b.slug, b)],
        fake_fetch,
        tmp_path,
        lines.append,
    )
    assert failures == 0
    assert lines[0].endswith(
        "holds 2026-09-30, notebook deferred until Kaggle's limit on saving notebooks resets"
    )
    assert lines[1] == "kaggle other: holds 2026-09-30"
    calls = (tmp_path / "args").read_text().splitlines()
    assert len([c for c in calls if c.startswith("kernels push")]) == 1


def test_kaggles_running_notebook_limit_is_waited_out_then_deferred(tmp_path, monkeypatch):
    monkeypatch.setattr(hubs.time, "sleep", lambda s: None)
    busy = 'echo "Kernel push error: Maximum batch CPU session count of 5 reached."; exit 0\n'

    def kaggle(clears_after):
        n = tmp_path / f"n{clears_after}"
        cli = fake_cli(
            tmp_path,
            'if [ "$1" = "kernels" ] && [ "$2" = "push" ]; then\n'
            f'  echo x >> {n}; [ "$(wc -l < {n})" -le {clears_after} ] && {{ {busy.strip()}; }}\n'
            "  exit 0; fi\n",
        )
        return hubs.Kaggle("o", "tok", cli)

    k = kaggle(1)
    assert k.push_notebook(make(), tmp_path / "a") is True
    k = kaggle(9)
    assert k.push_notebook(make(), tmp_path / "b") is False
    assert k._notebooks_limited


def test_a_dataset_the_register_keeps_off_the_hubs_is_reported_but_is_no_failure(tmp_path):
    lines = []
    failures = hubs.run(
        {"h": FakeHub()},
        [
            ("gnaf", hubs.Excluded("gnaf: a database is served here only, not copied to the hubs")),
            ("bad", hubs.Refused("bad is in the catalogue but not in the register")),
        ],
        fake_fetch,
        tmp_path,
        lines.append,
    )
    assert failures == 1
    assert (
        lines[0]
        == "h gnaf: not copied, gnaf: a database is served here only, not copied to the hubs"
    )
    assert lines[1].startswith("h bad: REFUSED")


def test_read_site_passes_an_exclusion_through_as_an_exclusion():
    got = dict(
        hubs.read_site(
            "https://publicdata.au",
            {"qld-road-crash-factors"},
            FakeSiteHttp(),
            register={"qld-road-crash-factors": Reg(kind="database")},
        )
    )
    assert isinstance(got["qld-road-crash-factors"], hubs.Excluded)


def test_reading_the_site_retries_a_dropped_connection_but_never_an_upload():
    retry = hubs._http().get_adapter("https://publicdata.au/").max_retries
    assert retry.total >= 3
    assert retry.is_retry("GET", 503)
    assert not retry.is_retry("POST", 503)
