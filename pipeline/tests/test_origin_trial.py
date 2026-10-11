import base64
import datetime as dt
import json

import pytest

from publicdata.site import ORIGIN_TRIAL

# origin-trial.yml also runs this file weekly, so the warning comes with no pull request open.
NOTICE = dt.timedelta(days=14)
RENEW = (
    "Renew the WebMCP token for https://publicdata.au at "
    "https://developer.chrome.com/origintrials/ and replace ORIGIN_TRIAL in "
    "pipeline/publicdata/site.py."
)


def _payload(token: str) -> dict[str, object]:
    # A token is a version byte, a 64-byte signature, a 4-byte big-endian length and the JSON.
    raw = base64.b64decode(token, validate=True)
    assert raw[0] in {2, 3}, f"unknown origin-trial token version {raw[0]}"
    size = int.from_bytes(raw[65:69], "big")
    assert len(raw) == 69 + size, "origin-trial token length does not match its payload"
    payload: dict[str, object] = json.loads(raw[69:])
    return payload


def _expiry(payload: dict[str, object]) -> dt.datetime:
    expiry = payload.get("expiry")
    assert isinstance(expiry, int), "the token carries no expiry"
    return dt.datetime.fromtimestamp(expiry, dt.UTC)


def _problems(token: str, now: dt.datetime) -> list[str]:
    p = _payload(token)
    problems = []
    if p.get("origin") != "https://publicdata.au:443":
        problems.append(
            f"the token's origin is {p.get('origin')} and this site needs https://publicdata.au:443"
        )
    if p.get("feature") != "WebMCP":
        problems.append(f"the token's feature is {p.get('feature')} and this site needs WebMCP")
    ends = _expiry(p)
    if now >= ends - NOTICE:
        problems.append(
            f"the token expires {ends:%d %b %Y %H:%M} UTC, and no WebMCP tool registers after that"
        )
    return problems


def test_the_webmcp_origin_trial_token_is_for_this_site_and_not_about_to_expire() -> None:
    problems = _problems(ORIGIN_TRIAL, dt.datetime.now(dt.UTC))
    assert not problems, "; ".join(problems) + ". " + RENEW


def test_the_check_fails_from_fourteen_days_before_expiry() -> None:
    ends = _expiry(_payload(ORIGIN_TRIAL))
    assert _problems(ORIGIN_TRIAL, ends - NOTICE - dt.timedelta(seconds=1)) == []
    assert _problems(ORIGIN_TRIAL, ends - NOTICE) == [
        f"the token expires {ends:%d %b %Y %H:%M} UTC, and no WebMCP tool registers after that"
    ]


def test_the_check_fails_a_token_for_another_origin_or_feature() -> None:
    raw = bytearray(base64.b64decode(ORIGIN_TRIAL))
    body = json.loads(raw[69:])
    body |= {"origin": "https://example.com:443", "feature": "Other"}
    data = json.dumps(body).encode()
    forged = base64.b64encode(bytes(raw[:65]) + len(data).to_bytes(4, "big") + data).decode()
    problems = _problems(forged, dt.datetime(2000, 1, 1, tzinfo=dt.UTC))
    assert problems == [
        "the token's origin is https://example.com:443 and this site needs https://publicdata.au:443",
        "the token's feature is Other and this site needs WebMCP",
    ]


def test_a_token_that_is_not_an_origin_trial_token_fails() -> None:
    with pytest.raises(AssertionError, match="length"):
        _payload(base64.b64encode(b"\x02" + bytes(70)).decode())
