"""pyarrow.compute calls whose types pyarrow-stubs 20 gets wrong, typed once here."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow.compute as pc

if TYPE_CHECKING:
    from collections.abc import Callable

    from .normalise import Arr

# The stubs type fill_null as coalesce, whose fill must be an Arrow value and not a Python one.
_fill_null: Callable[[Arr, object], Arr] = pc.fill_null  # type: ignore[assignment]  # as above


def fill_null(values: Arr, fill: object) -> Arr:
    """pc.fill_null, with the fill given as a Python value as pyarrow allows."""
    return _fill_null(values, fill)
