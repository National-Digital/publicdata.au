"""Compare the DuckDB files of two built trees by what they hold.

A DuckDB file's bytes are not reproducible: its storage picks a compression for each block by
sampling, so two writes of the same rows differ. CI proves determinism with a byte comparison of
everything else and this comparison of every data.duckdb, table by table, row by row.

    python -m publicdata.dbcheck /tmp/build-a /tmp/build-b
"""

from __future__ import annotations

import sys
from pathlib import Path

from .serialise import duckdb_digest

NAME = "data.duckdb"


def compare(a: Path, b: Path) -> list[str]:
    """Every data.duckdb under a must be under b with the same content, and the other way."""
    problems = []
    fa = {p.relative_to(a).as_posix(): p for p in a.rglob(NAME)}
    fb = {p.relative_to(b).as_posix(): p for p in b.rglob(NAME)}
    problems.extend(
        f"{rel}: only in {'a' if rel in fa else 'b'}" for rel in sorted(set(fa) ^ set(fb))
    )
    problems.extend(
        f"{rel}: the two files hold different content"
        for rel in sorted(set(fa) & set(fb))
        if duckdb_digest(fa[rel]) != duckdb_digest(fb[rel])
    )
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:  # noqa: PLR2004 - the two trees
        print("usage: python -m publicdata.dbcheck <tree-a> <tree-b>", file=sys.stderr)
        return 2
    a, b = Path(argv[0]), Path(argv[1])
    problems = compare(a, b)
    for p in problems:
        print(f"dbcheck: {p}", file=sys.stderr)
    n = len(list(a.rglob(NAME)))
    print(
        f"dbcheck: {'FAIL' if problems else 'PASS'} ({n} DuckDB file(s), {len(problems)} problems)"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
