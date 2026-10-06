import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from publicdata.register import Dataset, Licence, Publisher, Source
from publicdata.store import Manifest

ROOT = Path(__file__).resolve().parents[2]

# The pre-push hook runs `pytest -m "not slow"`. A test is slow when it uses one of SLOW_FIXTURES
# or is named in SLOW_TESTS (relative to tests/, without parameters).
SLOW_FIXTURES = {"fixture_site", "site_copy", "fixture_builds", "site", "fixture_store"}
SLOW_TESTS = {
    "test_cache.py::test_a_code_change_prunes_the_old_entries_before_it_builds",
    "test_cache.py::test_a_new_version_diffs_against_the_cached_one_without_its_source",
    "test_cache.py::test_a_register_change_rebuilds_and_prune_drops_the_old_entry",
    "test_cache.py::test_a_warm_build_needs_no_source_bytes_and_matches_less_the_published_formats",
    "test_cache.py::test_cached_files_are_read_only",
    "test_d1.py::test_a_version_too_large_for_d1_is_files_only_everywhere",
    "test_d1.py::test_openapi_lists_the_query_api_only_once_it_is_switched_on",
    "test_diff.py::test_the_arrow_diff_matches_the_per_row_reference_on_random_versions",
    "test_directory.py::test_a_live_entry_marks_the_record_its_source_url_names_as_served",
    "test_directory.py::test_a_planned_register_entry_takes_the_votes_of_its_catalogue_record",
    "test_fetch.py::test_ala_reads_each_provider_in_slices_and_dates_the_version_by_the_newest_load",
    "test_fetch.py::test_ala_splits_one_place_and_second_by_year_and_refuses_what_it_cannot_read",
    "test_fetch.py::test_new_versions_are_grouped_by_government_for_their_own_pull_requests",
    "test_fetch.py::test_one_failing_dataset_does_not_stop_the_others",
    "test_hooks.py::test_pre_push_from_a_worktree_hides_the_repository_from_the_tests",
    "test_hooks.py::test_pre_push_passes_when_the_fast_tests_pass_and_leaves_slow_ones_out",
    "test_hooks.py::test_pre_push_stops_a_failing_fast_test_and_names_it",
    "test_hubs.py::test_hubs_command_fails_when_a_hub_fails",
    "test_hubs.py::test_hubs_render_command_writes_every_hubs_files",
    "test_hubs.py::test_the_hubs_command_writes_the_record_as_it_goes",
    "test_register.py::test_label_drafts_reuse_the_register_then_read_the_name",
    "test_register.py::test_real_register_loads",
    "test_site_and_gate.py::test_a_withheld_source_is_left_out_listed_and_not_expected",
    "test_site_and_gate.py::test_bibtex_protects_the_institutional_author_and_the_title",
    "test_site_and_gate.py::test_catalog_modified_moves_with_the_newest_release",
    "test_site_and_gate.py::test_home_links_to_the_dataset_when_its_table_has_no_explorer",
    "test_site_and_gate.py::test_labels_are_words_unique_and_never_a_field_name",
    "test_spine.py::test_a_partitioned_polygon_layer_writes_json_partitions_without_point_geojson",
    "test_spine.py::test_a_publishers_datum_is_moved_to_gda2020_for_the_join_only",
    "test_spine.py::test_each_point_takes_the_area_it_falls_in_and_blanks_stay_null",
    "test_xlsx_source.py::test_a_file_replaced_inside_one_resource_is_dated_by_the_resource_metadata",
    "test_xlsx_source.py::test_a_later_file_date_than_the_portal_dates_the_version",
    "test_xlsx_source.py::test_a_presentation_table_reads_one_row_per_group_of_cells",
    "test_xlsx_source.py::test_a_wide_table_of_dated_columns_unpivots_to_one_row_per_cell",
}


def pytest_collection_modifyitems(items):
    for item in items:
        name = item.nodeid.split("[")[0].removeprefix("tests/")
        if SLOW_FIXTURES & set(item.fixturenames) or name in SLOW_TESTS:
            item.add_marker(pytest.mark.slow)


@pytest.fixture
def register_dir() -> Path:
    return ROOT / "register"


@pytest.fixture
def fixture_store() -> Path:
    return Path(__file__).parent / "fixtures" / "store"


@pytest.fixture(scope="session")
def fixture_site(tmp_path_factory) -> Path:
    """One `build --fixtures` per session, read only; a test that edits it takes site_copy."""
    out = tmp_path_factory.mktemp("fixture-site") / "dist"
    subprocess.run(
        [sys.executable, "-m", "publicdata", "build", "--fixtures", "--out", str(out)], check=True
    )
    return out


@pytest.fixture
def site_copy(fixture_site, tmp_path) -> Path:
    return Path(shutil.copytree(fixture_site, tmp_path / "dist"))


@pytest.fixture(scope="session")
def fixture_builds(tmp_path_factory, fixture_site):
    """The plain build (the shared fixture site) and a cold cached build of the same store."""
    from publicdata.__main__ import main

    root = tmp_path_factory.mktemp("fixture-builds")
    store = Path(__file__).parent / "fixtures" / "store"
    plain, cold, cache = fixture_site, root / "cold", root / "cache"
    assert main(["build", "--store", str(store), "--out", str(cold), "--cache", str(cache), "--absent", str(root / "cold.json")]) == 0  # fmt: skip
    return plain, cold, cache


def make_dataset(fields, **kw) -> Dataset:
    base = dict(
        slug="t",
        title="Test",
        status="live",
        publisher=Publisher("Test Agency", "Test", "Qld", "https://example.gov.au/"),
        licence=Licence(
            "CC-BY-4.0",
            "https://example.gov.au/data",
            "Test Agency, Test, sourced {sourced}. CC BY 4.0.",
        ),
        source=Source(adapter="ckan-resource", url="https://example.gov.au/data"),
        fields=tuple(fields),
    )
    base.update(kw)
    return Dataset(**base)


def make_manifest(data: bytes, **kw) -> Manifest:
    import hashlib

    base = dict(
        dataset="t",
        version="2026-01-02",
        as_at="2025-12-31",
        fetched_at="2026-01-02T01:00:00+00:00",
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        filename="source.csv",
        encoding="utf-8",
        source={"url": "https://example.gov.au/file.csv"},
        licence={"id": "CC-BY-4.0", "title": "Creative Commons Attribution 4.0"},
    )
    base.update(kw)
    return Manifest(**base)


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))
