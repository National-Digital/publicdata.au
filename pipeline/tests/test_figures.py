import re
import sqlite3
from types import SimpleNamespace

from publicdata import figures

from .conftest import as_parquet, make_dataset


def _db(tmp_path, rows):
    db = tmp_path / "data.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE records (crash_year INTEGER, severity TEXT, lon REAL, lat REAL)")
    con.executemany("INSERT INTO records VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return as_parquet(db)


def _ds():
    return make_dataset(
        [
            figures.__class__  # placeholder replaced below
        ]
        if False
        else [
            SimpleNamespace(name="crash_year", type="integer", display="Year"),
            SimpleNamespace(name="severity", type="string", display="Severity"),
            SimpleNamespace(name="lon", type="number", display="Longitude"),
            SimpleNamespace(name="lat", type="number", display="Latitude"),
        ],
        geometry={"lon": "lon", "lat": "lat", "crs": "EPSG:7844"},
        row_label="Crashes",
    )


def test_a_part_year_is_left_out_and_named(tmp_path):
    db = _db(
        tmp_path,
        [
            (2023, "Fatal", 153.0, -27.5),
            (2024, "Fatal", 153.0, -27.5),
            (2024, "Minor", 153.1, -27.5),
            (2025, "Fatal", 153.0, -27.5),
        ],
    )
    s = figures.series(db, "crash_year", "integer", "severity", "count", "2025-06-30")
    assert s["years"] == [2023, 2024]
    assert s["partial"] == [2025]
    assert s["values"][2024] == {"Fatal": 1, "Minor": 1}
    # A file that runs to the last day of the year keeps that year.
    assert figures.series(db, "crash_year", "integer", None, "count", "2025-12-31")["years"] == [
        2023,
        2024,
        2025,
    ]
    m = SimpleNamespace(as_at="2025-06-30", fetched_at="2026-01-01T00:00:00+00:00")
    console = {
        "fields": [{"name": "severity", "type": "string", "values": ["Fatal", "Minor", "Serious"]}],
        "example": {"metric": "count", "group": [], "filters": []},
    }
    fig = figures.dataset_figures(_ds(), m, console, db, tmp_path)
    assert (
        fig["chart_caption"]
        == "Crashes per year by severity, 2023 to 2024. 2025 is not drawn because the file runs to 30 June 2025."
    )
    assert "<svg" in fig["chart"]
    assert 'fill="var(--s1)"' in fig["chart"]
    assert "Fatal" in fig["chart"]
    assert fig["years"] == "2023 to 2024"
    assert "<svg" in fig["spark"]


def test_a_financial_year_is_drawn_and_named_as_the_publisher_writes_it(tmp_path):
    db = tmp_path / "data.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE records (financial_year TEXT, grp TEXT, amount REAL)")
    con.executemany(
        "INSERT INTO records VALUES (?, ?, ?)",
        [
            ("2017-18", "Metallic", 4.0),
            ("2017-18", "Energy", 1.0),
            ("2018-19", "Metallic", 5.0),
            ("2025-26", "Metallic", 2.0),
            ("Total", "Metallic", 99.0),
        ],
    )
    con.commit()
    con.close()
    db = as_parquet(db)
    s = figures.series(db, "financial_year", "financial", "grp", "sum.amount", "2026-03-31")
    # 2025-26 runs to June 2026, after the cut-off; a value that is no financial year is no year.
    assert s["years"] == [2017, 2018] and s["partial"] == [2025]
    assert s["values"][2017] == {"Energy": 1.0, "Metallic": 4.0}
    assert figures.year_span(s) == "2017-18 to 2018-19"
    assert ">2018-19</text>" in figures.stacked_svg(s, "Amount")
    ds = make_dataset(
        [
            SimpleNamespace(name="financial_year", type="string", display="Financial year"),
            SimpleNamespace(name="grp", type="string", display="Group"),
            SimpleNamespace(name="amount", type="number", display="Amount"),
        ],
        chart={
            "where": (),
            "split": "grp",
            "metric": "sum.amount",
            "label": "Amount",
            "year": "financial_year",
        },
    )
    assert figures.year_field(ds) == ("financial_year", "financial")
    m = SimpleNamespace(as_at="2026-03-31", fetched_at="2026-04-01T00:00:00+00:00")
    fig = figures.dataset_figures(ds, m, None, db, tmp_path)
    assert fig["chart_caption"].startswith(
        "Amount per financial year by group, 2017-18 to 2018-19."
    )
    assert "2025-26 is not drawn" in fig["chart_caption"]


