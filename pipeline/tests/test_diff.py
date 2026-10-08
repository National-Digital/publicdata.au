from publicdata.diff import diff
from publicdata.normalise import normalise
from publicdata.register import Field

from .conftest import make_dataset, make_manifest

F = [Field("id", "Id", "integer"), Field("v", "V")]


def _tbl(csv: bytes, version: str):
    ds = make_dataset(F, key=("id",))
    return normalise(ds, make_manifest(csv, version=version), csv)


def test_diff_by_key():
    a = _tbl(b"Id,V\n1,a\n2,b\n3,c\n", "2026-01-01")
    b = _tbl(b"Id,V\n2,b\n3,C\n4,d\n", "2026-02-01")
    d = diff(a, b)
    assert (d["added"], d["removed"], d["changed"], d["unchanged"]) == (1, 1, 1, 1)
    assert d["added_keys"] == [4]
    assert d["removed_keys"] == [1]
    assert d["changed_keys"] == [3]
    assert d["examples"][0]["fields"] == {"v": {"from": "c", "to": "C"}}


def test_diff_handles_suppressed_rows_and_rejects_duplicate_keys():
    import pytest

    from publicdata.register import Field as F2

    fields = [F2("id", "Id", "integer"), F2("n", "N", "integer")]
    ds = make_dataset(fields, key=("id",), suppression=("<5",))
    a_csv, b_csv = b"Id,N\n1,<5\n2,7\n", b"Id,N\n1,<5\n2,8\n"
    a = normalise(ds, make_manifest(a_csv, version="2026-01-01"), a_csv)
    b = normalise(ds, make_manifest(b_csv, version="2026-02-01"), b_csv)
    d = diff(a, b)
    assert d["changed"] == 1
    assert d["unchanged"] == 1
    dup = b"Id,V\n1,a\n1,b\n"
    with pytest.raises(ValueError, match="not unique"):
        diff(_tbl(dup, "2026-01-01"), _tbl(dup, "2026-02-01"))


# The per-row implementation this module replaced, kept as the oracle for the Arrow join.
def _reference(a, b):
    import hashlib
    import json

    from publicdata.serialise import iter_rows, json_view

    def keyed(t, key):
        out = {}
        for row in iter_rows(json_view(t)):
            k = tuple(row[c] for c in key)
            if k in out:
                msg = f"key {key} is not unique: {k} appears twice"
                raise ValueError(msg)
            out[k] = hashlib.sha256(
                json.dumps(row, sort_keys=True, default=str).encode()
            ).hexdigest()
        return out

    def rows_for(t, key, wanted):
        return {k: r for r in iter_rows(json_view(t)) if (k := tuple(r[c] for c in key)) in wanted}

    key = a.dataset.key
    ka, kb = keyed(a.table, key), keyed(b.table, key)
    added = sorted(k for k in kb if k not in ka)
    removed = sorted(k for k in ka if k not in kb)
    changed = sorted(k for k in kb if k in ka and ka[k] != kb[k])
    want = set(changed[:10])
    ra, rb = rows_for(a.table, key, want), rows_for(b.table, key, want)
    examples = [
        {
            "key": list(k),
            "fields": {
                f: {"from": ra[k].get(f), "to": rb[k].get(f)}
                for f in rb[k]
                if ra[k].get(f) != rb[k].get(f)
            },
        }
        for k in changed[:10]
    ]
    fmt = (lambda k: k[0]) if len(key) == 1 else list
    return {
        "added": len(added),
        "removed": len(removed),
        "changed": len(changed),
        "unchanged": len(kb) - len(added) - len(changed),
        "added_keys": [fmt(k) for k in added],
        "removed_keys": [fmt(k) for k in removed],
        "changed_keys": [fmt(k) for k in changed],
        "examples": examples,
    }


