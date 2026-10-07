from publicdata import abbreviations as ab

KNOWN = ab.known_tokens({"CC BY": "Creative Commons Attribution", "CSV": "comma-separated values"})


def test_a_glossary_phrase_covers_each_of_its_words():
    assert {"CC", "BY", "CSV"} <= set(KNOWN)
    assert ab.unknown("Published under CC BY 4.0 as a CSV.", KNOWN) == []


def test_an_unexplained_abbreviation_is_reported_once():
    assert ab.unknown("The ABS and the ABS again, and the ATO.", KNOWN) == ["ABS", "ATO"]


def test_an_inline_expansion_and_a_publishers_name_pass():
    assert ab.unknown("Department of Transport and Main Roads (TMR) releases it.", KNOWN) == []
    assert ab.unknown("Released by DETSI each year.", KNOWN, names=("DETSI",)) == []


def test_codes_dates_single_letters_and_quoted_identifiers_are_not_abbreviations():
    assert ab.unknown(
        "Version 2026-04-24, column FILRSBVRT, grade A, SA2, PM10 and G-NAF.", KNOWN
    ) == ["G-NAF"]
    assert ab.unknown("The publisher's `PRSEC` table, and the ABS.", KNOWN) == ["ABS"]


def test_prose_reads_main_and_skips_code_tables_captions_and_data_regions():
    page = """<html><head><title>ABS</title></head><body><header>HDR</header><main>
    <p>Prose with <code>CODE</code> and <span class="mono">MONO</span>, from the <span class="pub">PUB</span>.</p>
    <table><tr><td>CELL</td></tr></table>
    <figure><figcaption>CAPTION</figcaption></figure>
    <div data-conformance="aa"><p>DENSE</p></div>
    <p>Second &amp; last: TAIL</p>
    </main><footer>FOOT</footer></body></html>"""
    text = ab.prose(page)
    assert "Prose with" in text and "TAIL" in text and "&" in text
    for hidden in ("HDR", "CODE", "MONO", "PUB", "CELL", "CAPTION", "DENSE", "FOOT", "ABS"):
        assert hidden not in text


def test_the_committed_glossary_is_sorted_and_renders():
    entries = ab.glossary()
    assert list(entries) == sorted(entries)
    html = ab.render(entries)
    assert html.startswith('<dl class="glossary">') and "<dt>CSV</dt>" in html


def test_check_page_names_the_page_and_the_words():
    bad = ab.check_page("<main><p>The XYZQ file.</p></main>", "about/index.html")
    assert bad == [
        "about/index.html: abbreviation not in the glossary or expanded on the page: XYZQ"
    ]
    assert ab.check_page("<main><p>The XYZQ (expanded here) file.</p></main>", "x") == [
        "x: abbreviation not in the glossary or expanded on the page: XYZQ"
    ]
    assert ab.check_page("<main><p>The Xyz Query (XYZQ) file.</p></main>", "x") == []
