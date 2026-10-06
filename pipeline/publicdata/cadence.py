"""The register's cadence text read as versions a year. The cost check and the hubs both read it
here, so the two never disagree about how often a dataset changes."""

from __future__ import annotations

import re

# The weekly fetch makes at most one version a week, and the daily feed run one a day.
WEEKLY_MAX = 52
FEED_MAX = 365

# An end date or a closed state outranks a rate in the same phrase ("monthly until June 2024").
ENDED = re.compile(
    r"\buntil (\w+ )?\d{4}\b"
    r"|^(historical, )?(closed|no longer updated|not updated)\b"
    r"|\bno new readings\b"
)
# First match wins; the rate words come before the words that only describe the history.
RATES = (
    (r"\bchecked weekly\b", 52),
    (r"every \d+ minutes|\bhourly\b|\bcontinual\b|\blive\b|\bdaily\b", 365),
    (r"\bweekly\b", 52),
    (r"\bfortnightly\b", 26),
    (r"\bmonthly\b|\bthrough the year\b", 12),
    (r"\bquarterly\b|several times a year", 4),
    (r"twice a year|half-yearly", 2),
    (r"\byearly\b|\bannually\b|\ba year\b|school year|one or two years", 1),
    (r"\bcensus\b", 0.2),
    (r"\bclosed\b|\bhistorical\b|\bno longer\b|\bnot updated\b", 0),
)


def per_year(text: str, feed: bool = False) -> float | None:
    """The rate the cadence names, or None when it names none ("irregular", "as required")."""
    t = text.strip().lower()
    if ENDED.search(t):
        return 0.0
    for pattern, n in RATES:
        if re.search(pattern, t):
            return float(n)
    return float(FEED_MAX) if feed else None


def kaggle_frequency(text: str) -> str:
    """Kaggle's fixed choices. A rate between two takes the slower one, so a page never promises
    updates more often than the publisher makes them."""
    n = per_year(text)
    if n is None:
        return "annually"
    for floor, name in ((365, "daily"), (52, "weekly"), (12, "monthly"), (4, "quarterly")):
        if n >= floor:
            return name
    return "never" if n == 0 else "annually"