def _random_table(rng, key_type, extra, retype, rows):
    import datetime as dt

    import pyarrow as pa

    ids = rng.sample(range(rows * 2), rows)
    region = [rng.choice("NS") for _ in ids]
    cols = {
        "id": pa.array([str(i) if key_type == "string" else i for i in ids]),
        "region": pa.array(region),
        "n": pa.array(
            [
                None
                if rng.random() < 0.2
                else float(rng.randint(0, 3))
                if retype
                else rng.randint(0, 3)
                for _ in ids
            ],
            pa.float64() if retype else pa.int64(),
        ),
        "x": pa.array(
            [None if rng.random() < 0.1 else rng.choice([0.5, 1.5, float("nan")]) for _ in ids]
        ),
        "day": pa.array([dt.date(2020, 1, 1 + rng.randint(0, 2)) for _ in ids], pa.date32()),
        "ok": pa.array([rng.choice([True, False, None]) for _ in ids]),
        "suppressed": pa.array(
            [rng.choice([None, [], ["n"], ["n", "x"]]) for _ in ids], pa.list_(pa.string())
        ),
    }
    if extra:
        cols["extra"] = pa.array([rng.choice(["a", None]) for _ in ids])
    return pa.table(cols)


def _next_release(rng, ta, tnew):
    """ta with some rows dropped, some edited and tnew's rows under ids ta lacks added."""
    import pyarrow as pa
    import pyarrow.compute as pc

    kept = ta.filter(pa.array([rng.random() < 0.8 for _ in range(ta.num_rows)], pa.bool_()))
    n = [v if rng.random() < 0.7 else (v or 0) + 1 for v in kept["n"].to_pylist()]
    kept = kept.set_column(kept.column_names.index("n"), "n", pa.array(n, kept["n"].type))
    fresh = tnew.filter(pc.invert(pc.is_in(tnew["id"], value_set=ta["id"].combine_chunks())))
    return pa.concat_tables([kept, fresh])


def test_the_arrow_diff_matches_the_per_row_reference_on_random_versions():
    import json
    import random

    from publicdata.normalise import Table
    from publicdata.register import Field as F2

    rng = random.Random(7)
    for trial in range(60):
        key = ("id",) if trial % 3 else ("region", "id")
        fields = [F2(n, n) for n in ("id", "region", "n", "x", "day", "ok")]
        ds = make_dataset(fields, key=key)
        kt = rng.choice(["integer", "string"])
        ta = _random_table(rng, kt, False, False, rng.randint(0, 40))
        tb = _random_table(
            rng,
            kt if trial % 7 else ("string" if kt == "integer" else "integer"),
            trial % 5 == 0,
            trial % 4 == 0,
            rng.randint(0, 40),
        )
        if trial % 2 and tb.schema == ta.schema:
            tb = _next_release(rng, ta, tb)
        a = Table(ds, make_manifest(b"a", version="2026-01-01"), ta)
        b = Table(ds, make_manifest(b"b", version="2026-02-01"), tb)
        got = diff(a, b)
        want = _reference(a, b)
        # Compared as the JSON the diff file holds, since NaN never equals itself.
        dump = lambda d: json.dumps(d, sort_keys=True)  # noqa: E731
        assert dump({k: got[k] for k in want}) == dump(want), trial


def test_a_blank_key_part_matches_itself_across_versions():
    from publicdata.register import Field as F2

    fields = [F2("offence", "Offence"), F2("sub", "Sub"), F2("n", "N", "integer")]
    ds = make_dataset(fields, key=("offence", "sub"))
    a_csv = b"Offence,Sub,N\nAssault,,5\nTheft,Shop,3\nArson,,1\n"
    b_csv = b"Offence,Sub,N\nAssault,,6\nTheft,Shop,3\nFraud,,2\n"
    a = normalise(ds, make_manifest(a_csv, version="2026-01-01"), a_csv)
    b = normalise(ds, make_manifest(b_csv, version="2026-02-01"), b_csv)
    d = diff(a, b)
    assert (d["added"], d["removed"], d["changed"], d["unchanged"]) == (1, 1, 1, 1)
    assert d["added_keys"] == [["Fraud", None]]
    assert d["removed_keys"] == [["Arson", None]]
    assert d["changed_keys"] == [["Assault", None]]
    assert d["examples"][0]["fields"] == {"n": {"from": 5, "to": 6}}
