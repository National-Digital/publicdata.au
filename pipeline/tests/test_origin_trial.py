import base64
import binascii
import datetime as dt
import json
import os

import pytest
import yaml

from publicdata.site import ORIGIN_TRIAL

from .conftest import ROOT

# CI fails from 14 days before expiry. origin-trial.yml raises the notice so its weekly run opens
# an issue at least two runs before pull requests start to fail.
NOTICE_DAYS = "ORIGIN_TRIAL_NOTICE_DAYS"
CI_NOTICE = 14
RENEW = (
    "Renew the WebMCP token for https://publicdata.au at "
    "https://developer.chrome.com/origintrials/ and replace ORIGIN_TRIAL in "
    "pipeline/publicdata/site.py."
)
# A token is a version byte, a signature, a big-endian payload length and the JSON payload.
VERSIONS = {2, 3}
SIGNATURE = 64
LENGTH = 4
HEADER = 1 + SIGNATURE + LENGTH
WORKFLOW = ROOT / ".github/workflows/origin-trial.yml"


class TokenError(ValueError):
    pass


def _payload(token: str) -> dict[str, object]:
    try:
        raw = base64.b64decode(token, validate=True)
    except binascii.Error as e:
        msg = f"it is not base64 ({e})"
        raise TokenError(msg) from e
    if len(raw) < HEADER or raw[0] not in VERSIONS:
        msg = "it does not start with an origin-trial token header"
        raise TokenError(msg)
    size = int.from_bytes(raw[1 + SIGNATURE : HEADER], "big")
    if len(raw) != HEADER + size:
        msg = "its length does not match its payload"
        raise TokenError(msg)
    try:
        payload = json.loads(raw[HEADER:])
    except ValueError as e:
        msg = f"its payload is not JSON ({e})"
        raise TokenError(msg) from e
    if not isinstance(payload, dict):
        msg = "its payload is not a JSON object"
        raise TokenError(msg)
    return payload


def _expiry(payload: dict[str, object]) -> dt.datetime:
    expiry = payload.get("expiry")
    if type(expiry) is not int:
        msg = "its payload has no expiry in epoch seconds"
        raise TokenError(msg)
    return dt.datetime.fromtimestamp(expiry, dt.UTC)


def _notice() -> dt.timedelta:
    return dt.timedelta(days=int(os.environ.get(NOTICE_DAYS, CI_NOTICE)))


def _problems(token: str, now: dt.datetime, notice: dt.timedelta) -> list[str]:
    try:
        p = _payload(token)
        ends = _expiry(p)
    except TokenError as e:
        return [f"ORIGIN_TRIAL does not decode as an origin-trial token: {e}"]
    problems = []
    if p.get("origin") != "https://publicdata.au:443":
        problems.append(
            f"the token's origin is {p.get('origin')} and this site needs https://publicdata.au:443"
        )
    if p.get("feature") != "WebMCP":
        problems.append(f"the token's feature is {p.get('feature')} and this site needs WebMCP")
    if now >= ends - notice:
        problems.append(
            f"the token expires {ends:%d %b %Y %H:%M} UTC, and no WebMCP tool registers after that"
        )
    return problems


def _token(payload: bytes, version: int = 2, length: int | None = None) -> str:
    size = len(payload) if length is None else length
    raw = bytes([version]) + bytes(SIGNATURE) + size.to_bytes(LENGTH, "big") + payload
    return base64.b64encode(raw).decode()


def _forge(**changes: object) -> str:
    raw = base64.b64decode(ORIGIN_TRIAL)
    return _token(json.dumps(json.loads(raw[HEADER:]) | changes).encode(), version=raw[0])


def test_the_webmcp_origin_trial_token_is_for_this_site_and_not_about_to_expire() -> None:
    problems = _problems(ORIGIN_TRIAL, dt.datetime.now(dt.UTC), _notice())
    if problems:
        pytest.fail("; ".join(problems) + ". " + RENEW, pytrace=False)


@pytest.mark.parametrize("days", [CI_NOTICE, 28])
def test_the_check_fails_from_the_notice_before_expiry(days: int) -> None:
    ends = _expiry(_payload(ORIGIN_TRIAL))
    notice = dt.timedelta(days=days)
    assert _problems(ORIGIN_TRIAL, ends - notice - dt.timedelta(seconds=1), notice) == []
    assert _problems(ORIGIN_TRIAL, ends - notice, notice) == [
        f"the token expires {ends:%d %b %Y %H:%M} UTC, and no WebMCP tool registers after that"
    ]


def test_the_notice_is_14_days_unless_the_environment_widens_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(NOTICE_DAYS, raising=False)
    assert _notice() == dt.timedelta(days=14)
    monkeypatch.setenv(NOTICE_DAYS, "28")
    assert _notice() == dt.timedelta(days=28)


def test_the_weekly_run_warns_at_least_two_runs_before_pull_requests_fail() -> None:
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert int(wf["env"][NOTICE_DAYS]) >= CI_NOTICE + 14


def test_the_check_fails_a_token_for_another_origin_or_feature() -> None:
    forged = _forge(origin="https://example.com:443", feature="Other")
    assert _problems(forged, dt.datetime(2000, 1, 1, tzinfo=dt.UTC), _notice()) == [
        "the token's origin is https://example.com:443 and this site needs https://publicdata.au:443",
        "the token's feature is Other and this site needs WebMCP",
    ]


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        pytest.param("not base64!", "not base64", id="base64"),
        pytest.param(_token(b"{}", length=1), "length does not match", id="length"),
        pytest.param(_token(b"{}", version=9), "header", id="version"),
        pytest.param(_token(b"{"), "not JSON", id="json"),
        pytest.param(_token(b"[]"), "JSON object", id="object"),
        pytest.param(_token(b'{"expiry": true}'), "no expiry", id="bool-expiry"),
        pytest.param(_token(b'{"expiry": "1794873600"}'), "no expiry", id="string-expiry"),
    ],
)
def test_a_token_that_does_not_decode_is_reported_as_a_problem(token: str, reason: str) -> None:
    problems = _problems(token, dt.datetime(2000, 1, 1, tzinfo=dt.UTC), _notice())
    assert len(problems) == 1
    assert problems[0].startswith("ORIGIN_TRIAL does not decode as an origin-trial token: ")
    assert reason in problems[0]
