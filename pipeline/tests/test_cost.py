import datetime as dt
import json
import subprocess
import urllib.error
from dataclasses import replace

import pytest

from publicdata import cache, cadence, cost
from publicdata.__main__ import main
from publicdata.register import Field, Source

from .conftest import ROOT, make_dataset, make_manifest

TODAY = dt.date(2026, 10, 6)
PAGES = {"index.html", "index.md"}
GB = cost.GB


def entry(slug, cadence="", **kw):
    src = Source(adapter="file", url=f"https://example.gov.au/{slug}.csv", cadence=cadence)
    return make_dataset([Field("a", "string")], **{"slug": slug, "source": src, **kw})


def stored(root, slug, *versions, size=1000):
    for v in versions:
        d = root / slug / v
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(
            make_manifest(b"x", dataset=slug, version=v, bytes=size).to_json(), "utf-8"
        )


def catalog(**sizes):
    return {
        "dataset": [
            {
                "identifier": slug,
                "versionInfo": "2026-10-01",
                "distribution": [
                    {"format": f, "byteSize": n, "downloadURL": f"/d/{slug}/v/2026-10-01/data.{f}"}
                    for f, n in formats.items()
                ],
            }
            for slug, formats in sizes.items()
        ]
    }


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("weekly", 52),
        ("monthly", 12),
        ("quarterly", 4),
        ("yearly", 1),
        ("annually, in March", 1),
        ("one file a year", 1),
        ("half-yearly", 2),
        ("irregular, several times a year", 4),
        ("hourly, checked weekly", 52),
        ("every 30 minutes", 365),
        ("weekly, with historical records from 1990", 52),
        ("weekly until further notice", 52),
        ("monthly as contracts are disclosed", 12),
        ("updated daily (no new data on weekends)", 365),
        ("closed", 0),
        ("closed year", 0),
        ("historical, not updated since 2019", 0),
        ("monthly until June 2024", 0),
        ("not updated since 2017", 0),
        ("no new readings since April 2025", 0),
    ],
)
def test_a_declared_cadence_names_a_rate(text, n):
    assert cadence.per_year(text, today=TODAY) == n


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("weekly until 2030", 52),
        ("weekly until December 2026", 52),
        ("weekly until October 2026", 52),
        ("weekly until September 2026", 0),
        ("monthly until 2025", 0),
    ],
)
def test_an_end_date_ends_the_rate_only_once_it_has_passed(text, n):
    assert cadence.per_year(text, today=TODAY) == n


def test_the_rate_is_the_larger_of_declared_and_observed_and_capped():
    assert cadence.per_year("when the publisher updates the list") is None
    assert cadence.per_year("", feed=True) == 365
    ds = entry("t", "as required")
    assert cost.versions_per_year(ds, [], TODAY) == (52, "default")
    seen = ["2024-01-01", "2025-11-01", "2026-03-01", "2026-09-30"]
    assert cost.versions_per_year(ds, seen, TODAY) == (3, "observed")
    assert cost.versions_per_year(ds, ["2023-01-01"], TODAY) == (1, "observed")
    # The fetch never reads the cadence, so a closed one cannot hide versions still arriving.
    assert cost.versions_per_year(entry("t", "closed"), seen, TODAY) == (3, "observed")
    assert cost.versions_per_year(entry("t", "monthly"), seen, TODAY) == (12, "cadence")
    # A new entry takes its cadence's rate in full, and a feed is fetched daily whatever it says.
    assert cost.versions_per_year(entry("t", "daily"), [], TODAY)[0] == 365
    feed = entry("t", source=Source(adapter="file", url="u", cadence="weekly", feed=True))
    assert cost.versions_per_year(feed, [], TODAY)[0] == 365
    assert cost.versions_per_year(feed, seen, TODAY) == (365, "cadence")
    assert cost.versions_per_year(replace(ds, status="blocked"), [], TODAY)[0] == 0


def test_a_new_entry_counts_at_least_one_version_a_year():
    # Its first fetch stores a version whatever the cadence says.
    assert cost.versions_per_year(entry("t", "closed"), [], TODAY) == (1, "cadence")
    assert cost.versions_per_year(entry("t", "monthly until June 2024"), [], TODAY)[0] == 1
    assert cost.versions_per_year(entry("t", "weekly until 2030"), [], TODAY) == (52, "cadence")


def test_an_ended_cadence_cannot_zero_a_stored_entry(tmp_path):
    # The fetch still stores a version when the file changes, and a moved source always does.
    for cad in ("closed", "no longer updated", "monthly until June 2024"):
        assert cost.versions_per_year(entry("t", cad), ["2024-01-01"], TODAY) == (1, "observed")
    stored(tmp_path, "old", "2024-01-01", size=1000)
    out = cost.project([entry("old", "closed")], tmp_path, {}, TODAY, frozenset({"old"}),
                       frozenset({"old"}), prober=lambda d: cost.Sized(10 * GB), probing=True)[0]  # fmt: skip
    assert out.per_year == 1 and out.gb_per_year == 140 and out.over_budget


