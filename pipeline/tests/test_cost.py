import datetime as dt
import json
import subprocess
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
    assert cadence.per_year(text) == n


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
    stored(tmp_path, "moved", "2026-10-01", size=GB)
    sizes = cost.catalogue_sizes(
        catalog(served={"parquet": GB, "csv": GB}, moved={"parquet": 50 * GB})
    )
    ds = [entry(s, "monthly") for s in ("served", "stored", "probed", "new", "moved")]
    sized = {"probed": GB // 10, "moved": GB // 100}
    out = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"probed", "new", "moved"}),
                       frozenset({"moved"}), prober=lambda d: sized.get(d.slug), probing=True)  # fmt: skip
    p = {x.slug: x for x in out}
    assert (p["served"].bytes_per_version, p["served"].basis) == (4 * GB, "measured")
    assert (p["stored"].bytes_per_version, p["stored"].basis) == (13 * GB, "estimate")
    assert (p["probed"].bytes_per_version, p["probed"].basis) == (13 * GB // 10, "estimate")
    # A moved source is sized from the new file, not the stored one.
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
    assert cost.source_moved(tmp_path, "HEAD", entries) == {"a"}


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
    assert "| `big` | 13.000 (estimate) | 52 (cadence) | 676.000 | yes |" in text
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
