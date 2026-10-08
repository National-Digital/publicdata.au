"""The register's cadence text read as versions a year. The cost check and the hubs both read it
here, so the two never disagree about how often a dataset changes.
"""

from __future__ import annotations

import datetime as dt
import re

# The daily feed run makes a version a day.
FEED_MAX = 365

# A closed state, or an end date that has passed, outranks a rate in the same phrase ("monthly
# until June 2024").
ENDED = re.compile(r"^(historical, )?(closed|no longer updated|not updated)\b|\bno new readings\b")
UNTIL = re.compile(r"\buntil (?:([a-z]+) )?(\d{4})\b")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
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


def _ended(t: str, today: dt.date) -> bool:
    if ENDED.search(t):
        return True
    m = UNTIL.search(t)
    if not m:
        return False
    month = MONTHS.index(m[1][:3]) + 1 if m[1] and m[1][:3] in MONTHS else 12
    return (int(m[2]), month) < (today.year, today.month)


def per_year(text: str, feed: bool = False, today: dt.date | None = None) -> float | None:
    """The rate the cadence names, or None when it names none ("irregular", "as required")."""
    t = text.strip().lower()
    if _ended(t, today or dt.date.today()):
        return 0.0
    for pattern, n in RATES:
        if re.search(pattern, t):
            return float(n)
    return float(FEED_MAX) if feed else None


# Cadences that name no rate but whose rate is known from the store's history, or whose rate
# above is an upper bound the hubs must not promise.
KAGGLE_KNOWN = {
    "as the police database changes": "monthly",
    "weekly in the sampling season": "monthly",
}
# Some number of times a year above one: the cost counts it as quarterly, Kaggle as annually.
KAGGLE_SLOWER = re.compile(r"several times a year")


def kaggle_frequency(text: str) -> str:
    """Kaggle's fixed choices. A rate between two takes the slower one, so a page never promises
    updates more often than the publisher makes them.
    """
    if known := KAGGLE_KNOWN.get(text.strip().lower()):
        return known
    n = per_year(text)
    if n is None or (n and KAGGLE_SLOWER.search(text.lower())):
        return "annually"
    for floor, name in ((365, "daily"), (52, "weekly"), (12, "monthly"), (4, "quarterly")):
        if n >= floor:
            return name
    return "never" if n == 0 else "annually"
