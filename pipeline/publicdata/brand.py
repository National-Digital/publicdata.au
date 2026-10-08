"""The mark, the app icons and the social cards, drawn from one set of colours and the site's fonts."""

from __future__ import annotations

import functools
import hashlib
import io
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import PIL
from PIL import Image, ImageDraw, ImageFont, features

from . import OPERATOR, SITE

if TYPE_CHECKING:
    from .cache import BuildCache

NAVY = "#040453"
ORANGE = "#f26324"
ORANGE_ON_NAVY = "#ff8b5a"
INK_ON_NAVY = "#f4f4f8"
MUTED_ON_NAVY = "#b4b5e6"
PAGE_LIGHT = "#fafafa"
PAGE_DARK = "#04041d"

# The mark on its 32 grid: a navy tile with an orange dot.
GRID = 32
TILE_RX = 6
DOT_R = 7

CARD_W, CARD_H = 1200, 630
PAD = 72
FONTS = Path(__file__).parent / "static" / "fonts"
REGULAR, MEDIUM, BOLD = (
    "RG-StandardRegular.woff2",
    "RG-StandardMedium.woff2",
    "RG-StandardBold.woff2",
)
SUPERSAMPLE = 8

NAME = SITE.replace("https://", "")
SHORT_NAME = "publicdata"
HEADLINE = "Australian government open data, in the format you and your AI need."
DESCRIPTION = "Australian government open data as dated versions that keep their content, with schema, provenance and diffs."


def favicon_svg() -> str:
    c = GRID / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}">'
        f'<rect width="{GRID}" height="{GRID}" rx="{TILE_RX}" fill="{NAVY}"/>'
        f'<circle cx="{c:g}" cy="{c:g}" r="{DOT_R}" fill="{ORANGE}"/></svg>\n'
    )


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONTS / name), size)
    except OSError as e:
        msg = f"{name}: Pillow's FreeType cannot read WOFF2 (needs Brotli)"
        raise RuntimeError(msg) from e


def _icon(size: int, *, rounded: bool, dot: float = DOT_R / GRID) -> Image.Image:
    """Drawn large and reduced, since Pillow does not anti-alias shapes."""
    big = size * SUPERSAMPLE
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if rounded:
        d.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * TILE_RX / GRID, fill=NAVY)
    else:
        d.rectangle((0, 0, big, big), fill=NAVY)
    r = big * dot
    d.ellipse((big / 2 - r, big / 2 - r, big / 2 + r, big / 2 + r), fill=ORANGE)
    return im.resize((size, size), Image.Resampling.LANCZOS)


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@dataclass(frozen=True)
class Card:
    path: str
    alt: str
    url: str


def _wordmark(d: ImageDraw.ImageDraw, x: float, y: float, size: int) -> float:
    """publicdata, the orange dot, au; as the header draws it. Returns the right edge."""
    f = _font(BOLD, size)
    track = -0.02 * size
    for part in ("publicdata", "au"):
        for ch in part:
            d.text((x, y), ch, font=f, fill=INK_ON_NAVY, anchor="ls")
            x += f.getlength(ch) + track
        if part == "publicdata":
            r = 0.21 * size
            x += 0.08 * size
            cy = y - 0.2 * size
            d.ellipse((x, cy - r, x + 2 * r, cy + r), fill=ORANGE)
            x += 2 * r + 0.08 * size
    return x


def _wrap(text: str, f: ImageFont.FreeTypeFont, width: float) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and f.getlength(f"{lines[-1]} {word}") <= width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines


def _fit(text: str, name: str, sizes: range, width: float, max_lines: int):
    for size in sizes:
        f = _font(name, size)
        lines = _wrap(text, f, width)
        if len(lines) <= max_lines and all(f.getlength(ln) <= width for ln in lines):
            return f, lines
    f = _font(name, sizes[-1])
    lines = _wrap(text, f, width)[:max_lines]
    while f.getlength(lines[-1] + "…") > width and " " in lines[-1]:
        lines[-1] = lines[-1].rsplit(" ", 1)[0]
    lines[-1] += "…"
    return f, lines