def test_a_financial_year_is_read_however_the_publisher_writes_it():
    starts = {
        "2018-19": 2018,
        "2011\u201312": 2011,
        "2008/09": 2008,
        "1931/1932": 1931,
        "2011-2012": 2011,
        "Detail Data 2008 - 2009": 2008,
        "FY201213": 2012,
        "FY24-25": 2024,
        # Australia names a financial year by the year it ends.
        "FY2010": 2009,
        "1999-00": 1999,
    }
    for text, year in starts.items():
        assert figures.financial_start(text) == year, text
    for text in ("2015", "Total", "2011-13", "2010-2012", "FY24-26", "2019-20 to 2020-21", ""):
        assert figures.financial_start(text) is None, text


def test_the_year_comes_from_an_integer_year_field_before_a_date():
    ds = make_dataset(
        [
            SimpleNamespace(name="reporting_year", type="integer"),
            SimpleNamespace(name="year_of_crash", type="integer"),
            SimpleNamespace(name="date", type="date"),
        ]
    )
    assert figures.year_field(ds) == ("year_of_crash", "integer")
    assert figures.year_field(make_dataset([SimpleNamespace(name="date", type="date")])) == (
        "date",
        "date",
    )
    assert figures.year_field(make_dataset([SimpleNamespace(name="n", type="integer")])) is None


def test_cells_count_rows_and_the_national_map_hatches_states_without_data(tmp_path):
    db = _db(
        tmp_path,
        [
            (2024, "Fatal", 153.02, -27.47),
            (2024, "Fatal", 153.02, -27.47),
            (2024, "Fatal", 999.0, -27.5),
        ],
    )
    c = figures.cells(db, "lon", "lat")
    assert list(c.values()) == [2]  # the row outside Australia is not counted
    assert list(figures.cells(db, "lon", "lat", ("severity", "=", "Fatal")).values()) == [2]
    assert figures.cells(db, "lon", "lat", ("severity", "=", "Minor")) == {}
    svg = figures.national_map(
        [("Queensland", c)], "Crashes", "no published crash locations", tmp_path
    )
    assert 'class="nodata"' in svg
    assert "Western Australia" in svg
    assert "no published crash locations" in svg
    assert svg.count('class="nodata"') == len(figures.STATES) - 1
    # The hero is the data alone: no outlines, no city names, a gap still hatched and named.
    # The raster is a file named by its bytes, in an img whose alt is worked out from the cells.
    assert "data:" not in svg
    assert "Brisbane" not in svg
    assert 'class="state"' not in svg
    src = re.search(r'src="(/maps/[0-9a-f]{16}\.png)"', svg).group(1)
    assert (tmp_path / src.lstrip("/")).stat().st_size > 0
    assert 'alt="2 crashes drawn in 1 cells of 0.05 degrees, the fullest with 2. ' in svg
    assert "Brisbane" in figures.map_html(c, "Crashes", tmp_path, box=(0, 50, 941, 661))
    # A state whose rows all miss the drawing's condition is covered, so it is not hatched.
    both = figures.national_map(
        [("Queensland", c)], "Crashes", "none", tmp_path, covered={"Victoria"}
    )
    assert both.count('class="nodata"') == len(figures.STATES) - 2
    assert set(figures.GAP_LABEL) == set(figures.STATES)
    own = figures.map_html(c, "Crashes", tmp_path)
    assert 'class="nodata"' not in own
    assert "<svg" in own


def test_the_chart_condition_keeps_to_the_rows_it_names_and_says_so(tmp_path):
    db = _db(
        tmp_path,
        [
            (2023, "Fatal", 153.0, -27.5),
            (2023, "Property damage only", 153.0, -27.5),
            (2024, "Fatal", 153.0, -27.5),
            (2024, "Property damage only", 153.1, -27.5),
        ],
    )
    where = {"field": "severity", "op": "!=", "value": "Property damage only"}
    s = figures.series(db, "crash_year", "integer", "severity", "count", "2024-12-31", where)
    assert s["values"] == {2023: {"Fatal": 1}, 2024: {"Fatal": 1}}
    assert s["categories"] == ["Fatal"]
    ds = _ds()
    ds = ds.__class__(**{**ds.__dict__, "chart": {"where": (where,), "split": None}})
    assert figures.where_words(ds) == (
        " Rows where severity is not Property damage only are drawn."
    )
    m = SimpleNamespace(as_at="2024-12-31", fetched_at="2025-01-01T00:00:00+00:00")
    fig = figures.dataset_figures(ds, m, None, db, tmp_path)
    assert "is not Property damage only" in fig["chart_caption"]
    assert figures.where_words(_ds()) == ""


def _counts(tmp_path):
    db = tmp_path / "counts.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE records (crash_year INTEGER, severity TEXT, drink INTEGER, killed INTEGER, rate REAL)"
    )
    con.executemany(
        "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
        [
            (2023, "Fatal", 1, 3, 2.0),
            (2023, "Injury", 0, 0, 4.0),
            (2024, "Fatal", 0, 2, 6.0),
            (2024, "Injury", 1, 0, 9.0),
        ],
    )
    con.commit()
    con.close()
    return as_parquet(db)