def test_observed_versions_are_not_capped_at_one_a_week():
    # Manual and backfill runs stored six versions of one entry in a week.
    week = [f"2026-09-{d:02d}" for d in range(20, 26)] * 10
    assert cost.versions_per_year(entry("t", "weekly"), week, TODAY) == (60, "observed")


def test_kaggle_frequency_reads_the_same_rate():
    assert [cadence.kaggle_frequency(c) for c in ("daily", "weekly", "monthly", "quarterly")] == [
        "daily",
        "weekly",
        "monthly",
        "quarterly",
    ]
    assert cadence.kaggle_frequency("about twice a year") == "annually"
    assert cadence.kaggle_frequency("closed") == "never"
    assert cadence.kaggle_frequency("as required") == "annually"
    assert cadence.kaggle_frequency("as the police database changes") == "monthly"


# Every cadence in the register, by the Kaggle choice it maps to, so a change to the reader
# cannot change what a hub page promises unnoticed.
KAGGLE = {
    "daily": ["continual", "daily", "every 30 minutes", "live"],
    "weekly": ["hourly, checked weekly", "weekly"],
    "monthly": [
        "as the police database changes",
        "monthly",
        "through the year",
        "weekly in the sampling season",
    ],
    "quarterly": ["quarterly"],
    "annually": [
        "",
        "about twice a year",
        "after each Census",
        "annually",
        "annually, in March",
        "annually, last updated for 2022/23",
        "as buoys are deployed and retired",
        "as counts are added",
        "as localities change",
        "as places are listed",
        "as required",
        "as stations open and close",
        "as the register changes",
        "each school year",
        "half-yearly",
        "irregular",
        "irregular, about every one or two years",
        "irregular, about twice a year",
        "irregular, several times a year",
        "one file a year",
        "several times a year",
        "twice a year",
        "when a site changes",
        "when the ABS releases a new edition",
        "when the council updates the list",
        "when the publisher maps new fires",
        "when the publisher updates the layer",
        "when the publisher updates the list",
        "when the zones change",
        "yearly",
        "yearly, for the next school year",
    ],
    "never": [
        "closed",
        "closed year",
        "historical, not updated",
        "historical, not updated since 2019",
        "monthly until June 2024",
        "no longer updated",
        "no new readings since April 2025",
        "not updated since 2015",
        "not updated since 2017",
        "not updated since 2022",
    ],
}


@pytest.mark.parametrize(("choice", "text"), [(k, t) for k, ts in KAGGLE.items() for t in ts])
def test_each_register_cadence_keeps_its_kaggle_choice(choice, text):
    assert cadence.kaggle_frequency(text) == choice


def test_measured_bytes_count_the_source_once_and_the_partitions():
    files = {"data.parquet": 10, "data.ndjson": 100, "data.geojson": 200}
    ds = entry("t")
    # The raw store holds the publisher's file once, withheld or not.
    assert cost.measured_bytes(ds, files, 5) == 315
    assert cost.measured_bytes(replace(ds, source_withheld="terms"), files, 5) == 315
    # A table too large for data.json still writes its partitions as JSON.
    parted = replace(ds, partition_by=("a", "b"), geometry={"kind": "point"})
    assert cost.measured_bytes(parted, files, 0) == 310 + 2 * 300


def test_the_catalogue_is_summed_by_file_so_a_database_keeps_every_table():
    rec = {
        "identifier": "db",
        "versionInfo": "2026-10-01",
        "distribution": [
            {"format": "duckdb", "byteSize": 200_000_000, "downloadURL": "/d/db/v/2026-10-01/data.duckdb"},
            {"format": "parquet", "byteSize": 7, "downloadURL": "/d/db/v/2026-10-01/tables/a.parquet"},
            {"format": "parquet", "byteSize": 9, "downloadURL": "/d/db/v/2026-10-01/tables/b.parquet"},
            {"format": "parquet", "byteSize": 1, "downloadURL": "/d/db/v/2026-09-01/tables/a.parquet"},
        ],
    }  # fmt: skip
    files = cost.catalogue_sizes({"dataset": [rec]})["db"]
    # DuckDB sizes are published to one significant figure, so the upper bound is counted.
    assert files == {"data.duckdb": 250_000_000, "tables/a.parquet": 7, "tables/b.parquet": 9}


