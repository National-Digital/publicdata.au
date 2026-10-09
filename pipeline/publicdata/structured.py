"""JSON-LD checks: schema.org vocabulary conformance, then Google's rich-result rules.

The vocabulary is a compacted copy of a pinned schema.org release. To move release:
python -m publicdata.structured <schemaorg-current-https.jsonld> <version>
"""

from __future__ import annotations

import json
import re
import sys
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, TypeGuard, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .jsontypes import JSON, JSONObject

VOCAB = Path(__file__).parent / "schemaorg.json"
LD_BLOCK = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
ISO_DATE = r"\d{4}(-\d{2}(-\d{2}(T[\d:.]+(Z|[+-]\d{2}:?\d{2})?)?)?)?"
ISO_INTERVAL = re.compile(rf"^({ISO_DATE}|\.\.)(/({ISO_DATE}|\.\.))?$")
AGENT = {"Person", "Organization"}


class Vocab(TypedDict):
    version: str
    types: dict[str, list[str]]
    # Each property's domain and range.
    properties: dict[str, list[list[str]]]


def compact(src: Path, version: str) -> Vocab:
    graph = json.loads(src.read_text(encoding="utf-8"))["@graph"]

    def ids(v: list[dict[str, str]] | dict[str, str] | None) -> list[str]:
        v = v if isinstance(v, list) else [v] if v else []
        return sorted(x["@id"].removeprefix("schema:") for x in v)

    types: dict[str, list[str]] = {}
    props: dict[str, list[list[str]]] = {}
    for n in graph:
        kind = n["@type"] if isinstance(n["@type"], list) else [n["@type"]]
        name = n["@id"].removeprefix("schema:")
        if "rdf:Property" in kind:
            props[name] = [ids(n.get("schema:domainIncludes")), ids(n.get("schema:rangeIncludes"))]
        elif "rdfs:Class" in kind or "schema:DataType" in kind:
            types[name] = ids(n.get("rdfs:subClassOf"))
    types.setdefault("DataType", [])
    for dt in ("Boolean", "Date", "DateTime", "Number", "Text", "Time"):
        types[dt] = sorted(set(types.get(dt, [])) | {"DataType"})
    return {"version": version, "types": types, "properties": props}


@cache
def _vocab() -> Vocab:
    vocab: Vocab = json.loads(VOCAB.read_text(encoding="utf-8"))
    return vocab


@cache
def _ancestors(t: str) -> frozenset[str]:
    seen: set[str] = set()
    todo = [t]
    while todo:
        x = todo.pop()
        if x not in seen:
            seen.add(x)
            todo.extend(_vocab()["types"].get(x, []))
    return frozenset(seen)


def _types(node: Mapping[str, JSON]) -> list[str]:
    t = node.get("@type", [])
    # A node's @type is a name or a list of names.
    return cast("list[str]", t if isinstance(t, list) else [t])


def _values(v: JSON) -> list[JSON]:
    return [x for x in (v if isinstance(v, list) else [v]) if x is not None]


def _is(node: object, *names: str) -> TypeGuard[JSONObject]:
    return isinstance(node, dict) and bool(set(_types(node)) & set(names))


def _literal_ok(value: object, rng: set[str]) -> bool:
    """Whether a literal fits a range.

    It does if the range admits that datatype, or the literal is a URL standing in for an entity.
    """
    kinds = set().union(*(_ancestors(r) for r in rng))
    if isinstance(value, bool):
        return "Boolean" in kinds
    if isinstance(value, (int, float)):
        return "Number" in kinds or "Text" in kinds
    if isinstance(value, str):
        return bool(kinds & {"Text", "Date", "DateTime", "Time"}) or value.startswith("https://")
    return False


