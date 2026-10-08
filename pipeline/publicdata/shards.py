"""Which datasets a deploy builds in its own jobs, and in how many.

A runner has one disk, so the versions the cache cannot serve are spread over up to `count` jobs
by source bytes, and the deploy then builds the site from the cache those jobs filled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import cache as cache_mod
from . import store
from .build import pending
from .cache import BuildCache, shape_layer

if TYPE_CHECKING:
    from pathlib import Path

    from .register import Dataset

# A job's share is never under this many source bytes, so a few small changes build in one job.
FLOOR = 1_000_000_000


def plan(weights: dict[str, int], count: int, floor: int = FLOOR) -> list[list[str]]:
    """Datasets packed largest first into jobs of at most max(total / count, floor) bytes.

    A dataset larger than that alone gets a job of its own. When that needs more than `count`
    jobs, each dataset goes to the lightest of `count` jobs instead, which holds every job within
    a third over the lightest packing `count` jobs allow.
    """
    todo = sorted(((w, s) for s, w in weights.items() if w > 0), key=lambda x: (-x[0], x[1]))
    if not todo:
        return []
    cap = max(-(-sum(w for w, _ in todo) // count), floor)
    jobs: list[tuple[int, list[str]]] = []
    for w, s in todo:
        fit = next((i for i, (load, _) in enumerate(jobs) if load + w <= cap), None)
        if fit is None:
            jobs.append((w, [s]))
        else:
            jobs[fit] = (jobs[fit][0] + w, [*jobs[fit][1], s])
    if len(jobs) > count:
        jobs = [(0, []) for _ in range(count)]
        for w, s in todo:
            i = min(range(count), key=lambda k: jobs[k][0])
            jobs[i] = (jobs[i][0] + w, [*jobs[i][1], s])
    return [sorted(slugs) for _, slugs in jobs if slugs]


def weights(datasets: list[Dataset], store_dir: Path, cache_dir: Path | None) -> dict[str, int]:
    """Each dataset's source bytes still to build.

    These are every version without a cache, else those the cache cannot serve as they are.
    """
    if cache_dir is None:
        return {
            d.slug: sum(max(m.bytes, 1) for m in store.manifests(store_dir, d.slug))
            for d in datasets
            if d.publishable
        }
    cache = BuildCache(cache_dir)
    now = {shape: cache_mod.writer_keys(shape) for shape in (False, True)}
    return {d.slug: pending(cache, d, store_dir, now[shape_layer(d)]) for d in datasets}