def test_sizes_are_measured_estimated_from_the_source_or_unknown(tmp_path):
    stored(tmp_path, "served", "2026-10-01", size=GB)
    stored(tmp_path, "stored", "2026-10-01", size=GB)
    stored(tmp_path, "moved", "2026-10-01", size=1000)
    sizes = cost.catalogue_sizes(
        catalog(served={"parquet": GB, "csv": GB}, moved={"parquet": 1000})
    )
    ds = [entry(s, "monthly") for s in ("served", "stored", "probed", "new", "moved")]
    sized = {"probed": GB // 10, "moved": GB // 100}

    def prober(d):
        if d.slug not in sized:
            raise cost.Unsized("the socrata adapter's source is not sized ahead of a fetch")
        return cost.Sized(sized[d.slug])

    out = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"probed", "new", "moved"}),
                       frozenset({"moved"}), prober=prober, probing=True)  # fmt: skip
    p = {x.slug: x for x in out}
    assert (p["served"].bytes_per_version, p["served"].basis) == (3 * GB, "measured")
    assert (p["stored"].bytes_per_version, p["stored"].basis) == (14 * GB, "estimate")
    assert (p["probed"].bytes_per_version, p["probed"].basis) == (14 * GB // 10, "estimate")
    # A moved source is sized from the new file when that is larger than the stored one.
    assert (p["moved"].bytes_per_version, p["moved"].basis) == (14 * GB // 100, "estimate")
    assert p["new"].basis == "unknown" and p["new"].over_budget
    assert "not sized ahead of a fetch" in p["new"].note
    assert p["served"].gb_per_year == 36 and p["served"].over_budget


def test_a_ckan_entry_is_probed_at_the_resource_it_resolves_to(monkeypatch):
    from publicdata import fetch

    ds = make_dataset([Field("a", "string")], source=Source(adapter="ckan-resource",
        url="https://example.gov.au/dataset/p", portal="https://example.gov.au", package="p", resource="r1"))  # fmt: skip
    resources = [
        {"id": "r0", "size": 1, "url": "https://x/"},
        {"id": "r1", "size": 4321, "url": "https://y/"},
    ]
    monkeypatch.setattr(fetch, "_package", lambda ds, s, api: {"resources": resources})
    assert cost.probe(ds) == cost.Sized(4321)
    resources[1] = {"id": "r1", "size": "2 MiB", "url": "https://y/"}
    monkeypatch.setattr(
        cost, "_size", lambda url, timeout: (2_251_010 if url == "https://y/" else None, "", url)
    )
    assert cost.probe(ds) == cost.Sized(2_251_010)
    # A portal's size for a zip is its packed size, so the host is asked for the members.
    resources[1] = {"id": "r1", "size": "5000", "url": "https://y/", "format": "ZIP"}
    monkeypatch.setattr(cost, "unpacked_bytes", lambda url, n, kind, timeout: 9 * n)
    assert cost.probe(ds) == cost.Sized(2_251_010, "zip", 9 * 2_251_010)


@pytest.mark.parametrize("cad", ["weekly until 2030", "closed", "Closed"])
def test_a_new_entry_cannot_project_zero_from_its_cadence(tmp_path, cad):
    ds = [entry("copy", cad)]
    out = cost.project(ds, tmp_path, {}, TODAY, frozenset({"copy"}), prober=lambda d: cost.Sized(GB),
                       probing=True)[0]  # fmt: skip
    assert out.per_year >= 1 and out.gb_per_year >= 13 and out.over_budget


def test_a_source_edit_never_sizes_below_the_measured_version(tmp_path):
    stored(tmp_path, "crime", "2026-10-01", size=GB // 10)
    sizes = cost.catalogue_sizes(catalog(crime={"parquet": 2 * GB}))
    ds = [entry("crime", "quarterly")]
    small = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"crime"}), frozenset({"crime"}),
                         prober=lambda d: cost.Sized(GB // 100), probing=True)[0]  # fmt: skip
    assert (small.bytes_per_version, small.basis) == (2 * GB + GB // 10, "measured")
    assert small.over_budget
    big = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"crime"}), frozenset({"crime"}),
                       prober=lambda d: cost.Sized(GB), probing=True)[0]  # fmt: skip
    assert (big.bytes_per_version, big.basis) == (14 * GB, "estimate")

    def lost_host(d):
        raise cost.Unsized("the source could not be read (IncompleteRead)")

    # A moved source the probe cannot size stays unknown and fails closed.
    lost = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"crime"}), frozenset({"crime"}),
                        prober=lost_host, probing=True)[0]  # fmt: skip
    assert lost.basis == "unknown" and lost.over_budget and "IncompleteRead" in lost.note


def test_a_ckan_landing_page_edit_is_not_a_moved_source(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    def write(slug, url, resource="r1", adapter="ckan-resource"):
        (tmp_path / "register" / f"{slug}.yaml").write_text(
            f"source:\n  adapter: {adapter}\n  url: {url}\n  resource: {resource}\n", "utf-8"
        )

    git("init", "-q")
    (tmp_path / "register").mkdir()
    for slug in ("page", "res", "file"):
        write(slug, f"https://x/{slug}", adapter="file" if slug == "file" else "ckan-resource")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "base")
    write("page", "https://x/page/")
    write("res", "https://x/res", resource="r2")
    write("file", "https://x/file/", adapter="file")
    entries = {s: f"register/{s}.yaml" for s in ("page", "res", "file")}
    assert cost.entry_changes(tmp_path, "HEAD", entries)[0] == {"res", "file"}


def test_a_host_that_refuses_head_is_sized_from_a_get_it_does_not_read(monkeypatch):
    class Resp:
        def __init__(self, headers):
            self.headers = headers

        def geturl(self):
            return "https://bucket.example/f.csv?sig=1"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = []

    def urlopen(req, timeout):
        seen.append((req.get_method(), req.full_url, req.get_header("Range")))
        if req.get_method() == "HEAD":
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        if req.get_header("Range") is None:
            return Resp({"Content-Type": "text/csv"})
        return Resp({"Content-Range": "bytes 0-0/987654"})

    monkeypatch.setattr(cost.urllib.request, "urlopen", urlopen)
    url = "https://example.gov.au/f.csv"
    assert cost._size(url, 5) == (987654, "text/csv", "https://bucket.example/f.csv?sig=1")
    # The range goes to the file the redirect reached, never to the redirect itself.
    assert seen[-1] == ("GET", "https://bucket.example/f.csv?sig=1", "bytes=0-0")