def _counts_ds(**kw):
    return make_dataset(
        [
            SimpleNamespace(name="crash_year", type="integer", display="Year"),
            SimpleNamespace(name="severity", type="string", display="Severity"),
            SimpleNamespace(name="drink", type="boolean", display="Drink driving involved"),
            SimpleNamespace(name="killed", type="integer", display="Fatalities"),
            SimpleNamespace(name="rate", type="number", display="Rate per 100,000"),
        ],
        row_label="Crashes",
        **kw,
    )


CONSOLE_FIELDS = [
    {"name": "crash_year", "type": "integer", "values": [2023, 2024]},
    {"name": "severity", "type": "string", "values": ["Fatal", "Injury"]},
    {"name": "drink", "type": "boolean"},
    {"name": "killed", "type": "integer"},
    {"name": "rate", "type": "number"},
]


def test_the_example_answers_with_every_operator_and_a_boolean(tmp_path):
    db = _counts(tmp_path)
    console = {
        "fields": CONSOLE_FIELDS,
        "example": {
            "filters": [
                {"field": "drink", "op": "eq", "value": "true"},
                {"field": "crash_year", "op": "gte", "value": "2023"},
            ],
            "group": ["severity"],
            "metric": "sum.killed",
        },
    }
    assert figures.example_rows(db, console) == [("Fatal", 3), ("Injury", 0)]
    console["example"].update(
        filters=[{"field": "severity", "op": "neq", "value": "Injury"}], metric="avg.rate"
    )
    assert figures.example_rows(db, console) == [("Fatal", 4)]


def test_a_measure_reads_as_its_label_and_the_caption_names_how_it_is_worked_out():
    ds = _counts_ds()
    assert figures.measure(ds, "count") == "Crashes"
    assert figures.measure(ds, "sum.killed") == "Fatalities"
    assert figures.measure(ds, "avg.rate") == "Average rate per 100,000"
    assert figures.measure(ds, "avg.rate", "Deaths per 100,000") == "Deaths per 100,000"
    assert figures.basis(ds, "count") == "a count of rows"
    assert figures.basis(ds, "sum.killed") == "a sum of the fatalities column"
    assert figures.basis(ds, "max.rate") == "the highest of the rate per 100,000 column"


def test_the_chart_draws_the_register_measure_and_an_average_as_one_series(tmp_path):
    db = _counts(tmp_path)
    m = SimpleNamespace(as_at="2024-12-31", fetched_at="2025-01-01T00:00:00+00:00")
    console = {
        "fields": CONSOLE_FIELDS,
        "example": {"filters": [], "group": ["severity"], "metric": "sum.killed"},
    }
    # The example's measure carries to the chart, split by the short list of severities.
    fig = figures.dataset_figures(_counts_ds(), m, console, db, tmp_path)
    assert fig["chart_caption"].startswith("Fatalities per year by severity, 2023 to 2024.")
    assert fig["basis"] == "a sum of the fatalities column"
    assert fig["series"]["values"] == {
        2023: {"Fatal": 3, "Injury": 0},
        2024: {"Fatal": 2, "Injury": 0},
    }
    chart = {"where": (), "split": None, "metric": "avg.rate", "label": ""}
    fig = figures.dataset_figures(_counts_ds(chart=chart), m, console, db, tmp_path)
    assert fig["chart_caption"].startswith("Average rate per 100,000 per year, 2023 to 2024.")
    assert fig["series"]["values"] == {2023: {"": 3}, 2024: {"": 7.5}}
    chart = {
        "where": ({"field": "severity", "op": "=", "value": "Fatal"},),
        "split": "",
        "metric": "",
        "label": "Deaths",
    }
    fig = figures.dataset_figures(_counts_ds(chart=chart), m, console, db, tmp_path)
    assert fig["chart_caption"].startswith(
        "Deaths per year, 2023 to 2024. Rows where severity is Fatal are drawn."
    )
    assert fig["series"]["values"] == {2023: {"": 3}, 2024: {"": 2}}


def test_a_version_without_a_column_the_chart_names_draws_no_chart(tmp_path):
    db = _counts(tmp_path)
    m = SimpleNamespace(as_at="2024-12-31", fetched_at="2025-01-01T00:00:00+00:00")
    console = {
        "fields": CONSOLE_FIELDS,
        "example": {"filters": [], "group": ["severity"], "metric": "count"},
    }
    chart = {"where": (), "split": "", "metric": "avg.added_later", "label": ""}
    ds = _counts_ds(chart=chart)
    ds = ds.__class__(
        **{
            **ds.__dict__,
            "fields": (
                *ds.fields,
                SimpleNamespace(name="added_later", type="number", display="Added later"),
            ),
        }
    )
    assert figures.dataset_figures(ds, m, console, db, tmp_path)["chart"] == ""