def _card(eyebrow: str, title: str, lede: str, facts: list[str], wordmark_size: int) -> bytes:
    im = Image.new("RGB", (CARD_W, CARD_H), NAVY)
    d = ImageDraw.Draw(im)
    width = CARD_W - 2 * PAD
    top = PAD + wordmark_size * 0.8
    _wordmark(d, PAD, top, wordmark_size)
    by = _font(REGULAR, 22)
    d.text((CARD_W - PAD, top), f"by {OPERATOR}", font=by, fill=MUTED_ON_NAVY, anchor="rs")

    y = top + 84
    if eyebrow:
        ef = _font(MEDIUM, 24)
        track = 1.5
        eyebrow = eyebrow.upper()
        eyebrow = _fit(eyebrow, MEDIUM, range(24, 23, -1), width - track * len(eyebrow), 1)[1][0]
        x = PAD
        for ch in eyebrow:
            d.text((x, y), ch, font=ef, fill=ORANGE_ON_NAVY, anchor="ls")
            x += ef.getlength(ch) + track
        y += 30
    tf, lines = _fit(title, BOLD, range(72, 47, -4), width, 2 if lede else 3)
    lh = round(tf.size * 1.12)
    y += lh - round(tf.size * 0.2)
    for ln in lines:
        d.text((PAD, y), ln, font=tf, fill=INK_ON_NAVY, anchor="ls")
        y += lh
    if lede:
        lf, llines = _fit(lede, REGULAR, range(32, 25, -2), width, 1)
        y += 8
        for ln in llines:
            d.text((PAD, y), ln, font=lf, fill=MUTED_ON_NAVY, anchor="ls")
            y += round(lf.size * 1.3)

    foot = CARD_H - PAD + 8
    ff = _font(REGULAR, 20)
    d.text(
        (PAD, foot),
        "An independent republication. No government agency has endorsed this site.",
        font=ff,
        fill=MUTED_ON_NAVY,
        anchor="ls",
    )
    if facts:
        xf = _font(MEDIUM, 28)
        while len(facts) > 1 and sum(map(xf.getlength, facts)) + 32 * (len(facts) - 1) > width:
            facts = facts[:-1]
        x, fy = PAD, foot - 48
        for i, fact in enumerate(facts):
            if i:
                d.ellipse((x + 12, fy - 13, x + 20, fy - 5), fill=ORANGE)
                x += 32
            d.text((x, fy), fact, font=xf, fill=INK_ON_NAVY, anchor="ls")
            x += xf.getlength(fact)
    d.rectangle((0, CARD_H - 10, CARD_W, CARD_H), fill=ORANGE)
    return _png(im)


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def _put(out: Path, rel: str, body: bytes) -> str:
    p = out / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return f"{SITE}/{rel}?v={hashlib.sha256(body).hexdigest()[:12]}"


# Card cache entries are named apart so the build's first prune, which knows only version keys,
# leaves them for the last one to judge.
CARD_PREFIX = "card-"


@functools.cache
def _fingerprint() -> str:
    """Everything besides a card's own words that decides its pixels."""
    h = hashlib.sha256(Path(__file__).read_bytes())
    for name in (REGULAR, MEDIUM, BOLD):
        h.update((FONTS / name).read_bytes())
    h.update(f"{PIL.__version__} {features.version('freetype2')} {OPERATOR}".encode())
    return h.hexdigest()


def _drawn(out: Path, rel: str, spec: list, cache: BuildCache | None) -> str:
    if cache is None:
        return _put(out, rel, _card(*spec))
    key = CARD_PREFIX + cache.key(_fingerprint(), json.dumps(spec, ensure_ascii=False))
    if cache.get(key) is not None:
        return _put(out, rel, (cache.root / key / "files" / "card.png").read_bytes())
    body = _card(*spec)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "card.png").write_bytes(body)
        cache.put(key, {}, Path(tmp))
    return _put(out, rel, body)


def site_card(out: Path, datasets: int, publishers: int, cache: BuildCache | None = None) -> Card:
    spec = [
        "",
        HEADLINE,
        "Every release is a dated version with its schema, provenance and a diff.",
        [_count(datasets, "dataset"), _count(publishers, "publisher")],
        64,
    ]
    rel = "og/site.png"
    return Card(rel, f"{NAME}: {HEADLINE}", _drawn(out, rel, spec, cache))


def dataset_card(
    out: Path,
    rel: str,
    title: str,
    eyebrow: str,
    facts: list[str],
    publisher: str,
    cache: BuildCache | None = None,
) -> Card:
    url = _drawn(out, rel, [eyebrow, title, "", facts, 44], cache)
    return Card(rel, f"{title}, from {publisher}, on {NAME}", url)


ICONS = (
    ("icon-192.png", 192, "any"),
    ("icon-512.png", 512, "any"),
    ("icon-maskable-512.png", 512, "maskable"),
)


def icons(out: Path) -> None:
    (out / "favicon.svg").write_text(favicon_svg(), encoding="utf-8")
    (out / "favicon-32.png").write_bytes(_png(_icon(32, rounded=True)))
    ico = [_icon(s, rounded=True) for s in (16, 32, 48)]
    buf = io.BytesIO()
    ico[-1].save(buf, "ICO", sizes=[(16, 16), (32, 32), (48, 48)], append_images=ico[:-1])
    (out / "favicon.ico").write_bytes(buf.getvalue())
    # iOS rounds the corners itself and shows transparency as black.
    (out / "apple-touch-icon.png").write_bytes(_png(_icon(180, rounded=False).convert("RGB")))
    for name, size, purpose in ICONS:
        im = _icon(size, rounded=purpose == "any")
        (out / name).write_bytes(_png(im if purpose == "any" else im.convert("RGB")))


def manifest(shortcuts: list[tuple[str, str]]) -> str:
    return (
        json.dumps(
            {
                "id": "/",
                "name": NAME,
                "short_name": SHORT_NAME,
                "description": DESCRIPTION,
                "lang": "en-AU",
                "dir": "ltr",
                "start_url": "/",
                "scope": "/",
                "display": "standalone",
                "background_color": PAGE_LIGHT,
                "theme_color": PAGE_LIGHT,
                "categories": ["government", "utilities"],
                "icons": [
                    {"src": "/favicon.svg", "type": "image/svg+xml", "sizes": "any"},
                    *(
                        {"src": f"/{n}", "type": "image/png", "sizes": f"{s}x{s}", "purpose": p}
                        for n, s, p in ICONS
                    ),
                ],
                "shortcuts": [
                    {
                        "name": name,
                        "url": url,
                        "icons": [
                            {"src": "/icon-192.png", "type": "image/png", "sizes": "192x192"}
                        ],
                    }
                    for name, url in shortcuts
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
