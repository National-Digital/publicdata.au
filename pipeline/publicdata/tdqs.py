"""Static check of the MCP tool definitions that api.json writes.

Each tool must state its purpose, say when to use it against its siblings, describe every
parameter, state its limits and what comes back, carry consistent annotations and stay concise.
The tool set must follow one naming convention and hold no two tools a caller could confuse.
The check reads api.json alone and asks no service, so a contributor's machine and CI give the
same answer.
"""

from __future__ import annotations

import argparse
import itertools
import os
import re
import sys

from .api_text import mcp_spec

NAME = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
MIN_DESCRIPTION, MAX_DESCRIPTION = 200, 1000
MAX_SENTENCE_WORDS = 35
MIN_PARAM_WORDS, MAX_PARAM_CHARS = 3, 500
MAX_TITLE_CHARS = 40
MAX_TOOLS = 20
MIN_PURPOSE_WORDS = 6
# Purpose sentences this alike must point at each other; this alike they are the same tool.
OVERLAP_NAMED, OVERLAP_DUPLICATE = 0.3, 0.7
NOT_A_PURPOSE = {"a", "an", "the", "this", "it", "use", "used", "tool"}
WRITE_VERBS = {"add", "create", "delete", "remove", "set", "update", "write"}
LIMITS = re.compile(r"rate limit|queries each address may make", re.IGNORECASE)
RETURNS = re.compile(
    r"\bcomes? back\b|\bthe answer\b|\bone (?:answer|page)\b|\ba page holds\b"
    r"|\breturns? (?!(?:a |an )?(?:\d+ )?error)",
    re.IGNORECASE,
)
STOPWORDS = set(
    "a an and any as at be by for from has in into is it its of on one or so than that the this "
    "to up use when which with".split()
)


def tools() -> list[dict]:
    return mcp_spec()["tools"]


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in STOPWORDS}


def overlap(a: str, b: str) -> float:
    x, y = words(a), words(b)
    return len(x & y) / len(x | y) if x | y else 0.0


