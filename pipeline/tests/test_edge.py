from typing import ClassVar

from publicdata import edge


def test_purge_sends_prefixes_on_the_host_in_batches() -> None:
    class Resp:
        def __init__(self, body: dict[str, object]) -> None:
            self.body, self.ok = body, True

        def json(self) -> dict[str, object]:
            return self.body

        def raise_for_status(self) -> None:
            pass

    class Session:
        posts: ClassVar[list[tuple[str, list[str]]]] = []

        def get(self, url: str, **kw: object) -> Resp:
            return Resp({"result": [{"id": "z1"}]})

        def post(self, url: str, json: dict[str, list[str]], **kw: object) -> Resp:
            self.posts.append((url, json["prefixes"]))
            return Resp({"success": True})

    s = Session()
    prefixes = [f"d/x/v/2026-01-{i:02d}/" for i in range(1, 32)]
    assert edge.purge(prefixes, "t", session=s) == 31  # type: ignore[arg-type]  # a fake session
    assert [len(p) for _, p in s.posts] == [30, 1]
    assert s.posts[0][0].endswith("/zones/z1/purge_cache")
    assert s.posts[0][1][0] == "publicdata.au/d/x/v/2026-01-01/"


def test_a_version_prefix_also_purges_its_query_api_answers():
    assert edge.with_answers(["d/x-y/v/2026-01-02/"]) == [
        "d/x-y/v/2026-01-02/",
        "api/v1/datasets/x-y/versions/2026-01-02/",
    ]
