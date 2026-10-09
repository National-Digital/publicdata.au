import json

from publicdata.structured import check_page, duplicate_names

GOOD = {
    "@context": "https://schema.org",
    "@type": "Dataset",
    "@id": "https://publicdata.au/d/x/",
    "name": "Road crash locations, Queensland",
    "description": "Every reported road crash in Queensland with its location and severity.",
    "url": "https://publicdata.au/d/x/",
    "license": "https://creativecommons.org/licenses/by/4.0/",
    "isAccessibleForFree": True,
    "temporalCoverage": "2001-01-01/2025-06-30",
    "spatialCoverage": {"@type": "Place", "name": "Queensland"},
    "isPartOf": "https://publicdata.au/c/y/",
    "alternateName": None,
    "creator": {"@type": "Organization", "name": "Agency", "url": "https://agency.gov.au/"},
    "publisher": {"@type": "Organization", "name": "Agency", "url": "https://agency.gov.au/"},
    "distribution": [
        {
            "@type": "DataDownload",
            "encodingFormat": "text/csv",
            "contentUrl": "https://publicdata.au/d/x/v/1/data.csv",
        }
    ],
}


def _page(*blocks: object) -> str:
    return "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks)


def _errors(**change: object) -> list[str]:
    node: dict[str, object] = {**GOOD, **change}
    node = {k: v for k, v in node.items() if v != "DROP"}
    return check_page(_page(node), "p")


def test_valid_dataset_passes() -> None:
    assert _errors() == []


def test_google_dataset_rules() -> None:
    assert any("description must be 50" in e for e in _errors(description="Too short."))
    assert any("needs a license" in e for e in _errors(license="DROP"))
    assert any("needs a creator" in e for e in _errors(creator="DROP"))
    gov = {"@type": "GovernmentOrganization", "name": "Agency"}
    assert any("exactly Person or Organization" in e for e in _errors(creator=gov))
    assert any("exactly Person or Organization" in e for e in _errors(publisher=gov))
    stub = {"@type": "Dataset", "name": "Stub", "url": "https://publicdata.au/c/y/"}
    errs = _errors(isPartOf=stub)
    assert any("isPartOf: Dataset description" in e for e in errs)
    assert any("must be a URL or a full Dataset" in e for e in _errors(hasPart=[{"@id": "x"}]))
    assert any("ISO 8601" in e for e in _errors(temporalCoverage="2001 to 2025"))
    bad_dl = [{"@type": "DataDownload", "encodingFormat": "text/csv"}]
    assert any("contentUrl" in e for e in _errors(distribution=bad_dl))


def test_schema_org_vocabulary() -> None:
    assert any("unknown property" in e for e in _errors(licence="https://x.org/"))
    assert any("unknown type Datset" in e for e in _errors(**{"@type": "Datset"}))
    place = {"@type": "Place", "name": "Queensland", "author": "Someone"}
    assert any("not a property of Place" in e for e in _errors(spatialCoverage=place))
    assert any("outside the range" in e for e in _errors(creator={"@type": "Place"}))
    assert any("outside the range" in e for e in _errors(isAccessibleForFree="yes"))
    assert any("no @type" in e for e in _errors(spatialCoverage={"name": "Queensland"}))


def test_breadcrumbs_and_faq() -> None:
    crumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home"},
            {"@type": "ListItem", "position": 3, "name": "Here"},
        ],
    }
    errs = check_page(_page(crumbs), "p")
    assert any("needs an item URL" in e for e in errs)
    assert any("position must be 2" in e for e in errs)
    faq = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": "Why?", "acceptedAnswer": {"text": "x"}}],
    }
    assert any("acceptedAnswer" in e for e in check_page(_page(faq), "p"))


def test_catalog_entries_must_be_full_datasets() -> None:
    catalog = {
        "@context": "https://schema.org",
        "@type": "DataCatalog",
        "name": "publicdata.au",
        "dataset": [{"@id": "https://publicdata.au/d/x/"}, "https://publicdata.au/d/y/"],
    }
    errs = check_page(_page(catalog), "p")
    assert sum("not a reference" in e for e in errs) == 2
    assert _errors(includedInDataCatalog={k: v for k, v in catalog.items() if k != "dataset"}) == []


def test_context_and_json() -> None:
    assert any("@context" in e for e in _errors(**{"@context": "http://schema.org"}))
    bad = '<script type="application/ld+json">{nope</script>'
    assert any("invalid JSON" in e for e in check_page(bad, "p"))


def test_dataset_names_are_unique_across_pages() -> None:
    a = _page(GOOD)
    b = _page({**GOOD, "@id": "https://publicdata.au/d/z/"})
    assert duplicate_names([("a", a), ("a-again", a)]) == []
    assert duplicate_names([("a", a), ("b", b)]) == [
        "b: Dataset name 'Road crash locations, Queensland' also used by https://publicdata.au/d/x/"
    ]
