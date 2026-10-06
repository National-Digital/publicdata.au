"""Checks register validate runs against the versions already stored, where their files are at
hand: a CI checkout has the manifests alone, a fetch runner or a working copy has more."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq

from .serialise.profile import INT32, misfits, query_key


def _parquet_misfits(path: Path, cols) -> list[str]:
    """Declared INT32 fields a built Parquet does not fit, read from its column statistics."""
    meta = pq.read_metadata(path)
    names = [meta.schema.column(i).name for i in range(meta.num_columns)]
    out = []
    for c in cols:
        if c not in names:
            continue
        i = names.index(c)
        lo = hi = None
        for g in range(meta.num_row_groups):
            st = meta.row_group(g).column(i).statistics
            if st is None or not st.has_min_max:
                if st is not None and st.null_count == meta.row_group(g).num_rows:
                    continue
                return misfits(pq.read_table(path, columns=[c]), [c])
            lo = st.min if lo is None else min(lo, st.min)
            hi = st.max if hi is None else max(hi, st.max)
        if lo is not None and not (INT32[0] <= lo and hi <= INT32[1]):
            out.append(f"{c} holds {lo} to {hi}, outside 32 bits")
    return out


def int32_misfits(ds, m, store_dir: Path, built: list[Path]) -> list[str] | None:
    """The entry's int32 fields this stored version does not fit, or None when neither a built
    Parquet of it nor its source is at hand."""
    rel = f"d/{ds.slug}/v/{m.version}/data.parquet"
    for root in built:
        for p in (root / rel, root / query_key(ds.slug, m.version)):
            if p.is_file():
                return _parquet_misfits(p, ds.int32)
    from . import store
    from .normalise import normalise

    src = store.source_path(store_dir, m)
    if not src.is_file():
        return None
    return misfits(normalise(ds, m, src.read_bytes()).table, ds.int32)