def _repo(tmp_path, files):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    for path, text in files.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text, "utf-8")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "base")
    return git


BASE = """source:
  adapter: socrata
  url: https://x/a
  encoding: utf-8
fields:
  - name: a
    source: A
    type: string
    description: one
"""


@pytest.mark.parametrize(
    ("edit", "moved", "reshaped"),
    [
        (BASE.replace("utf-8", "cp1252"), False, False),
        (BASE.replace("description: one", "description: two"), False, False),
        (BASE + "unpivot: a\n", True, True),
        (BASE + "wide:\n  key: a\n", True, True),
        (BASE + "kind: database\n", True, True),
        (BASE.replace("type: string", "type: integer"), True, True),
        (BASE + "partition_by: [a]\n", False, True),
        (BASE.replace("https://x/a", "https://x/b"), True, False),
    ],
)
def test_an_edit_to_what_a_version_publishes_counts_as_moved(tmp_path, edit, moved, reshaped):
    _repo(tmp_path, {"register/a.yaml": BASE})
    (tmp_path / "register" / "a.yaml").write_text(edit, "utf-8")
    fresh, shaped = cost.entry_changes(tmp_path, "HEAD", {"a": "register/a.yaml"})
    assert ("a" in fresh, "a" in shaped) == (moved, reshaped)


@pytest.mark.parametrize(
    "edit, priced",
    [
        (BASE.replace("description: one", "description: two"), False),
        (BASE + "title: Another title\n", False),
        (BASE + "rebuild: 1\n", False),
        (BASE.replace("utf-8", "cp1252"), False),
        (BASE.replace("encoding: utf-8", "encoding: utf-8\n  cadence: daily"), True),
        (BASE.replace("encoding: utf-8", "encoding: utf-8\n  feed: true"), True),
        (BASE + "query: false\n", True),
        (BASE + "status: live\n", True),
        (BASE + "licence:\n  id: CC-BY-4.0\n", True),
        (BASE + "key: [a]\n", True),
        (BASE + "partition_by: [a]\n", True),
        (BASE.replace("https://x/a", "https://x/b"), True),
        (BASE.replace("type: string", "type: integer"), True),
    ],
)
def test_only_an_edit_to_what_an_entry_costs_is_priced(tmp_path, edit, priced):
    _repo(tmp_path, {"register/a.yaml": BASE})
    (tmp_path / "register" / "a.yaml").write_text(edit, "utf-8")
    assert ("a" in cost.costed(tmp_path, "HEAD", {"a": "register/a.yaml"})) is priced
    # An entry with no base copy is priced as a new one.
    assert cost.costed(tmp_path, "HEAD", {"new": "register/a.yaml"}) == {"new"}


def test_the_gate_passes_a_copy_edit_to_an_entry_over_budget(tmp_path, capsys, monkeypatch):
    real = (ROOT / "register" / "qld-fuel-prices.yaml").read_text("utf-8")
    files = {"register/qld-fuel-prices.yaml": real}
    for sub in ("publishers", "licences"):
        for f in (ROOT / "register" / sub).glob("*.yaml"):
            files[f"register/{sub}/{f.name}"] = f.read_text("utf-8")
    git = _repo(tmp_path, files)
    store = tmp_path / "store"
    stored(store, "qld-fuel-prices", *(f"2026-0{m}-01" for m in range(1, 10)), size=GB)
    (tmp_path / "c.json").write_text(json.dumps(catalog()), "utf-8")
    monkeypatch.setattr(cost, "live_rows", lambda slugs, **kw: {})
    args = ["cost", "--root", str(tmp_path), "--base", "HEAD~1", "--store", str(store),
            "--catalog", str(tmp_path / "c.json"), "--today", "2026-10-06",
            "--summary", str(tmp_path / "s.md")]  # fmt: skip

    def commit(text):
        (tmp_path / "register" / "qld-fuel-prices.yaml").write_text(text, "utf-8")
        git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qam", "edit")

    commit(real.replace("title: Fuel price", "title: Fuel prices"))
    assert main(args) == 0
    assert (
        "qld-fuel-prices: edited, but nothing the projection reads changed"
        in capsys.readouterr().out
    )
    commit(real.replace("cadence: monthly", "cadence: weekly"))
    assert main(args) == 1
    assert "OVER BUDGET" in capsys.readouterr().out


