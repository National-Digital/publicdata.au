"""Abbreviations in the site's own prose must be in the glossary (WCAG 2.2 SC 3.1.4).

The glossary is glossary.json, rendered on the glossary page; the gate reads each built page's
prose and the register's headline copy is checked at validate time, so an unexplained abbreviation
stops a pull request before it stops a deploy.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .register import Dataset

GLOSSARY = Path(__file__).with_name("glossary.json")
# The register copy a page shows as prose. A publisher's code quoted in it goes in backticks.
COPY_FIELDS = (
    "title",
    "summary",
    "description",
    "search_title",
    "collection_title",
    "collection_description",
    "source_withheld",
)

# Two to seven capitals or digits led by a capital, with hyphenated forms such as G-NAF and SHA-256.
# Longer runs are a publisher's codes, not words a reader is expected to know.
# A token followed by a dot and letters is a file name (BRAND.md, README.md), not an abbreviation.
TOKEN = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]{0,6}(?:-[A-Z0-9]{1,6})*)(?![\w-]|\.\w)")
# Elements whose text is not the site's prose: code, data cells, charts, and dense data regions.
SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "pre",
        "code",
        "kbd",
        "svg",
        "table",
        "figcaption",
        "title",
        "noscript",
        "template",
        "textarea",
    }
)
# aka: the search aliases a dataset is also called; method: an HTTP verb in the query console;
# attr: the attribution wording the licence asks for, quoted as the publisher wrote it.
SKIP_CLASSES = frozenset({"mono", "pub", "pname", "chip", "aka", "method", "attr"})
# A publisher's value set in the site's own sentence, such as a place, a category or a chart series.
QUOTED = "data-quoted"


@lru_cache(maxsize=1)
def glossary(path: Path = GLOSSARY) -> dict[str, str]:
    data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    if list(data) != sorted(data):
        msg = "glossary.json is kept in alphabetical order"
        raise ValueError(msg)
    return data


def known_tokens(entries: dict[str, str]) -> frozenset[str]:
    """Every token a glossary entry covers.

    That is the entry itself and, for a phrase such as CC BY, each word of it.
    """
    out: set[str] = set()
    for term in entries:
        out.add(term)
        out.update(TOKEN.findall(term))
    return frozenset(out)


def unknown(text: str, known: frozenset[str], names: tuple[str, ...] = ()) -> list[str]:
    """The abbreviations in text that the glossary does not hold and the text does not expand inline.

    "Transport and Main Roads (TMR)" expands TMR inline. names are proper names to read past.
    """
    for n in sorted(names, key=len, reverse=True):
        if n and n in text:
            # Whole words only: a short name such as GA must not split GUNGAHLIN into two tokens.
            text = re.sub(rf"(?<![\w-]){re.escape(n)}(?![\w-])", " ", text)
    text = re.sub(r"`[^`\n]*`", " ", text)  # a code or identifier quoted in register copy
    expanded = set(re.findall(r"\(([A-Z][A-Z0-9-]+)\)", text))
    seen: list[str] = []
    for tok in TOKEN.findall(text):
        # A token with a digit is a code, a version or a formula (TRLB04, SA2, PM10), not a word a
        # reader is owed an expansion of; the glossary still explains the ones in common use.
        if sum(ch.isalpha() for ch in tok) < 2 or any(ch.isdigit() for ch in tok):  # noqa: PLR2004 - one letter is no word
            continue
        if tok in known or tok in expanded or tok in seen:
            continue
        seen.append(tok)
    return seen


class _Prose(HTMLParser):
    """The text of <main>, less what is not the site's own prose.

    That leaves out the elements SKIP_TAGS and SKIP_CLASSES name, every element marked data-quoted
    and every region marked data-conformance="aa".
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []
        self.skipping = 0
        self.in_main = False

    VOID = frozenset(
        {
            "br",
            "hr",
            "img",
            "input",
            "meta",
            "link",
            "source",
            "wbr",
            "col",
            "area",
            "base",
            "embed",
            "param",
            "track",
        }
    )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.VOID:
            return
        a = dict(attrs)
        if tag == "main":
            self.in_main = True
        classes = set((a.get("class") or "").split())
        skip = (
            tag in SKIP_TAGS
            or bool(classes & SKIP_CLASSES)
            or QUOTED in a
            or a.get("data-conformance") == "aa"
        )
        self.stack.append((tag, skip))
        if skip:
            self.skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        # Close back to the matching open tag, as browsers do with unclosed inline elements.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for _, skip in self.stack[i:]:
                    if skip:
                        self.skipping -= 1
                del self.stack[i:]
                break
        if tag == "main":
            self.in_main = False

    def handle_data(self, data: str) -> None:
        if self.in_main and not self.skipping:
            self.parts.append(data)


def prose(page_html: str) -> str:
    p = _Prose()
    p.feed(page_html)
    return unescape(" ".join(p.parts))


def check_page(page_html: str, rel: str, names: tuple[str, ...] = ()) -> list[str]:
    bad = unknown(prose(page_html), known_tokens(glossary()), names)
    return (
        [f"{rel}: abbreviation not in the glossary or expanded on the page: {', '.join(bad)}"]
        if bad
        else []
    )


def register_copy(d: Dataset) -> dict[str, str]:
    """Every piece of a register entry's copy that a page shows as prose, by where it sits."""
    fields = {k: getattr(d, k, "") or "" for k in COPY_FIELDS}
    for i, (q, a) in enumerate(d.faq):
        fields[f"faq[{i}].q"] = q
        fields[f"faq[{i}].a"] = a
    fields["licence.condition"] = d.licence.condition or ""
    fields["sample.label"] = (d.sample.get("label") if d.sample else None) or ""
    fields["geometry.crs_note"] = (d.geometry.get("crs_note") if d.geometry else None) or ""
    return fields


def check_copy(fields: dict[str, str], ctx: str, names: tuple[str, ...] = ()) -> list[str]:
    """The register's copy, as validate sees it."""
    errors = []
    known = known_tokens(glossary())
    for k, v in fields.items():
        bad = unknown(v or "", known, names)
        if bad:
            errors.append(
                f"{ctx}: {k} uses {', '.join(bad)}, which the glossary (pipeline/publicdata/glossary.json) does not explain"
            )
    return errors


def render(entries: dict[str, str]) -> str:
    """The glossary page's list, one definition per term."""
    items = "".join(
        f"<div><dt>{escape(t)}</dt><dd>{escape(d)}</dd></div>" for t, d in entries.items()
    )
    return f'<dl class="glossary">{items}</dl>'
