import dataclasses
import hashlib
import json
import re
import shutil
import subprocess

import pytest

from publicdata import brand, store
from publicdata.__main__ import main
from publicdata.cache import BuildCache
from publicdata.gate import _png_size

from .conftest import ROOT


def test_the_site_fonts_load_from_woff2():
    for name in (brand.REGULAR, brand.MEDIUM, brand.BOLD):
        assert brand._font(name, 20).getname()[0].startswith("Random Grotesque")


def test_the_repository_carries_only_the_free_fonts_with_their_attribution():
    # RM-EULA Type-C 4.3 covers the free package only; a paid style must never be committed.
    tracked = subprocess.run(
        ["git", "ls-files", "*.woff2", "*.woff", "*.ttf", "*.otf"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    fonts = "pipeline/publicdata/static/fonts/"
    assert set(tracked.split()) == {fonts + n for n in (brand.REGULAR, brand.MEDIUM, brand.BOLD)}
    assert (brand.FONTS / "RandomGrotesque-EULA.txt").is_file()


def test_cards_are_the_same_bytes_every_time(tmp_path):
    a = brand.dataset_card(
        tmp_path / "a", "og/d/s.png", "A title", "Queensland · Agency", ["3 rows"], publisher="A"
    )
    b = brand.dataset_card(
        tmp_path / "b", "og/d/s.png", "A title", "Queensland · Agency", ["3 rows"], publisher="A"
    )
    assert a.url == b.url
    assert (tmp_path / "a" / a.path).read_bytes() == (tmp_path / "b" / b.path).read_bytes()
    assert _png_size(tmp_path / "a" / a.path) == (brand.CARD_W, brand.CARD_H)


def test_a_title_too_long_for_three_lines_is_cut_at_a_word(tmp_path):
    long = " ".join(["Consultancies and contractors engaged by the Commissioner"] * 6)
    c = brand.dataset_card(
        tmp_path, "og/d/s.png", long, "Commonwealth · " + "Office " * 40, [], publisher="O"
    )
    assert _png_size(tmp_path / c.path) == (brand.CARD_W, brand.CARD_H)
    _, lines = brand._fit(long, brand.BOLD, range(72, 47, -4), 1056, 3)
    assert len(lines) == 3
    assert lines[-1].endswith("…")
    assert " …" not in lines[-1]


def test_every_page_names_its_card_and_the_manifest_its_icons(fixture_site):
    out = fixture_site
    slug = min(p.name for p in (out / "d").iterdir() if (p / "index.html").exists())
    ds = (out / "d" / slug / "index.html").read_text(encoding="utf-8")
    assert re.search(rf'og:image" content="https://publicdata.au/og/d/{slug}.png\?v=\w{{12}}"', ds)
    home = (out / "index.html").read_text(encoding="utf-8")
    assert 'og:image" content="https://publicdata.au/og/site.png?v=' in home
    assert f"<h1>{brand.HEADLINE}</h1>" in home
    assert f"# {brand.HEADLINE}\n" in (out / "index.md").read_text(encoding="utf-8")
    _, lines = brand._fit(
        brand.HEADLINE, brand.BOLD, range(72, 47, -4), brand.CARD_W - 2 * brand.PAD, 2
    )
    assert not lines[-1].endswith("…")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in home
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in home
    m = json.loads((out / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["display"] == "standalone"
    assert m["start_url"] == "/"
    assert {i["src"] for i in m["icons"]} >= {"/icon-192.png", "/icon-512.png"}
    assert _png_size(out / "apple-touch-icon.png") == (180, 180)
    assert (out / "favicon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"
    assert "/manifest.webmanifest\n  Content-Type: application/manifest+json" in (
        out / "_headers"
    ).read_text(encoding="utf-8")


def test_an_older_version_page_carries_its_own_card(fixture_store, tmp_path):
    s = tmp_path / "store"
    shutil.copytree(fixture_store, s)
    slug = "qld-road-casualties"
    first = store.manifests(s, slug)[0]
    data = b"".join(store.source_path(s, first).read_bytes().splitlines(keepends=True)[:101])
    newer = dataclasses.replace(
        first,
        version="2026-05-24",
        fetched_at="2026-09-25T00:00:00+00:00",
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
    )
    store.write(s, newer, data)
    out = tmp_path / "dist"
    assert main(["build", "--store", str(s), "--out", str(out)]) == 0

    def card(rel: str) -> str:
        page = (out / rel).read_text(encoding="utf-8")
        return re.search(r'og:image" content="https://publicdata.au/([^"?]+)', page).group(1)

    assert card(f"d/{slug}/index.html") == f"og/d/{slug}.png"
    assert card(f"d/{slug}/v/2026-05-24/index.html") == f"og/d/{slug}.png"
    old = card(f"d/{slug}/v/{first.version}/index.html")
    assert old == f"og/d/{slug}/v/{first.version}.png"
    assert (out / old).read_bytes() != (out / f"og/d/{slug}.png").read_bytes()


def test_a_card_is_drawn_once_and_again_only_when_its_words_change(tmp_path, monkeypatch):
    drawn = []
    real = brand._card
    monkeypatch.setattr(brand, "_card", lambda *a: drawn.append(a) or real(*a))
    cache = BuildCache(tmp_path / "cache")
    args = ("og/d/s.png", "A title", "Queensland · Agency", ["3 rows"])
    first = brand.dataset_card(tmp_path / "a", *args, publisher="A", cache=cache)
    again = brand.dataset_card(tmp_path / "b", *args, publisher="A", cache=cache)
    assert len(drawn) == 1
    assert first.url == again.url
    assert (tmp_path / "a" / first.path).read_bytes() == (tmp_path / "b" / again.path).read_bytes()
    renamed = brand.dataset_card(
        tmp_path / "c", "og/d/s.png", "A new title", *args[2:], publisher="A", cache=cache
    )
    assert len(drawn) == 2
    assert renamed.url != first.url


def test_card_entries_outlast_the_first_prune_and_the_last_drops_the_unused(
    fixture_store, tmp_path
):
    cache = tmp_path / "cache"
    run = ["build", "--store", str(fixture_store), "--cache", str(cache)]
    assert main([*run, "--out", str(tmp_path / "a"), "--absent", str(tmp_path / "a.json")]) == 0
    cards = {p.name: p.joinpath("meta.json").stat().st_ino for p in cache.glob("card-*")}
    datasets = {p.parent.parent.name for p in fixture_store.glob("*/*/manifest.json")} - {
        "catalogue"
    }
    assert len(cards) == 1 + len(datasets)  # the site card and one per fixture dataset
    stale = cache / "card-stale"
    (stale / "files").mkdir(parents=True)
    (stale / "meta.json").write_text("{}")
    assert main([*run, "--out", str(tmp_path / "b"), "--absent", str(tmp_path / "b.json"), "--published", str(tmp_path / "a")]) == 0  # fmt: skip
    assert {p.name: p.joinpath("meta.json").stat().st_ino for p in cache.glob("card-*")} == cards