def test_an_entry_moved_into_a_folder_is_compared_with_its_base_copy(tmp_path):
    git = _repo(tmp_path, {"register/a.yaml": BASE, "register/b.yaml": BASE})
    (tmp_path / "register" / "qld").mkdir()
    git("mv", "register/a.yaml", "register/qld/a.yaml")
    git("mv", "register/b.yaml", "register/qld/b.yaml")
    (tmp_path / "register/qld/a.yaml").write_text(BASE.replace("x/a", "x/big"), "utf-8")
    entries = {"a": "register/qld/a.yaml", "b": "register/qld/b.yaml"}
    assert cost.entry_changes(tmp_path, "HEAD", entries)[0] == {"a"}
    # An entry with no base copy at all is sized as a new one.
    assert cost.entry_changes(tmp_path, "HEAD", {"c": "register/qld/a.yaml"})[0] == {"c"}


def test_a_new_partition_counts_the_rebuild_of_every_stored_version(tmp_path):
    stored(tmp_path, "crime", *(f"2026-0{m}-01" for m in range(1, 10)), size=0)
    sizes = cost.catalogue_sizes(catalog(crime={"parquet": 10**8, "ndjson": 3 * 10**8}))
    ds = [replace(entry("crime", "yearly"), partition_by=("a",))]
    out = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"crime"}),
                       reshaped={"crime": {}})[0]  # fmt: skip
    # Each of the nine stored versions gains a 0.3 GB partition copy when it is rebuilt.
    assert out.bytes_per_version == 7 * 10**8 and out.per_year == 9
    assert out.rebuild_bytes == 9 * 3 * 10**8 and out.over_budget
    assert out.gb_per_year == pytest.approx(9 * 0.7 + 2.7)
    same = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"crime"}),
                        reshaped={"crime": {"partition_by": ["a"]}})[0]  # fmt: skip
    assert same.rebuild_bytes == 0


def test_a_moved_source_is_found_against_the_base(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "register").mkdir()
    for slug in ("a", "b"):
        (tmp_path / "register" / f"{slug}.yaml").write_text(
            f"source:\n  url: https://x/{slug}\n  cadence: weekly\n", "utf-8"
        )
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "base")
    (tmp_path / "register" / "a.yaml").write_text("source:\n  url: https://x/new\n", "utf-8")
    (tmp_path / "register" / "b.yaml").write_text("source:\n  url: https://x/b\n  cadence: monthly\n", "utf-8")  # fmt: skip
    entries = {"a": "register/a.yaml", "b": "register/b.yaml"}
    assert cost.entry_changes(tmp_path, "HEAD", entries) == ({"a"}, {})


def test_the_gate_fails_a_changed_entry_over_budget(tmp_path):
    stored(tmp_path, "big", "2026-10-01", size=GB)
    stored(tmp_path, "small", "2026-10-01", size=1000)
    ds = [entry("big", "weekly"), entry("small", "weekly")]
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps(catalog()), "utf-8")
    summary = tmp_path / "summary.md"
    run = dict(datasets=ds, store_dir=tmp_path, catalog_src=str(cat), today=TODAY)
    assert cost.run(changed={"big"}, summary=str(summary), rows={}, **run) == 1
    text = summary.read_text("utf-8")
    assert "| `big` | 14.000 (estimate) | 52 (cadence) | 0.000 | 728.000 | 3,640,000,000 | yes |" in text  # fmt: skip
    assert "cost-approved" in text and "Projected growth" in text and "$" not in text
    assert cost.run(changed={"big"}, approved=True, rows={}, **run) == 0
    assert cost.run(changed={"big"}, approve=lambda: (True, "approved by m"), rows={}, **run) == 0
    assert cost.run(changed={"big"}, approve=lambda: (False, "author"), rows={}, **run) == 1
    assert cost.run(changed={"small"}, rows={}, **run) == 0

    def never():
        raise AssertionError("an entry within budget needs no approval")

    assert cost.run(changed={"small"}, approve=never, rows={}, **run) == 0
    # A fetch PR changes no entry, so an entry already over budget cannot block it.
    assert cost.run(changed=set(), rows={}, **run) == 0


def test_only_register_entries_count_as_changed(tmp_path):
    reg = tmp_path / "register"
    (reg / "publishers").mkdir(parents=True)
    (reg / "a.yaml").write_text("{}", "utf-8")
    (reg / "publishers" / "p.yaml").write_text("{}", "utf-8")
    paths = [
        "register/a.yaml",
        "register/publishers/p.yaml",
        "register/gone.yaml",
        "store/a/2026-10-01/manifest.json",
    ]
    assert cost.changed_entries(reg, paths, tmp_path) == {"a": "register/a.yaml"}


def test_the_cli_reads_a_catalogue_file(tmp_path, capsys, monkeypatch):
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps(catalog(**{"qld-road-casualties": {"parquet": 1}})), "utf-8")
    store = ROOT / "pipeline" / "tests" / "fixtures" / "store"
    args = ["cost", "--store", str(store), "--catalog", str(cat), "--today", "2026-10-06"]
    monkeypatch.setattr(cost, "live_rows", lambda slugs, **kw: {})
    assert main([*args, "--summary", str(tmp_path / "s.md")]) == 0
    assert "GB a year projected" in capsys.readouterr().out


def test_an_unreadable_catalogue_fails_a_changed_entry_closed(tmp_path):
    stored(tmp_path, "t", "2026-10-01", size=1000)
    summary = tmp_path / "s.md"
    run = dict(store_dir=tmp_path, catalog_src=str(tmp_path / "missing.json"), today=TODAY)
    assert cost.run([entry("t", "yearly")], changed={"t"}, summary=str(summary), **run) == 1
    assert "could not be read" in summary.read_text("utf-8")
    assert cost.run([entry("t", "yearly")], changed=set(), **run) == 0


