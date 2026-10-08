import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from publicdata.__main__ import main
from publicdata.register import Dataset, Field, Licence, Publisher, Source
from publicdata.store import Manifest

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import TypedDict, Unpack

    from publicdata.jsontypes import JSON, JSONObject
    from publicdata.register import (
        Chart,
        Database,
        Example,
        Geometry,
        Kind,
        Sample,
        Status,
        TableSpec,
        View,
        Wide,
    )
    from publicdata.serialise.profile import Layout
    from publicdata.store import ManifestLicence, ManifestSource

    class DatasetFields(TypedDict, total=False):
        """The Dataset fields a test may set; the rest keep make_dataset's values."""

        slug: str
        title: str
        status: Status
        publisher: Publisher
        licence: Licence
        source: Source
        description: str
        summary: str
        collection: str
        collection_title: str
        key: tuple[str, ...]
        partition_by: tuple[str, ...]
        sort: tuple[str, ...]
        lookup: tuple[str, ...]
        int32: tuple[str, ...]
        unpivot: str
        wide: Wide | None
        geometry: Geometry | None
        enrich: tuple[str, ...]
        suppression: tuple[str, ...]
        note: str
        blocked_reason: str
        planned: str
        temporal_start: str
        order: int
        search_title: str
        also_known_as: tuple[str, ...]
        keywords: tuple[str, ...]
        faq: tuple[tuple[str, str], ...]
        collection_description: str
        landing: str
        row_label: str
        topics: tuple[str, ...]
        example: Example | None
        chart: Chart | None
        sample: Sample | None
        omit: dict[str, str]
        source_withheld: str
        collection_search_title: str
        place_field: str
        rebuild: int
        query: bool
        path: str
        extra: dict[str, object]
        kind: Kind
        database: Database | None
        tables: tuple[TableSpec, ...]
        views: tuple[View, ...]

    class ManifestFields(TypedDict, total=False):
        """The Manifest fields a test may set; the rest keep make_manifest's values."""

        dataset: str
        version: str
        as_at: str
        fetched_at: str
        sha256: str
        bytes: int
        filename: str
        encoding: str
        source: ManifestSource
        licence: ManifestLicence
        backfilled: bool
        tombstone: JSONObject | None
        notes: list[str]
        rows_sha256: str
        parquet: Layout
        caps: int


ROOT = Path(__file__).resolve().parents[2]

# The pre-push hook runs `pytest -m "not slow"`. A test is slow when it uses one of SLOW_FIXTURES
# or is named in SLOW_TESTS (relative to tests/, without parameters).
SLOW_FIXTURES = {"fixture_site", "site_copy", "fixture_builds", "site", "fixture_store"}
SLOW_TESTS = {
    "test_cache.py::test_a_new_version_diffs_against_the_cached_one_without_its_source",
    "test_cache.py::test_a_rebuild_prunes_the_old_entries_before_it_builds",
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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        name = item.nodeid.split("[")[0].removeprefix("tests/")
        fixtures = set(item.fixturenames)  # type: ignore[attr-defined]  # every item here is a pytest.Function
        if SLOW_FIXTURES & fixtures or name in SLOW_TESTS:
            item.add_marker(pytest.mark.slow)


@pytest.fixture
def register_dir() -> Path:
    return ROOT / "register"


@pytest.fixture
def fixture_store() -> Path:
    return Path(__file__).parent / "fixtures" / "store"


@pytest.fixture(scope="session")
def fixture_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One `build --fixtures` per session, read only; a test that edits it takes site_copy."""
    out = tmp_path_factory.mktemp("fixture-site") / "dist"
    subprocess.run(
        [sys.executable, "-m", "publicdata", "build", "--fixtures", "--out", str(out)], check=True
    )
    return out


@pytest.fixture
def site_copy(fixture_site: Path, tmp_path: Path) -> Path:
    return Path(shutil.copytree(fixture_site, tmp_path / "dist"))


@pytest.fixture(scope="session")
def fixture_builds(
    tmp_path_factory: pytest.TempPathFactory, fixture_site: Path
) -> tuple[Path, Path, Path]:
    """The plain build (the shared fixture site) and a cold cached build of the same store."""
    root = tmp_path_factory.mktemp("fixture-builds")
    store = Path(__file__).parent / "fixtures" / "store"
    plain, cold, cache = fixture_site, root / "cold", root / "cache"
    assert main(["build", "--store", str(store), "--out", str(cold), "--cache", str(cache), "--absent", str(root / "cold.json")]) == 0  # fmt: skip
    return plain, cold, cache


def make_dataset(fields: Iterable[Field], **kw: Unpack[DatasetFields]) -> Dataset:
    base = Dataset(
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
    return replace(base, **kw)


def make_manifest(data: bytes, **kw: Unpack[ManifestFields]) -> Manifest:
    base = Manifest(
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
    return replace(base, **kw)


def present[T](x: T | None) -> T:
    """x, which the test expects to be there."""
    assert x is not None
    return x


def obj(v: JSON) -> JSONObject:
    """v, which the test expects to be a JSON object."""
    assert isinstance(v, dict)
    return v


def arr(v: JSON) -> list[JSON]:
    """v, which the test expects to be a JSON array."""
    assert isinstance(v, list)
    return v


def dig(v: JSON, *path: str | int) -> JSON:
    """The value at path in parsed JSON, each step an object key or an array index."""
    for step in path:
        v = obj(v)[step] if isinstance(step, str) else arr(v)[step]
    return v


def read_json(p: Path) -> JSONObject:
    """A JSON file the build writes, each of which holds one object."""
    got: JSONObject = json.loads(p.read_text(encoding="utf-8"))
    return got


def as_parquet(db: Path) -> Path:
    """A hand-made data.sqlite's records table as the data.parquet beside it.

    It is typed from its declared columns, which is what the build reads now.
    """
    types = {"INTEGER": pa.int64(), "REAL": pa.float64(), "TEXT": pa.string()}
    con = sqlite3.connect(db)
    cols = con.execute("PRAGMA table_info(records)").fetchall()
    rows = con.execute("SELECT * FROM records").fetchall()
    con.close()
    out = db.with_name("data.parquet")
    pq.write_table(
        pa.table({c[1]: pa.array([r[i] for r in rows], types[c[2]]) for i, c in enumerate(cols)}),
        out,
    )
    return out