def _vocab_errors(node: object, where: str) -> list[str]:  # noqa: C901, PLR0912 - one check per vocabulary rule
    errors: list[str] = []
    if isinstance(node, list):
        for i, v in enumerate(node):
            errors += _vocab_errors(v, f"{where}[{i}]")
        return errors
    if not isinstance(node, dict):
        return errors
    types, props = _vocab()["types"], _vocab()["properties"]
    node_types = _types(node)
    for t in node_types:
        if t not in types:
            errors.append(f"{where}: unknown type {t}")
    if not node_types and set(node) - {"@id", "@context"}:
        errors.append(f"{where}: node has properties but no @type")
    lineage = set().union(*(_ancestors(t) for t in node_types)) if node_types else set()
    for key, value in node.items():
        if key.startswith("@"):
            continue
        at = f"{where}.{key}"
        if key not in props:
            errors.append(f"{at}: unknown property")
            continue
        domain, rng = props[key]
        if lineage and not lineage & set(domain):
            errors.append(f"{at}: not a property of {'/'.join(node_types)}")
        for v in _values(value):
            if isinstance(v, dict):
                vt = _types(v)
                if vt and not set().union(*(_ancestors(t) for t in vt)) & set(rng):
                    errors.append(f"{at}: {'/'.join(vt)} is outside the range {rng}")
            elif not _literal_ok(v, set(rng)):
                errors.append(f"{at}: {type(v).__name__} value is outside the range {rng}")
        errors += _vocab_errors(value, at)
    return errors


def _text(v: object) -> TypeGuard[str]:
    return isinstance(v, str) and v.strip() != ""


def _url(v: object) -> TypeGuard[str]:
    return isinstance(v, str) and v.startswith("https://")


# Google's Dataset rules: a description of 50 to 5,000 characters.
DESCRIPTION_MIN, DESCRIPTION_MAX = 50, 5000


def _dataset(n: JSONObject, at: str) -> list[str]:  # noqa: C901, PLR0912 - one check per Dataset property
    e: list[str] = []
    if not _text(n.get("name")):
        e.append(f"{at}: Dataset needs a name")
    desc = n.get("description")
    if not _text(desc) or not DESCRIPTION_MIN <= len(desc) <= DESCRIPTION_MAX:
        e.append(f"{at}: Dataset description must be 50 to 5000 characters")
    for role in ("creator", "publisher", "funder"):
        for v in _values(n.get(role)):
            if not (isinstance(v, dict) and len(_types(v)) == 1 and _types(v)[0] in AGENT):
                e.append(f"{at}.{role}: must be exactly Person or Organization")
            elif not _text(v.get("name")):
                e.append(f"{at}.{role}: needs a name")
    if not _values(n.get("creator")):
        e.append(f"{at}: Dataset needs a creator")
    lic = _values(n.get("license"))
    if not lic:
        e.append(f"{at}: Dataset needs a license")
    e.extend(
        f"{at}.license: must be a URL or CreativeWork"
        for v in lic
        if not (_url(v) or _is(v, "CreativeWork"))
    )
    for key in ("hasPart", "isPartOf"):
        e.extend(
            f"{at}.{key}: must be a URL or a full Dataset"
            for v in _values(n.get(key))
            if not (_url(v) or _is(v, "Dataset"))
        )
    for key in ("url", "sameAs"):
        e.extend(f"{at}.{key}: must be a URL" for v in _values(n.get(key)) if not _url(v))
    e.extend(
        f"{at}.isAccessibleForFree: must be a boolean"
        for v in _values(n.get("isAccessibleForFree"))
        if not isinstance(v, bool)
    )
    e.extend(
        f"{at}.temporalCoverage: {v!r} is not an ISO 8601 date or interval"
        for v in _values(n.get("temporalCoverage"))
        if not (isinstance(v, str) and ISO_INTERVAL.match(v))
    )
    e.extend(
        f"{at}.spatialCoverage: must be text or a named Place"
        for v in _values(n.get("spatialCoverage"))
        if not (_text(v) or (_is(v, "Place") and _text(v.get("name"))))
    )
    for i, v in enumerate(_values(n.get("distribution"))):
        if not _is(v, "DataDownload") or not _url(v.get("contentUrl")):
            e.append(f"{at}.distribution[{i}]: must be a DataDownload with a contentUrl")
        elif not _text(v.get("encodingFormat")):
            e.append(f"{at}.distribution[{i}]: needs an encodingFormat")
    e.extend(
        f"{at}.includedInDataCatalog: must be a DataCatalog"
        for v in _values(n.get("includedInDataCatalog"))
        if not _is(v, "DataCatalog")
    )
    return e


