"""Compare two versions of one dataset by the declared key."""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc

from .normalise import Table
from .serialise import json_view

CAP = 50_000
EXAMPLES = 10
ROW = "__row"


def _indexed(t: pa.Table, key: tuple[str, ...]) -> pa.Table:
    """The table as its JSON files show it, with each row's position, checked for a unique key."""
    v = json_view(t)
    v = v.append_column(ROW, pa.array(range(v.num_rows), pa.int64()))
    counts = v.group_by(list(key)).aggregate([(ROW, "count")])
    dup = counts.filter(pc.greater(counts[f"{ROW}_count"], 1))
    if dup.num_rows:
        k = tuple(dup.slice(0, 1).select(list(key)).to_pylist()[0].values())
        msg = f"key {key} is not unique: {k} appears twice"
        raise ValueError(msg)
    return v


NULL_KEY = "\0"


def _join_keys(t: pa.Table, key: tuple[str, ...]) -> list[pa.ChunkedArray]:
    """Each key column as text with nulls as a marker no value holds, for joining only."""
    return [pc.fill_null(pc.cast(t[k], pa.string()), NULL_KEY) for k in key]


def _flat(c: pa.ChunkedArray) -> pa.ChunkedArray:
    # A list of suppressed field names compares as one string; no field name holds a NUL.
    return pc.binary_join(c, "\0") if pa.types.is_list(c.type) else c


def _differs(x: pa.ChunkedArray, y: pa.ChunkedArray) -> pa.ChunkedArray:
    """True where the two cells would serialise differently. Values of different types always
    do, unless both are null.
    """
    if x.type != y.type:
        return pc.or_(pc.is_valid(x), pc.is_valid(y))
    x, y = _flat(x), _flat(y)
    one_null = pc.xor(pc.is_null(x), pc.is_null(y))
    ne = pc.fill_null(pc.not_equal(x, y), False)
    if pa.types.is_floating(x.type):
        ne = pc.and_(ne, pc.invert(pc.fill_null(pc.and_(pc.is_nan(x), pc.is_nan(y)), False)))
    return pc.or_(ne, one_null)


def _keys(t: pa.Table, key: tuple[str, ...]) -> list[tuple]:
    rows = t.sort_by([(k, "ascending") for k in key]).select(list(key)).to_pylist()
    return [tuple(r[k] for k in key) for r in rows]


def _cap(items: list) -> tuple[list, bool]:
    return items[:CAP], len(items) > CAP


def diff(a: Table, b: Table) -> dict:
    """a is the older version, b the newer."""
    key = a.dataset.key
    fa = {f.name: f.type for f in a.dataset.fields}
    fb = {f.name: f.type for f in b.dataset.fields}
    schema = {
        "fields_added": [n for n in fb if n not in fa],
        "fields_removed": [n for n in fa if n not in fb],
        "fields_retyped": [
            {"field": n, "from": fa[n], "to": fb[n]} for n in fa if n in fb and fa[n] != fb[n]
        ],
    }
    out = {
        "dataset": a.dataset.slug,
        "from": a.manifest.version,
        "to": b.manifest.version,
        "rows_from": a.rows,
        "rows_to": b.rows,
        "key": list(key),
        "schema": schema,
    }
    if not key:
        out["note"] = "No key is declared, so rows are compared by count only."
        return out
    va, vb = _indexed(a.table, key), _indexed(b.table, key)
    ka = va.select([*key, ROW]).rename_columns([*key, "__a"])
    kb = vb.select([*key, ROW]).rename_columns([*key, "__b"])
    if any(ka[k].type != kb[k].type for k in key):
        # A retyped key matches nothing, as its values no longer compare equal.
        added, removed = kb, ka
        both = kb.slice(0, 0).append_column("__a", pa.nulls(0, pa.int64()))
    else:
        # A join never matches a null to a null, so a blank key part, such as a subcategory the
        # publisher leaves empty, would read as a row removed and another added.
        names = [f"__k{i}" for i in range(len(key))]
        ja = pa.table([*_join_keys(va, key), va[ROW]], names=[*names, "__a"])
        jb = pa.table([*_join_keys(vb, key), vb[ROW]], names=[*names, "__b"])
        j = jb.join(ja, keys=names, join_type="full outer")
        new = j.filter(pc.is_null(j["__a"]))
        old = j.filter(pc.is_null(j["__b"]))
        kept = j.filter(pc.and_(pc.is_valid(j["__a"]), pc.is_valid(j["__b"])))
        added = kb.take(new["__b"])
        removed = ka.take(old["__a"])
        both = kb.take(kept["__b"]).append_column("__a", kept["__a"])
    cols_a = [c for c in va.column_names if c != ROW]
    cols_b = [c for c in vb.column_names if c != ROW]
    ra, rb = va.take(both["__a"]), vb.take(both["__b"])
    if set(cols_a) != set(cols_b):
        # A field added or dropped changes every row's JSON.
        mask = pa.chunked_array([pa.array([True] * both.num_rows, pa.bool_())])
    else:
        mask = pa.chunked_array([pa.array([False] * both.num_rows, pa.bool_())])
        for c in cols_b:
            mask = pc.or_(mask, _differs(ra[c], rb[c]))
    changed = both.filter(mask).sort_by([(k, "ascending") for k in key])
    examples = []
    first = changed.slice(0, EXAMPLES)
    if first.num_rows:
        before = va.take(first["__a"]).drop_columns([ROW]).to_pylist()
        after = vb.take(first["__b"]).drop_columns([ROW]).to_pylist()
        for k, x, y in zip(_keys(first, key), before, after, strict=True):
            fields = {f: {"from": x.get(f), "to": y.get(f)} for f in y if x.get(f) != y.get(f)}
            examples.append({"key": list(k), "fields": fields})
    keyfmt = (lambda k: k[0]) if len(key) == 1 else list
    add_l, add_t = _cap([keyfmt(k) for k in _keys(added, key)])
    rem_l, rem_t = _cap([keyfmt(k) for k in _keys(removed, key)])
    chg_l, chg_t = _cap([keyfmt(k) for k in _keys(changed, key)])
    out.update(
        {
            "added": added.num_rows,
            "removed": removed.num_rows,
            "changed": changed.num_rows,
            "unchanged": vb.num_rows - added.num_rows - changed.num_rows,
            "added_keys": add_l,
            "removed_keys": rem_l,
            "changed_keys": chg_l,
            "truncated": add_t or rem_t or chg_t,
            "examples": examples,
        }
    )
    return out