def names(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def _purpose(t: dict) -> list[str]:
    out = []
    title = (t.get("title") or "").strip()
    if not title:
        out.append("has no title; give it a short name in sentence case")
    elif len(title) > MAX_TITLE_CHARS or title.endswith(".") or not title[0].isupper():
        out.append(
            f"title {title!r} should be sentence case, under {MAX_TITLE_CHARS} characters, "
            "with no full stop"
        )
    first = sentences(t.get("description") or "")
    if not first:
        out.append("has no description; open with a sentence saying what the tool does")
        return out
    lead = first[0].split()
    if (
        len(lead) < MIN_PURPOSE_WORDS
        or lead[0].lower() in NOT_A_PURPOSE
        or not lead[0][0].isupper()
    ):
        out.append(
            "the description should open with a sentence of six words or more that starts "
            "with the verb for what the tool does"
        )
    return out


def _usage(t: dict, siblings: list[str]) -> list[str]:
    d = t.get("description") or ""
    if siblings and not any(names(d, s) for s in siblings):
        return ["the description names no other tool; say when to use this one instead of another"]
    return []


def _parameters(t: dict) -> list[str]:
    s = t.get("inputSchema") or {}
    out = []
    if s.get("type") != "object":
        return ["inputSchema must be an object schema"]
    props = s.get("properties") or {}
    for r in s.get("required") or []:
        if r not in props:
            out.append(f"required parameter {r} is not among the properties")
    if s.get("additionalProperties") is not False:
        out.append("inputSchema should set additionalProperties to false so a typo is refused")
    for p, v in props.items():
        desc = str(v.get("description") or "").strip()
        if len(desc.split()) < MIN_PARAM_WORDS:
            out.append(
                f"parameter {p} needs a description of {MIN_PARAM_WORDS} words or more "
                "saying what it takes and where the value comes from"
            )
        elif len(desc) > MAX_PARAM_CHARS:
            out.append(f"parameter {p}'s description runs past {MAX_PARAM_CHARS} characters")
        if v.get("type") in ("integer", "number") and "minimum" not in v:
            out.append(f"parameter {p} is a number with no minimum")
        if v.get("type") == "array" and not isinstance(v.get("items"), dict):
            out.append(f"parameter {p} is an array with no items schema")
        if "default" in v and "enum" in v and v["default"] not in v["enum"]:
            out.append(f"parameter {p}'s default is not one of its values")
        lo, hi, dv = v.get("minimum"), v.get("maximum"), v.get("default")
        if isinstance(dv, int | float) and (
            (lo is not None and dv < lo) or (hi is not None and dv > hi)
        ):
            out.append(f"parameter {p}'s default {dv} is outside its range")
    return out


def _limits(t: dict) -> list[str]:
    if not LIMITS.search(t.get("description") or ""):
        return ["the description does not say what a call costs against the rate limit"]
    return []


def _returns(t: dict) -> list[str]:
    s = t.get("outputSchema") or {}
    props = s.get("properties") or {}
    if s.get("type") != "object" or not props:
        return ["outputSchema must be an object schema with properties"]
    out = [
        f"outputSchema requires {r}, which is not among its properties"
        for r in s.get("required") or []
        if r not in props
    ]
    # Output field names such as rows or key are ordinary words, so only a return cue counts.
    if not RETURNS.search(t.get("description") or ""):
        out.append(
            "the description does not say what comes back; add a sentence saying what one "
            "answer holds, such as 'Up to 50 matches come back in one answer'"
        )
    return out


def _annotations(t: dict) -> list[str]:
    a = t.get("annotations") or {}
    out = [f"annotations has no boolean {h}" for h in HINTS if not isinstance(a.get(h), bool)]
    if a.get("title") != t.get("title"):
        out.append("annotations.title differs from title")
    if a.get("readOnlyHint") is True:
        if a.get("destructiveHint") is True:
            out.append("a read-only tool cannot be destructive")
        lead = (t.get("description") or "").split()
        if lead and lead[0].lower() in WRITE_VERBS:
            out.append(f"is marked read-only but its description opens with {lead[0]!r}")
    return out


def _length(t: dict) -> list[str]:
    d = t.get("description") or ""
    out = []
    if d and not MIN_DESCRIPTION <= len(d) <= MAX_DESCRIPTION:
        out.append(
            f"the description is {len(d)} characters; keep it between {MIN_DESCRIPTION} "
            f"and {MAX_DESCRIPTION}"
        )
    for s in sentences(d):
        if len(s.split()) > MAX_SENTENCE_WORDS:
            out.append(f"a sentence runs past {MAX_SENTENCE_WORDS} words: {s[:60]!r}")
    return out


def _naming(t: dict) -> list[str]:
    if not NAME.match(t.get("name") or ""):
        return [
            (
                f"name {t.get('name')!r} should be lowercase verb_object snake case, "
                "like the other tools"
            )
        ]
    return []


QUALITIES = (
    ("naming", _naming),
    ("purpose", _purpose),
    ("usage", _usage),
    ("parameters", _parameters),
    ("limits", _limits),
    ("returns", _returns),
    ("annotations", _annotations),
    ("length", _length),
)


def tool_problems(t: dict, siblings: list[str]) -> list[tuple[str, str]]:
    out = []
    for quality, f in QUALITIES:
        found = f(t, siblings) if f is _usage else f(t)
        out += [(quality, m) for m in found]
    return out


def set_problems(ts: list[dict]) -> list[tuple[str, str]]:
    out = []
    if len(ts) > MAX_TOOLS:
        out.append(
            ("tool count", f"{len(ts)} tools is more than {MAX_TOOLS}; merge tools that overlap")
        )
    seen: dict[str, int] = {}
    for t in ts:
        seen[t.get("name")] = seen.get(t.get("name"), 0) + 1
    out += [("naming", f"{n} is used by {c} tools") for n, c in seen.items() if c > 1]
    titles = [t.get("title") for t in ts if t.get("title")]
    out += [
        ("naming", f"title {x!r} is used twice")
        for x in sorted({x for x in titles if titles.count(x) > 1})
    ]
    for a, b in itertools.combinations(ts, 2):
        pa, pb = (sentences(t.get("description") or "")[:1] or [""] for t in (a, b))
        o = overlap(pa[0], pb[0])
        da, db = a.get("description") or "", b.get("description") or ""
        if o >= OVERLAP_DUPLICATE:
            out.append(
                ("disambiguation", f"{a['name']} and {b['name']} open with near the same purpose")
            )
        elif o >= OVERLAP_NAMED and not (names(da, b["name"]) or names(db, a["name"])):
            out.append(
                (
                    "disambiguation",
                    (
                        f"{a['name']} and {b['name']} have similar purposes and neither names the "
                        "other; say when to use each"
                    ),
                )
            )
    return out


def problems(ts: list[dict]) -> tuple[list[str], list[str]]:
    """The failures that stop a merge, and a line per tool for the report."""
    errors, report = [], []
    all_names = [t.get("name") for t in ts]
    for t in ts:
        found = tool_problems(t, [n for n in all_names if n != t.get("name")])
        errors += [f"{t.get('name')}: {q}: {m}" for q, m in found]
        report.append(
            f"{t.get('name')!s:18s} "
            + ("pass" if not found else "fails " + ", ".join(dict.fromkeys(q for q, _ in found)))
        )
    found = set_problems(ts)
    errors += [f"tool set: {q}: {m}" for q, m in found]
    report.append(f"{'tool set':18s} " + ("pass" if not found else f"fails {len(found)}"))
    return errors, report


def check(ts: list[dict] | None = None) -> int:
    errors, report = problems(tools() if ts is None else ts)
    print("\n".join(report))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Tool definitions\n\n```\n" + "\n".join(report + errors) + "\n```\n")
    for e in errors:
        print("tool definition: " + e, file=sys.stderr)
    return 1 if errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m publicdata.tdqs", description=__doc__.splitlines()[0]
    )
    ap.add_argument("command", choices=["check"])
    ap.parse_args(argv)
    return check()


if __name__ == "__main__":
    sys.exit(main())