def test_an_unknown_slug_is_refused(tmp_path):
    rc = cost.run([entry("t")], tmp_path, str(tmp_path / "c.json"), {"typo"}, TODAY)
    assert rc == 2


def test_the_fleet_counts_storage_cumulatively_and_d1_rows():
    p = cost.Projection("t", 20 * GB, "measured", 1, "cadence", 1, d1_loaded=True, d1_rows=5,
                        d1_indexes=2)  # fmt: skip
    f = cost.fleet([p])
    assert (f.stored_gb, f.gb_per_year, f.d1_rows_per_year) == (20, 20, 15)
    assert set(f.as_json()) == {"stored_bytes", "growth_bytes_per_year", "d1_rows_written_per_year"}


def test_health_carries_the_fleet_projection_from_the_built_files(fixture_site):
    from publicdata.build import _size
    from publicdata.d1 import queryable
    from publicdata.register import load

    health = json.loads((fixture_site / "health.json").read_text("utf-8"))
    s = health["storage"]
    assert not any("usd" in k for k in s)
    stored = 0
    latest = {}
    for vdir in sorted(fixture_site.glob("d/*/v/*")):
        # The version's pages are written by the site after the build counts its files.
        files = [
            p for p in vdir.rglob("*") if p.is_file() and not (p.parent == vdir and p.name in PAGES)
        ]
        assert not any(p.name.startswith("source.") for p in files)
        versions = json.loads((vdir.parent.parent / "versions.json").read_text("utf-8"))
        v = next(x for x in versions["versions"] if x["version"] == vdir.name)
        # No built tree holds the publisher's file; the raw store keeps it once.
        n = sum(_size(p) for p in files) + v["bytes"]
        stored += n
        latest[vdir.parent.parent.name] = (n, v["rows"], (vdir / "data.csv"))
    assert s["stored_bytes"] == stored > 0
    # The fixtures' cadences give each dataset at least one version a year.
    assert s["growth_bytes_per_year"] >= sum(n for n, _, _ in latest.values())
    ds = {d.slug: d for d in load(ROOT / "register")}
    floor = sum(
        r * (1 + cost.d1_indexes(ds[slug]))
        for slug, (_, r, csv) in latest.items()
        if slug in ds and csv.exists() and queryable(ds[slug], csv.stat().st_size)
    )
    assert s["d1_rows_written_per_year"] >= floor > 0


def test_the_cost_module_stays_out_of_the_build_cache_key():
    assert {"cost.py", "cadence.py"}.isdisjoint(p.name for p in cache.code_files())


class Host:
    """Serves one file by HEAD and byte range, as urlopen would, counting requests."""

    def __init__(self, body: bytes, ranges: bool = True, drops: int = 0):
        self.body, self.ranges, self.drops, self.calls = body, ranges, drops, 0

    def __call__(self, req, timeout):
        import http.client

        self.calls += 1
        if self.drops:
            self.drops -= 1
            raise http.client.IncompleteRead(b"", 10)
        n = len(self.body)
        if req.get_method() == "HEAD":
            return _Resp(200, {"Content-Length": str(n), "Content-Type": "application/zip"}, b"")
        rng = req.get_header("Range")
        if not self.ranges or not rng:
            return _Resp(200, {}, self.body)
        a, b = rng.split("=")[1].split("-")
        a, b = int(a), min(int(b), n - 1)
        return _Resp(206, {"Content-Range": f"bytes {a}-{b}/{n}"}, self.body[a : b + 1])


