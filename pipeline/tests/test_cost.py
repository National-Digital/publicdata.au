import datetime as dt
import json
from dataclasses import replace

import pytest

from publicdata import cache, cost
from publicdata.__main__ import main
from publicdata.register import Field, Source

from .conftest import ROOT, make_dataset, make_manifest

TODAY = dt.date(2026, 10, 6)
GB = cost.GB


def entry(slug, cadence="", **kw):
    src = Source(adapter="file", url=f"https://example.gov.au/{slug}.csv", cadence=cadence)
    return make_dataset([Field("a", "string")], slug=slug, source=src, **kw)


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
        ("closed", 0),
        ("monthly until June 2024", 0),
        ("not updated since 2017", 0),
    ],
)
def test_a_declared_cadence_sets_versions_a_year(text, n):
    assert cost.per_year_from_cadence(text) == n


def test_a_cadence_with_no_rate_falls_to_observed_then_default(tmp_path):
    assert cost.per_year_from_cadence("when the publisher updates the list") is None
    assert cost.per_year_from_cadence("", feed=True) == 365
    ds = entry("t", "as required")
    assert cost.versions_per_year(ds, [], TODAY) == (52, "default")
    seen = ["2024-01-01", "2025-11-01", "2026-03-01", "2026-09-30"]
    assert cost.versions_per_year(ds, seen, TODAY) == (3, "observed")
    assert cost.versions_per_year(ds, ["2023-01-01"], TODAY) == (1, "observed")
    assert cost.versions_per_year(replace(ds, status="blocked"), [], TODAY)[0] == 0


def test_measured_bytes_count_the_source_and_the_partitions():
    formats = {"parquet": 10, "json": 100, "geojson": 200}
    ds = entry("t")
    assert cost.measured_bytes(ds, formats, 5) == 315
    assert cost.measured_bytes(replace(ds, source_withheld="terms"), formats, 5) == 310
    parted = replace(ds, partition_by=("a",), geometry={"kind": "point"})
    assert cost.measured_bytes(parted, formats, 5) == 315 + 300


def test_sizes_are_measured_estimated_from_the_source_or_unknown(tmp_path):
    stored(tmp_path, "served", "2026-10-01", size=GB)
    stored(tmp_path, "stored", "2026-10-01", size=GB)
    sizes = cost.catalogue_sizes(catalog(served={"parquet": GB, "csv": 2 * GB}))
    ds = [entry(s, "monthly") for s in ("served", "stored", "probed", "new")]
    out = cost.project(ds, tmp_path, sizes, TODAY, frozenset({"probed", "new"}),
                       prober=lambda url: GB // 10 if "probed" in url else None)  # fmt: skip
    p = {x.slug: x for x in out}
    assert (p["served"].bytes_per_version, p["served"].basis) == (4 * GB, "measured")
    assert (p["stored"].bytes_per_version, p["stored"].basis) == (13 * GB, "estimate")
    assert (p["probed"].bytes_per_version, p["probed"].basis) == (13 * GB // 10, "estimate")
    assert p["new"].basis == "unknown" and p["new"].over_budget
    assert p["served"].gb_per_year == 48 and p["served"].over_budget


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
    assert cost.changed_slugs(reg, paths, tmp_path) == {"a"}


def test_the_cli_reads_a_catalogue_file(tmp_path, capsys):
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps(catalog(**{"qld-road-casualties": {"parquet": 1}})), "utf-8")
    store = ROOT / "pipeline" / "tests" / "fixtures" / "store"
    args = ["cost", "--store", str(store), "--catalog", str(cat), "--today", "2026-10-06"]
    assert main([*args, "--summary", str(tmp_path / "s.md")]) == 0
    assert "GB a year projected" in capsys.readouterr().out


def test_an_unreadable_catalogue_falls_back_to_estimates(tmp_path):
    stored(tmp_path, "t", "2026-10-01", size=1000)
    summary = tmp_path / "s.md"
    rc = cost.run([entry("t", "yearly")], tmp_path, str(tmp_path / "missing.json"), {"t"},
                  TODAY, summary=str(summary))  # fmt: skip
    assert rc == 0 and "could not be read" in summary.read_text("utf-8")


def test_the_fleet_cost_counts_storage_cumulatively():
    p = cost.Projection("t", 20 * GB, "measured", 1, "cadence", 1)
    f = cost.fleet([p])
    assert (f.stored_gb, f.gb_per_year) == (20, 20)
    assert f.usd_per_month_now == pytest.approx(0.15)
    assert f.usd_per_month_in_a_year == pytest.approx(0.45)


def test_health_carries_the_fleet_projection(fixture_site):
    s = json.loads((fixture_site / "health.json").read_text("utf-8"))["storage"]
    assert set(s) == {
        "stored_gb",
        "growth_gb_per_year",
        "r2_usd_per_month",
        "r2_usd_per_month_in_a_year",
    }
    assert s["stored_gb"] >= 0


def test_the_cost_module_stays_out_of_the_build_cache_key():
    assert "cost.py" not in {p.name for p in cache.code_files()}