def test_a_figure_keeps_its_fraction_and_an_average_is_never_added_up(tmp_path):
    assert [figures.fmt(n) for n in (4.0, 1750.0, 4.35, 0.5, 19.25, 2112.6)] == [
        "4",
        "1,750",
        "4.35",
        "0.5",
        "19.2",
        "2,113",
    ]
    assert ">4.35<" in figures.hbars_svg([("Lending", 4.35), ("Deposit", 1.2)], "Rates")
    db = _counts(tmp_path)
    m = SimpleNamespace(as_at="2024-12-31", fetched_at="2025-01-01T00:00:00+00:00")
    console = {
        "fields": CONSOLE_FIELDS,
        "example": {"filters": [], "group": ["severity"], "metric": "sum.killed"},
    }
    chart = {"where": (), "split": None, "metric": "avg.rate", "label": ""}
    fig = figures.dataset_figures(_counts_ds(chart=chart), m, console, db, tmp_path)
    assert "in all" not in fig["chart"]
    assert "over 2 years" not in fig["spark"]
    assert "the highest 7.5 in 2024, the latest year." in fig["chart"]
    assert "2 years, 2023 to 2024, the highest 7.5 in 2024, the latest year." in fig["spark"]
    fig = figures.dataset_figures(_counts_ds(), m, console, db, tmp_path)
    assert ", 5 in all, the highest 3 in 2023." in fig["chart"]


def test_the_sample_shows_the_newest_rows_with_each_partition_value_in_turn(tmp_path):
    from publicdata.site import _sample

    db = _counts(tmp_path)
    ds = _counts_ds(partition_by=("severity",))
    s = _sample(ds, db)
    assert [r[:2] for r in s["rows"]] == [
        ["2024", "Fatal"],
        ["2024", "Injury"],
        ["2023", "Fatal"],
        ["2023", "Injury"],
    ]
    assert s["heading"] == "A sample of 4 rows"
    assert s["note"].startswith(
        "From the latest version, newest first by year, each severity in turn, then in the"
    )
    # A place page keeps to its place, so the place field takes no turns.
    within = {"field": "severity", "op": "=", "value": "Injury"}
    s = _sample(_counts_ds(partition_by=("severity",), place_field="severity"), db, within)
    assert [r[:2] for r in s["rows"]] == [["2024", "Injury"], ["2023", "Injury"]]
    sample = {
        "where": ({"field": "crash_year", "op": "=", "value": "newest"},),
        "order": (("rate", True),),
        "spread": "",
        "label": "The newest year's crashes, highest rate first.",
    }
    s = _sample(_counts_ds(partition_by=("severity",), sample=sample), db)
    assert [r[4] for r in s["rows"]] == ["9", "6"]
    assert s["note"] == (
        "The newest year's crashes, highest rate first. A blank cell is shown as null."
    )


def test_a_table_with_no_year_or_partition_shows_its_first_rows(tmp_path):
    from publicdata.site import _sample

    db = _counts(tmp_path)
    ds = make_dataset([SimpleNamespace(name="severity", type="string", display="Severity")])
    s = _sample(ds, db)
    assert s["heading"] == "The first 4 rows"
    assert s["rows"] == [["Fatal"], ["Injury"], ["Fatal"], ["Injury"]]


def test_related_datasets_share_subject_words_and_the_list_is_capped():
    from publicdata.register import Publisher
    from publicdata.site import RELATED_MAX, _related

    def out(slug, title, publisher, rows=1, topics=("water",)):
        ds = make_dataset(
            [],
            slug=slug,
            title=title,
            topics=topics,
            publisher=Publisher(publisher, publisher, "Vic", "https://example.gov.au/"),
        )
        return SimpleNamespace(dataset=ds, latest=SimpleNamespace(rows=rows))

    mine = out("dams", "Daily water storage levels, Australian dams", "Bureau")
    live = [
        mine,
        out("vic-storages", "Monthly water storage levels, Victoria", "DEECA"),
        out("melb", "Daily storage volume of Melbourne Water's dams", "Melbourne Water"),
        out("crocs", "CrocWatch crocodile sightings, Queensland", "DES"),
        out("own", "Water storage stations", "Bureau"),
        out("other-topic", "Dam safety storage incidents", "X", topics=("safety",)),
    ]
    assert {r["slug"] for r in _related(mine.dataset, live)} == {"melb", "vic-storages"}
    many = [mine, *(out(f"s{i}", f"Water storage {i}", f"P{i}") for i in range(30))]
    assert len(_related(mine.dataset, many)) == RELATED_MAX
