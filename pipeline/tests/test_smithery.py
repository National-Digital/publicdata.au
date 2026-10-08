from typing import TYPE_CHECKING, Any, Never

import requests

from publicdata import smithery
from publicdata.api_text import mcp_spec

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


def test_the_tools_and_listing_meet_every_smithery_rule_but_naming() -> None:
    assert smithery.problems() == []


def test_a_missing_output_schema_or_parameter_description_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = mcp_spec()
    t = spec["tools"][0]
    t.pop("outputSchema")
    p = next(iter(t["inputSchema"]["properties"]))
    t["inputSchema"]["properties"][p]["description"] = ""
    monkeypatch.setattr(smithery, "mcp_spec", lambda: spec)
    got = smithery.problems()
    assert f"{t['name']}: no outputSchema" in got
    assert f"{t['name']}: parameter {p} has no description" in got


class _Resp:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any] | None:
        return self.body


class _Smithery:
    """The two reads and the one write sync uses, over a dict that stands in for the listing."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state: dict[str, Any] = state
        self.patches: list[dict[str, Any]] = []

    def get(self, url: str, params: object = None, timeout: object = None) -> _Resp:
        if params:
            return _Resp({"servers": [{"qualifiedName": smithery.NAME, **self.state}]})
        return _Resp({k: v for k, v in self.state.items() if k != "homepage"})

    def patch(self, url: str, json: dict[str, Any], timeout: object = None) -> _Resp:
        self.patches.append(json)
        self.state.update(json)
        return _Resp()


def test_sync_writes_only_the_fields_that_drifted(monkeypatch: pytest.MonkeyPatch) -> None:
    want = smithery.listing()
    fake = _Smithery({**want, "description": "", "iconUrl": "https://example.test/icon"})
    monkeypatch.setattr(smithery, "_session", lambda key: fake)
    assert smithery.sync("key") == []
    assert fake.patches == [{"description": want["description"]}]
    assert smithery.sync("key") == []
    assert len(fake.patches) == 1


def test_sync_reports_a_field_the_write_did_not_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Smithery(
        {**smithery.listing(), "displayName": "x", "iconUrl": "https://example.test/i"}
    )
    monkeypatch.setattr(fake, "patch", lambda url, json, timeout=None: _Resp())
    monkeypatch.setattr(smithery, "_session", lambda key: fake)
    assert smithery.sync("key") == ["listing: displayName is still not what api.json says"]


def _raises(exc: BaseException) -> Callable[[str], Never]:
    def f(key: str) -> Never:
        raise exc

    return f


def test_a_4xx_fails_the_release_and_an_outage_only_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMITHERY_API_KEY", "key")
    bad = requests.Response()
    bad.status_code = 401
    monkeypatch.setattr(smithery, "sync", _raises(requests.HTTPError(response=bad)))
    assert smithery.main(["sync"]) == 1
    down = requests.Response()
    down.status_code = 503
    monkeypatch.setattr(smithery, "sync", _raises(requests.HTTPError(response=down)))
    assert smithery.main(["sync"]) == 0
    monkeypatch.setattr(smithery, "sync", _raises(requests.ConnectionError()))
    assert smithery.main(["sync"]) == 0