def _breadcrumbs(n: JSONObject, at: str) -> list[str]:
    items = _values(n.get("itemListElement"))
    if not items:
        return [f"{at}: BreadcrumbList needs itemListElement"]
    e: list[str] = []
    for i, item in enumerate(items):
        where = f"{at}.itemListElement[{i}]"
        if not _is(item, "ListItem"):
            e.append(f"{where}: must be a ListItem")
            continue
        if item.get("position") != i + 1:
            e.append(f"{where}: position must be {i + 1}")
        if not _text(item.get("name")):
            e.append(f"{where}: needs a name")
        target = item.get("item")
        if i < len(items) - 1 and not (
            _url(target) or (isinstance(target, dict) and _url(target.get("@id")))
        ):
            e.append(f"{where}: needs an item URL")
    return e


def _faq(n: JSONObject, at: str) -> list[str]:
    qs = _values(n.get("mainEntity"))
    if not qs:
        return [f"{at}: FAQPage needs mainEntity"]
    e: list[str] = []
    for i, q in enumerate(qs):
        where = f"{at}.mainEntity[{i}]"
        if not _is(q, "Question") or not _text(q.get("name")):
            e.append(f"{where}: must be a Question with a name")
            continue
        a = q.get("acceptedAnswer")
        if not _is(a, "Answer") or not _text(a.get("text")):
            e.append(f"{where}: needs an acceptedAnswer with text")
    return e


def _catalog(n: JSONObject, at: str) -> list[str]:
    """Errors for the DataCatalog's dataset entries that are only references.

    Google reads every entry in dataset as a Dataset item on this page, so a bare reference is an
    invalid item.
    """
    return [
        f"{at}.dataset[{i}]: must be a full Dataset, not a reference"
        for i, v in enumerate(_values(n.get("dataset")))
        if not _is(v, "Dataset")
    ]


RULES: dict[str, Callable[[JSONObject, str], list[str]]] = {
    "Dataset": _dataset,
    "DataCatalog": _catalog,
    "BreadcrumbList": _breadcrumbs,
    "FAQPage": _faq,
}


def _google_errors(node: object, where: str) -> list[str]:
    errors: list[str] = []
    if isinstance(node, list):
        for i, v in enumerate(node):
            errors += _google_errors(v, f"{where}[{i}]")
    elif isinstance(node, dict):
        for t in _types(node):
            if t in RULES:
                errors += RULES[t](node, where)
        for key, value in node.items():
            if not key.startswith("@"):
                errors += _google_errors(value, f"{where}.{key}")
    return errors


def blocks(html: str) -> list[JSON]:
    return [json.loads(b) for b in LD_BLOCK.findall(html)]


def check_page(html: str, page: str) -> list[str]:
    errors: list[str] = []
    for i, raw in enumerate(LD_BLOCK.findall(html)):
        at = f"{page} ld[{i}]"
        try:
            block = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"{at}: invalid JSON: {exc}")
            continue
        if block.get("@context") != "https://schema.org":
            errors.append(f"{at}: @context must be https://schema.org")
        errors += _vocab_errors(block, at) + _google_errors(block, at)
    return errors


def duplicate_names(pages: list[tuple[str, str]]) -> list[str]:
    """Google asks for a distinct name per distinct Dataset."""
    owner: dict[JSON, JSON] = {}
    errors: list[str] = []
    for page, html in pages:
        for b in blocks(html):
            if not (isinstance(b, dict) and _is(b, "Dataset") and b.get("@id") and b.get("name")):
                continue
            first = owner.setdefault(b["name"], b["@id"])
            if first != b["@id"]:
                errors.append(f"{page}: Dataset name {b.get('name')!r} also used by {first}")
    return errors


if __name__ == "__main__":
    out = compact(Path(sys.argv[1]), sys.argv[2])
    VOCAB.write_text(json.dumps(out, sort_keys=True, separators=(",", ":")) + "\n")
