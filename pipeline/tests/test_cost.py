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
    # Only the daily feed run makes more than one version a week.
    assert cost.versions_per_year(entry("t", "daily"), [], TODAY)[0] == 52
    feed = entry("t", source=Source(adapter="file", url="u", cadence="daily", feed=True))
    assert cost.versions_per_year(feed, [], TODAY)[0] == 365
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
                       frozenset({"old"}), prober=lambda d: 10 * GB, probing=True)[0]  # fmt: skip
    assert out.per_year == 1 and out.gb_per_year == 130 and out.over_budget


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


def test_measured_bytes_count_the_source_twice_and_the_partitions():
    files = {"data.parquet": 10, "data.ndjson": 100, "data.geojson": 200}
    ds = entry("t")
    assert cost.measured_bytes(ds, files, 5) == 310 + 5 * cost.SOURCE_COPIES
    assert cost.measured_bytes(replace(ds, source_withheld="terms"), files, 5) == 310
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
    out = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"probed", "new", "moved"}),
                       frozenset({"moved"}), prober=lambda d: sized.get(d.slug), probing=True)  # fmt: skip
    p = {x.slug: x for x in out}
    assert (p["served"].bytes_per_version, p["served"].basis) == (4 * GB, "measured")
    assert (p["stored"].bytes_per_version, p["stored"].basis) == (13 * GB, "estimate")
    assert (p["probed"].bytes_per_version, p["probed"].basis) == (13 * GB // 10, "estimate")
    # A moved source is sized from the new file when that is larger than the stored one.
    assert (p["moved"].bytes_per_version, p["moved"].basis) == (13 * GB // 100, "estimate")
    assert p["new"].basis == "unknown" and p["new"].over_budget
    assert p["served"].gb_per_year == 48 and p["served"].over_budget


def test_a_ckan_entry_is_probed_at_the_resource_it_resolves_to(monkeypatch):
    from publicdata import fetch

    ds = make_dataset([Field("a", "string")], source=Source(adapter="ckan-resource",
        url="https://example.gov.au/dataset/p", portal="https://example.gov.au", package="p", resource="r1"))  # fmt: skip
    resources = [{"id": "r0", "size": 1, "url": "x"}, {"id": "r1", "size": 4321, "url": "y"}]
    monkeypatch.setattr(fetch, "_package", lambda ds, s, api: {"resources": resources})
    assert cost.probe(ds) == 4321
    resources[1] = {"id": "r1", "size": "2 MiB", "url": "y"}
    monkeypatch.setattr(cost, "_size", lambda url, timeout: 2_251_010 if url == "y" else None)
    assert cost.probe(ds) == 2_251_010


@pytest.mark.parametrize("cad", ["weekly until 2030", "closed", "Closed"])
def test_a_new_entry_cannot_project_zero_from_its_cadence(tmp_path, cad):
    ds = [entry("copy", cad)]
    out = cost.project(ds, tmp_path, {}, TODAY, frozenset({"copy"}), prober=lambda d: GB,
                       probing=True)[0]  # fmt: skip
    assert out.per_year >= 1 and out.gb_per_year >= 13 and out.over_budget


def test_a_source_edit_never_sizes_below_the_measured_version(tmp_path):
    stored(tmp_path, "crime", "2026-10-01", size=GB // 10)
    sizes = cost.catalogue_sizes(catalog(crime={"parquet": 2 * GB}))
    ds = [entry("crime", "quarterly")]
    small = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"crime"}), frozenset({"crime"}),
                         prober=lambda d: GB // 100, probing=True)[0]  # fmt: skip
    assert (small.bytes_per_version, small.basis) == (2 * GB + 2 * GB // 10, "measured")
    assert small.over_budget
    big = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"crime"}), frozenset({"crime"}),
                       prober=lambda d: GB, probing=True)[0]  # fmt: skip
    assert (big.bytes_per_version, big.basis) == (13 * GB, "estimate")
    # A moved source the probe cannot size stays unknown and fails closed.
    lost = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"crime"}), frozenset({"crime"}),
                        prober=lambda d: None, probing=True)[0]  # fmt: skip
    assert lost.basis == "unknown" and lost.over_budget


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


def test_a_host_that_refuses_head_is_sized_from_a_one_byte_get(monkeypatch):
    class Resp:
        def __init__(self, headers):
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout):
        if req.get_method() == "HEAD":
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        assert req.get_header("Range") == "bytes=0-0"
        return Resp({"Content-Range": "bytes 0-0/987654"})

    monkeypatch.setattr(cost.urllib.request, "urlopen", urlopen)
    assert cost._size("https://example.gov.au/f.csv", 5) == 987654


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
    assert cost.run(changed={"big"}, summary=str(summary), **run) == 1
    text = summary.read_text("utf-8")
    assert "| `big` | 13.000 (estimate) | 52 (cadence) | 0.000 | 676.000 | yes |" in text
    assert "cost-approved" in text and "Projected growth" in text
    assert cost.run(changed={"big"}, approved=True, **run) == 0
    assert cost.run(changed={"small"}, **run) == 0
    # A fetch PR changes no entry, so an entry already over budget cannot block it.
    assert cost.run(changed=set(), **run) == 0


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


def test_the_cli_reads_a_catalogue_file(tmp_path, capsys):
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps(catalog(**{"qld-road-casualties": {"parquet": 1}})), "utf-8")
    store = ROOT / "pipeline" / "tests" / "fixtures" / "store"
    args = ["cost", "--store", str(store), "--catalog", str(cat), "--today", "2026-10-06"]
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


def test_the_fleet_cost_counts_storage_cumulatively():
    p = cost.Projection("t", 20 * GB, "measured", 1, "cadence", 1)
    f = cost.fleet([p])
    assert (f.stored_gb, f.gb_per_year) == (20, 20)
    assert f.usd_per_month_now == pytest.approx(0.15)
    assert f.usd_per_month_in_a_year == pytest.approx(0.45)


def test_health_carries_the_fleet_projection_from_the_built_files(fixture_site):
    from publicdata.build import _size

    health = json.loads((fixture_site / "health.json").read_text("utf-8"))
    s = health["storage"]
    stored = newest = 0
    latest = {}
    for vdir in sorted(fixture_site.glob("d/*/v/*")):
        # The version's pages are written by the site after the build counts its files.
        files = [
            p for p in vdir.rglob("*") if p.is_file() and not (p.parent == vdir and p.name in PAGES)
        ]
        n = sum(_size(p) for p in files)
        src = next(vdir.glob("source.*"), None)
        n += src.stat().st_size * (cost.SOURCE_COPIES - 1) if src else 0
        stored += n
        latest[vdir.parent.parent.name] = n
    newest = sum(latest.values())
    assert s["stored_bytes"] == stored > 0
    # The fixtures' cadences give each dataset at least one version a year.
    assert s["growth_bytes_per_year"] >= newest
    assert s["r2_usd_per_month"] == round(cost.r2_usd_per_month(stored / GB), 2)


def test_the_cost_module_stays_out_of_the_build_cache_key():
    assert {"cost.py", "cadence.py"}.isdisjoint(p.name for p in cache.code_files())
