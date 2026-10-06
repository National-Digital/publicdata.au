"""Tool Definition Quality Score for the MCP tools, with the rubric directories publish.

The rubric is https://github.com/glama-ai/tool-definition-quality-score, read at a pinned commit
and checked against a hash, since it carries no licence to copy. `score` asks a model to judge
each tool three times, only when the tool's definition hash has no stored score, and writes
tdqs.json. `check` reads tdqs.json and calls nothing, so CI answers the same way every time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .api_text import TOOLS_FILE

RUBRIC_URL = "https://raw.githubusercontent.com/glama-ai/tool-definition-quality-score/b9881b0cfec88969e42672c92544487ca191a992/README.md"
RUBRIC_SHA = "7861384507c131e3321024936e691ca6909a9ced2860e312e9bad32e9cd1ab5f"
# Sonnet tracked the directory scores within 0.1 on eight of nine tools; Haiku ran 0.2 low.
MODEL = os.environ.get("TDQS_MODEL", "claude-sonnet-5-5")
RUNS = 3
STORE = Path(os.environ.get("TDQS_STORE") or Path(__file__).with_name("tdqs.json"))
# The bar a tool and the server must clear. A median dimension of 4 anywhere costs a tool 0.1
# to 0.25, so these allow one or two soft spots and nothing more.
MIN_TOOL = 4.8
MIN_SERVER = 4.8

WEIGHTS = {
    "purpose_clarity": 25,
    "usage_guidelines": 20,
    "behavioral_transparency": 20,
    "parameter_semantics": 15,
    "conciseness_structure": 10,
    "contextual_completeness": 10,
}
COHERENCE = ("disambiguation", "naming_consistency", "tool_count_appropriateness", "completeness")
FIELDS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")


def round1(p: int, q: int) -> float:
    """p / q rounded half-up to one decimal in integers, as the rubric specifies."""
    return ((20 * p + q) // (2 * q)) / 10


def tool_score(dims: dict) -> float:
    return round1(sum(dims[k] * w for k, w in WEIGHTS.items()), 100)


def server_scores(tools: list[float], coherence: dict) -> dict:
    tenths = [round(t * 10) for t in tools]
    n = len(tenths)
    quality = round1(6 * sum(tenths) + 4 * n * min(tenths), 100 * n)
    coh = round1(sum(coherence[k] for k in COHERENCE), 4)
    return {
        "description_quality": quality,
        "coherence": coh,
        "overall": round1(7 * round(quality * 10) + 3 * round(coh * 10), 100),
    }


def definition_hash(tool: dict) -> str:
    body = json.dumps(
        {k: tool.get(k) for k in FIELDS}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def tools() -> list[dict]:
    return [
        {k: t.get(k) for k in FIELDS} for t in json.loads(TOOLS_FILE.read_text("utf-8"))["tools"]
    ]


def signals(tool: dict) -> dict:
    s = tool.get("inputSchema") or {}
    props = s.get("properties") or {}
    described = sum(1 for p in props.values() if str(p.get("description", "")).strip())
    return {
        "params": len(props),
        "required": len(s.get("required") or []),
        "coverage": round(described / len(props) * 100) if props else 100,
        "enums": sum(1 for p in props.values() if "enum" in p),
        "nested": any(p.get("type") == "object" for p in props.values()),
    }


def _union(s: dict) -> list[dict] | None:
    for k in ("oneOf", "anyOf"):
        branches = s.get(k)
        if branches and not (len(branches) == 2 and {"type": "null"} in branches):
            return branches
    return None


def _walk(s: dict, depth: int) -> tuple[int, int, int]:
    """Required fields, depth and union choices over the required subtree of one schema."""
    if depth > 10 or not isinstance(s, dict):
        return 0, 0, 0
    branches = _union(s)
    if branches:
        walked = [_walk(b, depth) for b in branches]
        return (
            max(w[0] for w in walked),
            max(w[1] for w in walked),
            len(branches) - 1 + sum(w[2] for w in walked),
        )
    if (
        s.get("type") == "array"
        and isinstance(s.get("items"), dict)
        and s["items"].get("properties")
    ):
        return _walk(s["items"], depth)
    props = s.get("properties") or {}
    if not props:
        return 0, 0, 0
    fields, deepest, unions = 0, 1, 0
    for name in s.get("required") or []:
        f, d, u = _walk(props.get(name) or {}, depth + 1)
        fields += 1 + f
        deepest = max(deepest, 1 + d)
        unions += u
    return fields, deepest, unions


def invocation_cost(tool: dict) -> tuple[int, int, int, int]:
    fields, depth, unions = _walk(tool.get("inputSchema") or {}, 0)
    return fields + 2 * max(0, depth - 1) + 2 * unions, fields, depth, unions


def shadow_candidates(ts: list[dict]) -> list[str]:
    cost = {t["name"]: invocation_cost(t)[0] for t in ts}
    out = []
    for t in ts:
        dear = cost[t["name"]]
        cheaper = [n for n, c in cost.items() if n != t["name"] and dear >= 2 * c and dear - c >= 4]
        if cheaper:
            # The dearest qualifying sibling is the likeliest real overlap.
            c = max(cheaper, key=lambda n: (cost[n], n))
            out.append(f"{t['name']} (cost {dear}) may be shadowed by {c} (cost {cost[c]})")
    return out


def set_hash(ts: list[dict]) -> str:
    return hashlib.sha256(server_message("publicdata-au", ts).encode("utf-8")).hexdigest()[:16]


def rubric() -> dict:
    with urllib.request.urlopen(RUBRIC_URL, timeout=30) as r:
        text = r.read().decode("utf-8")
    blocks = []
    for part in ("## Appendix A: Tool scoring prompt", "## Appendix B: Server coherence prompt"):
        blocks += re.findall(r"```text\n(.*?)\n```", text.split(part, 1)[1], re.S)[:2]
    if hashlib.sha256("\0".join(blocks).encode("utf-8")).hexdigest() != RUBRIC_SHA:
        raise SystemExit(
            "the rubric at the pinned commit has changed; review it and update RUBRIC_SHA"
        )
    return dict(
        zip(("tool_system", "tool_user", "server_system", "server_user"), blocks, strict=True)
    )


def tool_message(tool: dict, siblings: list[str]) -> str:
    s = signals(tool)

    def dump(v, none: str) -> str:
        return json.dumps(v, indent=2, ensure_ascii=False) if v else none

    return (
        f"TOOL NAME: {tool['name']}\nTITLE: {tool.get('title') or 'null'}\n\n"
        f'DESCRIPTION:\n"{tool.get("description") or ""}"\n\n'
        f"<input-schema>\n{dump(tool.get('inputSchema'), '{}')}\n</input-schema>\n\n"
        f"<output-schema>\n{dump(tool.get('outputSchema'), 'None provided')}\n</output-schema>\n\n"
        f"<annotations>\n{dump(tool.get('annotations'), 'None provided')}\n</annotations>\n\n"
        "CONTEXT SIGNALS:\n"
        f"- Parameter count: {s['params']}\n- Required parameters: {s['required']}\n"
        f"- Schema description coverage: {s['coverage']}%\n- Parameters with enums: {s['enums']}\n"
        f"- Has nested objects: {'true' if s['nested'] else 'false'}\n\n"
        f"<sibling-tools>\n{chr(10).join(siblings) or 'None'}\n</sibling-tools>\n\nRespond with JSON only."
    )


def server_message(name: str, ts: list[dict]) -> str:
    lines = []
    for t in ts:
        c, f, d, u = invocation_cost(t)
        lines.append(
            f"- {t['name']} [cost {c}: {f} required, depth {d}, {u} union choices]: {t.get('description') or '(no description)'}"
        )
    cands = shadow_candidates(ts)
    return (
        f"SERVER NAME: {name}\nTOOL COUNT: {len(ts)}\n\n<tools>\n"
        + "\n".join(lines)
        + "\n</tools>\n\n"
        "<shadow-candidates>\n"
        + ("\n".join(cands) or "None")
        + "\n</shadow-candidates>\n\nRespond with JSON only."
    )


def ask(system: str, message: str, keys: tuple[str, ...]) -> dict:
    """One judgement, through the Claude Code CLI so the org's Claude Code token can pay for it.
    A reply that is not the promised JSON is asked again, as the rubric does."""
    for _ in range(3):
        r = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                MODEL,
                "--system-prompt",
                system,
                "--tools",
                "",
                "--safe-mode",
                "--no-session-persistence",
                "--output-format",
                "json",
            ],
            input=message,
            capture_output=True,
            text=True,
            timeout=300,
        )
        try:
            # A list of events with verbose output on, or the result event alone.
            events = json.loads(r.stdout)
            events = events if isinstance(events, list) else [events]
            text = next(e for e in reversed(events) if e.get("type") == "result")["result"]
            got = json.loads(re.search(r"\{.*\}", text, re.S)[0])
            scores = {k: int(got["scores"][k]["score"]) for k in keys}
            if all(1 <= v <= 5 for v in scores.values()):
                return {
                    "scores": scores,
                    "why": {k: got["scores"][k]["justification"] for k in keys},
                    "summary": got.get("summary", ""),
                    **(
                        {"contradiction": bool(got.get("annotation_contradiction"))}
                        if "annotation_contradiction" in got
                        else {}
                    ),
                }
        # Python 3.14 takes several exception types without brackets (PEP 758), as ruff writes it.
        except ValueError, KeyError, TypeError, AttributeError, StopIteration:
            pass
    raise RuntimeError(f"no usable judgement after 3 tries: {r.stderr[-300:] or r.stdout[-300:]}")


def median(runs: list[dict], keys) -> dict:
    return {k: int(statistics.median_low(r["scores"][k] for r in runs)) for k in keys}


def load() -> dict:
    return json.loads(STORE.read_text("utf-8")) if STORE.exists() else {"tools": {}, "server": {}}


def rubric_id() -> str:
    return f"{RUBRIC_SHA[:12]}/{MODEL}/{RUNS}"


def score(workers: int = 6) -> int:
    ts = tools()
    names = [t["name"] for t in ts]
    old = load() if load().get("rubric") == rubric_id() else {"tools": {}, "server": {}}
    current = {definition_hash(t) for t in ts}
    kept = {h: v for h, v in old["tools"].items() if h in current}
    todo = [t for t in ts if definition_hash(t) not in kept]
    current = set_hash(ts)
    server_todo = old["server"].get("hash") != current
    if not todo and not server_todo:
        print("every tool and the tool set already have scores; nothing asked")
        return 0
    p = rubric()
    jobs = [(t, i) for t in todo for i in range(RUNS)]
    with ThreadPoolExecutor(workers) as ex:
        futs = [
            ex.submit(
                ask,
                p["tool_system"],
                tool_message(t, [n for n in names if n != t["name"]]),
                tuple(WEIGHTS),
            )
            for t, _ in jobs
        ]
        sfuts = (
            [
                ex.submit(ask, p["server_system"], server_message("publicdata-au", ts), COHERENCE)
                for _ in range(RUNS)
            ]
            if server_todo
            else []
        )
        results = [f.result() for f in futs]
        sresults = [f.result() for f in sfuts]
    for n, t in enumerate(todo):
        runs = results[n * RUNS : (n + 1) * RUNS]
        dims = median(runs, WEIGHTS)
        kept[definition_hash(t)] = {
            "name": t["name"],
            "tdqs": tool_score(dims),
            "median": dims,
            "runs": runs,
        }
        print(f"scored {t['name']}: {tool_score(dims)}")
    server = old["server"]
    if server_todo:
        server = {"hash": current, "median": median(sresults, COHERENCE), "runs": sresults}
    STORE.write_text(
        json.dumps(
            {
                "rubric": rubric_id(),
                "tools": dict(sorted(kept.items(), key=lambda kv: names.index(kv[1]["name"]))),
                "server": server,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


def problems(ts: list[dict], store: dict) -> tuple[list[str], list[str]]:
    """What stops a merge, and the report, from tdqs.json alone."""
    errors, report = [], []
    if store.get("rubric") != rubric_id():
        return [
            f"tdqs.json was scored under {store.get('rubric')}, not {rubric_id()}; run python -m publicdata.tdqs score"
        ], report
    scores = []
    for t in ts:
        s = store["tools"].get(definition_hash(t))
        if not s:
            errors.append(
                f"{t['name']}: its definition changed and has no score; run python -m publicdata.tdqs score"
            )
            continue
        scores.append(s["tdqs"])
        soft = [f"{k} {v}" for k, v in s["median"].items() if v < 5]
        report.append(
            f"{t['name']:18s} {s['tdqs']:.1f}" + (f"  ({', '.join(soft)})" if soft else "")
        )
        if s["tdqs"] < MIN_TOOL:
            errors.append(
                f"{t['name']}: TDQS {s['tdqs']} is under {MIN_TOOL}; its judgements say why in tdqs.json"
            )
        if any(r.get("contradiction") for r in s["runs"]):
            errors.append(f"{t['name']}: a judge found the description contradicts the annotations")
    current = set_hash(ts)
    if store["server"].get("hash") != current:
        errors.append(
            "the tool set changed and has no coherence score; run python -m publicdata.tdqs score"
        )
    elif len(scores) == len(ts):
        s = server_scores(scores, store["server"]["median"])
        report.append(
            f"server             {s['overall']:.1f}  (quality {s['description_quality']}, coherence {s['coherence']})"
        )
        if s["overall"] < MIN_SERVER:
            errors.append(f"server: overall {s['overall']} is under {MIN_SERVER}")
    return errors, report


def check() -> int:
    errors, report = problems(tools(), load())
    print("\n".join(report))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### TDQS\n\n```\n" + "\n".join(report + errors) + "\n```\n")
    for e in errors:
        print("TDQS: " + e, file=sys.stderr)
    return 1 if errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m publicdata.tdqs", description=__doc__.splitlines()[0]
    )
    ap.add_argument("command", choices=["score", "check"])
    a = ap.parse_args(argv)
    return score() if a.command == "score" else check()


if __name__ == "__main__":
    sys.exit(main())