class _Resp:
    def __init__(self, status, headers, body):
        import io

        self.status, self.headers, self._b = status, headers, io.BytesIO(body)

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _zip(members: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(cost, "SLEEP", lambda s: None)


def test_a_zip_is_sized_by_its_members_from_the_central_directory(monkeypatch, no_sleep):
    body = _zip({"a.csv": b"x,y\n" * 200_000, "inner.zip": b"z" * 1000})
    host = Host(body)
    monkeypatch.setattr(cost.urllib.request, "urlopen", host)
    s = cost._sized("https://example.gov.au/f.zip", 5)
    assert s.kind == "zip" and s.bytes == len(body)
    # A zip inside the zip is counted at the multiplier for a source that cannot be unpacked.
    assert s.unpacked == 800_000 + 1000 * cost.COMPRESSED_MULTIPLIER
    # Packed to a few kB, the CSV still projects at its unpacked size.
    assert cost.estimate(entry("t"), s) == s.unpacked * 13 + len(body)


def test_a_host_without_ranges_falls_back_to_the_compressed_multiplier(monkeypatch, no_sleep):
    body = _zip({"a.csv": b"x,y\n" * 1000})
    monkeypatch.setattr(cost.urllib.request, "urlopen", Host(body, ranges=False))
    s = cost._sized("https://example.gov.au/f.zip", 5)
    assert s == cost.Sized(len(body), "zip", None)
    assert cost.estimate(entry("t"), s) == len(body) * (cost.COMPRESSED_MULTIPLIER + 1)


def test_a_gzip_is_sized_from_its_trailer(monkeypatch, no_sleep):
    import gzip

    body = gzip.compress(b"a,b\n" * 100_000)
    monkeypatch.setattr(cost.urllib.request, "urlopen", Host(body))
    assert cost.unpacked_bytes("https://example.gov.au/f.csv.gz", len(body), "gzip", 5) == 400_000
    # Past the trailer's range its size is ambiguous, so it is not read.
    assert cost.unpacked_bytes("u", cost.GZIP_TRAILER_MAX + 1, "gzip", 5) is None


@pytest.mark.parametrize(
    ("name", "ct", "kind"),
    [
        ("https://x/a.zip?download=1", "", "zip"),
        ("https://x/a", "application/x-zip-compressed", "zip"),
        ("https://x/a.csv.gz", "", "gzip"),
        ("https://x/a", "application/gzip", "gzip"),
        ("https://x/a.xlsx", "", "spreadsheet"),
        (
            "https://x/a",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "spreadsheet",
        ),  # fmt: skip
        ("https://x/a.csv", "text/csv", "plain"),
    ],
)
def test_a_source_kind_is_read_from_its_name_or_type(name, ct, kind):
    assert cost.kind_of(name, ct) == kind


def test_a_spatial_entry_or_a_spreadsheet_projects_more_formats():
    geo = entry("t", geometry={"kind": "point"})
    assert cost.estimate(geo, cost.Sized(GB)) == 31 * GB
    assert cost.estimate(entry("t"), cost.Sized(GB, "spreadsheet")) == 91 * GB


def test_a_dropped_connection_is_retried_then_fails_closed(monkeypatch, no_sleep):
    body = b"a,b\n" * 10
    host = Host(body, drops=2)
    monkeypatch.setattr(cost.urllib.request, "urlopen", host)
    assert cost._size("https://example.gov.au/f.csv", 5)[0] == len(body)
    assert host.calls == 3
    monkeypatch.setattr(cost.urllib.request, "urlopen", Host(body, drops=99))
    ds = entry("t")
    with pytest.raises(cost.Unsized, match="IncompleteRead"):
        cost.probe(ds)


def test_an_adapter_that_cannot_be_sized_says_how_to_approve(tmp_path):
    ds = entry("t", "monthly", source=Source(adapter="socrata", url="https://x/a", cadence="monthly"))  # fmt: skip
    with pytest.raises(cost.Unsized, match="socrata adapter"):
        cost.probe(ds)
    summary = tmp_path / "s.md"
    cat = tmp_path / "c.json"
    cat.write_text(json.dumps(catalog()), "utf-8")
    rc = cost.run([ds], tmp_path, str(cat), {"t"}, TODAY, probing=True, fresh={"t"},
                  summary=str(summary), rows={})  # fmt: skip
    text = summary.read_text("utf-8")
    assert rc == 1 and "socrata adapter" in text and "cost-approved" in text


def test_d1_rows_count_each_index_and_every_version(tmp_path):
    stored(tmp_path, "crashes", "2026-09-01", size=1000)
    stored(tmp_path, "big", "2026-09-01", size=1000)
    stored(tmp_path, "files", "2026-09-01", size=1000)
    sizes = cost.catalogue_sizes(
        catalog(crashes={"csv": 10**6}, big={"csv": 600 * 10**6}, files={"csv": 10**6})
    )
    ds = [
        replace(entry("crashes", "monthly"), key=("id", "year"), partition_by=("year", "lga")),
        entry("big", "monthly"),
        replace(entry("files", "monthly"), query=False),
    ]
    rows = {"crashes": 250_000, "big": 10**7, "files": 10**7}
    p = {x.slug: x for x in cost.project(ds, tmp_path, sizes, TODAY, rows=rows)}
    # id, year and lga are indexed once each, so each row is written four times.
    assert p["crashes"].d1_indexes == 3
    assert p["crashes"].d1_rows_per_year == 250_000 * 4 * 12 and p["crashes"].over_d1
    # A data.csv over the loader's limit stays files-only, as does an entry with query off.
    assert p["big"].d1_rows_per_year == 0 == p["files"].d1_rows_per_year
    small = cost.project(ds[:1], tmp_path, sizes, TODAY, rows={"crashes": 1000})[0]
    assert not small.over_budget
    # A new output reloads the newest version once.
    again = cost.project(ds[:1], tmp_path, sizes, TODAY, reshaped={"crashes": {}}, rows=rows)[0]
    assert again.d1_rows_per_year == 250_000 * 4 * 13


def test_a_new_entry_estimates_its_rows_from_its_bytes(tmp_path):
    out = cost.project([entry("new", "twice a year")], tmp_path, {}, TODAY, frozenset({"new"}),
                       prober=lambda d: cost.Sized(10**8), probing=True)[0]  # fmt: skip
    assert out.d1_rows == 14 * 10**8 // cost.PUBLISHED_BYTES_PER_ROW
    assert out.over_d1 and not out.over_storage


def test_live_rows_reads_the_newest_version_and_skips_failures(monkeypatch, no_sleep):
    docs = {
        "a": {
            "latest": "2",
            "versions": [{"version": "1", "rows": 5}, {"version": "2", "rows": 7}],
        },
    }

    def urlopen(req, timeout):
        slug = req.full_url.split("/d/")[1].split("/")[0]
        if slug not in docs:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        return _Resp(200, {}, json.dumps(docs[slug]).encode())

    monkeypatch.setattr(cost.urllib.request, "urlopen", urlopen)
    assert cost.live_rows(["a", "b"]) == {"a": 7}


def _api(labels=("cost-approved",), events=(), perms=None, runs=(), author="alice", head="h2"):
    perms = perms or {"maint": "maintain", "alice": "admin", "reader": "read"}

    def get(path):
        if path.startswith("repos/o/r/pulls/"):
            return {
                "labels": [{"name": n} for n in labels],
                "user": {"login": author},
                "head": {"sha": head, "ref": "feat/x", "repo": {"full_name": "o/r"}},
            }
        if "/issues/" in path:
            return list(events) if "page=1" in path else []
        if "/collaborators/" in path:
            who = path.split("/collaborators/")[1].split("/")[0]
            if who not in perms:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return {"role_name": perms[who], "permission": perms[who]}
        if "/runs" in path:
            return {"workflow_runs": list(runs) if "page=1" in path else []}
        raise AssertionError(path)

    return get


def _run(sha, at, repo="o/r"):
    return {"head_sha": sha, "created_at": at, "head_repository": {"full_name": repo}}


def _label(who, at, event="labeled"):
    return {"event": event, "actor": {"login": who}, "created_at": at, "label": {"name": "cost-approved"}}  # fmt: skip


# Newest first, as the API lists them: h1 was pushed at 10:00 and h2 at 11:00.
RUNS = [_run("h2", "2026-10-07T11:05:00Z"), _run("h2", "2026-10-07T11:00:00Z"),
        _run("h1", "2026-10-07T10:00:00Z")]  # fmt: skip


@pytest.mark.parametrize(
    ("events", "kw", "ok", "why"),
    [
        ([_label("maint", "2026-10-07T11:01:00Z")], {}, True, "approved by maint"),
        ([_label("alice", "2026-10-07T11:01:00Z")], {}, False, "author"),
        ([_label("reader", "2026-10-07T11:01:00Z")], {}, False, "no write access"),
        ([_label("stranger", "2026-10-07T11:01:00Z")], {}, False, "no write access"),
        # Added on h1, before h2 arrived.
        ([_label("maint", "2026-10-07T10:30:00Z")], {}, False, "newest commit"),
        ([_label("maint", "2026-10-07T11:01:00Z")], {"labels": ()}, False, "not on"),
        (
            [
                _label("maint", "2026-10-07T11:01:00Z"),
                _label("bot", "2026-10-07T11:02:00Z", "unlabeled"),
            ],  # fmt: skip
            {},
            False,
            "no labelling",
        ),
        (
            [
                _label("maint", "2026-10-07T11:01:00Z"),
                {"event": "base_ref_changed", "created_at": "2026-10-07T11:03:00Z"},
            ],  # fmt: skip
            {},
            False,
            "change of base",
        ),
        # Another fork's runs on the same branch name do not count as this head's arrival.
        (
            [_label("maint", "2026-10-07T10:30:00Z")],
            {"runs": [_run("h2", "2026-10-07T10:00:00Z", "fork/r"), *RUNS]},
            False,
            "newest commit",
        ),
    ],
)
def test_an_approval_needs_another_writer_after_the_newest_commit(events, kw, ok, why):
    got, reason = cost.approval("o/r", 50, _api(events=events, **{"runs": RUNS, **kw}))
    assert got is ok and why in reason


def test_a_head_pushed_back_counts_from_its_return():
    # h2, then h1, then h2 again at 12:00: a label from 11:30 approved h1, not this h2.
    runs = [_run("h2", "2026-10-07T12:00:00Z"), _run("h1", "2026-10-07T11:10:00Z"), *RUNS]
    events = [_label("maint", "2026-10-07T11:30:00Z")]
    assert cost.approval("o/r", 50, _api(events=events, runs=runs))[0] is False


def test_symlinks_in_the_register_are_refused(tmp_path, capsys):
    git = _repo(tmp_path, {"register/a.yaml": BASE, "elsewhere/b.yaml": BASE})
    (tmp_path / "register" / "b.yaml").symlink_to("../elsewhere/b.yaml")
    assert cost.symlinks(tmp_path) == ["register/b.yaml"]
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-qm", "link")
    assert cost.symlinks(tmp_path) == ["register/b.yaml"]
    args = ["cost", "--root", str(tmp_path), "--base", "HEAD~1", "--catalog", str(tmp_path / "c.json")]  # fmt: skip
    assert main(args) == 2
    assert "symbolic links: register/b.yaml" in capsys.readouterr().out


def test_only_a_web_url_is_probed():
    ds = entry("t", source=Source(adapter="file", url="file:///proc/self/environ"))
    with pytest.raises(cost.Unsized, match="not an http or https URL"):
        cost.probe(ds)
