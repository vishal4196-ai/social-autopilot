"""Hybrid post-image generator: pre-rendered AI backgrounds + Pillow text overlay.

Why this design:
- AI-generated text in images is unreliable (mangled letters, off-brand fonts).
- AI-generated visuals scroll-stop better than solid colors.
- So: AI for the BG (one-time generation, bundled in repo), Pillow for the
  TEXT (every post, pixel-perfect, fully on-brand).

Layout (1376x768 = 16:9, ~Twitter/LinkedIn standard):
  [ AI BG image ]
  [ semi-transparent black gradient over bottom 60% ]
  [ small uppercase overline: CASE STUDY · LANDSCAPING / HOT TAKE / etc. ]
  [ thin red accent bar | BIG HEADLINE ]
  [                     | smaller sub-line ]
  [ small footer left: UpliftAI.co        right: @Vishalaii ]

Output is saved to /data/images/<uuid>.png (Railway volume) and served
via the /images/<filename> route. If /data doesn't exist (no volume),
falls back to a tmp dir inside the container — ephemeral but works for
the single publish call.
"""
from __future__ import annotations

import logging
import os
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .. import config

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
BACKGROUNDS_DIR = ROOT / "web" / "static" / "backgrounds"


def _output_dir() -> Path:
    """Where generated PNGs land. Prefer /data (volume), fall back to /tmp."""
    candidate = Path(config.DB_PATH).parent / "images" if config.DB_PATH else None
    for d in (candidate, Path("/data/images"), Path("/tmp/vai-images")):
        if d is None:
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    # Last resort: project root
    fallback = ROOT.parent / "_images"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


OUTPUT_DIR = _output_dir()


# ── Brand constants ───────────────────────────────────────────────

CANVAS_W, CANVAS_H = 1376, 768  # 16:9 at ~720p
ACCENT_RED = (204, 0, 0)
WHITE = (245, 245, 245)
MUTED = (180, 180, 180)
DIM = (130, 130, 130)


# ── Font loader (graceful fallback) ───────────────────────────────

def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Try a few common font paths; fall back to PIL's default if missing."""
    candidates = [
        # Linux (Railway uses Debian-based images, fonts come with fonts-dejavu)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        # Windows (local dev)
        "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    log.warning("No TTF font found, falling back to PIL default (size %d won't match)", size)
    return ImageFont.load_default()


# ── Background picker ─────────────────────────────────────────────

def _list_backgrounds() -> list[Path]:
    if not BACKGROUNDS_DIR.exists():
        return []
    return sorted(p for p in BACKGROUNDS_DIR.glob("*.png"))


def pick_background(topic_hint: str = "") -> Path:
    """Pick a background. Naive keyword match for now; can be Claude-driven later."""
    bgs = _list_backgrounds()
    if not bgs:
        raise FileNotFoundError(f"No backgrounds in {BACKGROUNDS_DIR}")
    hint = (topic_hint or "").lower()
    # Keyword affinity
    for kw, slug in [
        ("landsc", "landscape"),
        ("lawn", "landscape"),
        ("clean", "landscape"),
        ("hvac", "landscape"),
        ("chatgpt", "search"),
        ("perplexity", "search"),
        ("rank", "search"),
        ("google", "search"),
        ("ai search", "search"),
    ]:
        if kw in hint:
            for bg in bgs:
                if slug in bg.stem:
                    return bg
    # Otherwise alternate by hash of hint for visual variety
    return bgs[hash(hint) % len(bgs)]


# ── Text helpers ──────────────────────────────────────────────────

def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Word-wrap to max_width pixels for the given font."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        trial = (current + " " + w).strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _shrink_to_fit(
    text: str,
    max_w: int,
    max_lines: int,
    start_size: int,
    min_size: int,
    bold: bool,
    draw: ImageDraw.ImageDraw,
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """Reduce font size until the wrapped text fits within max_lines."""
    size = start_size
    while size >= min_size:
        font = _load_font(size, bold=bold)
        lines = _wrap(text, font, max_w, draw)
        if len(lines) <= max_lines:
            return lines, font
        size -= 4
    # Couldn't fit — return what we have at min_size, truncated
    font = _load_font(min_size, bold=bold)
    lines = _wrap(text, font, max_w, draw)[:max_lines]
    return lines, font


# ── Public API ────────────────────────────────────────────────────

@dataclass
class ImageResult:
    filename: str   # just the basename (used in /images/<filename> URL)
    full_path: str  # absolute path on disk


def generate_post_image(
    headline: str,
    subline: str | None = None,
    overline: str = "CASE STUDY",
    topic_hint: str = "",
) -> ImageResult:
    """Render one branded post image. Returns local file path + filename."""
    bg_path = pick_background(topic_hint)
    bg = Image.open(bg_path).convert("RGBA")
    if bg.size != (CANVAS_W, CANVAS_H):
        bg = bg.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)

    # Slight blur on the bg behind the text region for legibility
    bg_blurred = bg.filter(ImageFilter.GaussianBlur(radius=2))
    bg.paste(bg_blurred, (0, int(CANVAS_H * 0.35)), bg_blurred.split()[3]) if bg_blurred.mode == "RGBA" else None

    # Gradient overlay (bottom ~65% darkens for text legibility)
    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    grad_top = int(CANVAS_H * 0.35)
    for y in range(grad_top, CANVAS_H):
        # 0 alpha at top of gradient, ~200 at bottom
        progress = (y - grad_top) / (CANVAS_H - grad_top)
        alpha = int(40 + (215 - 40) * progress)
        odraw.line([(0, y), (CANVAS_W, y)], fill=(10, 10, 10, alpha))
    composed = Image.alpha_composite(bg, overlay)

    draw = ImageDraw.Draw(composed)

    # Layout zones (within an 80px padding box)
    pad_x = 80
    pad_y = 70
    accent_x = pad_x  # left edge of red accent
    text_x = pad_x + 24  # text starts after accent bar
    text_w = CANVAS_W - text_x - pad_x

    # Overline (small uppercase, muted)
    overline_font = _load_font(20, bold=True)
    overline_text = overline.upper()
    overline_y = CANVAS_H - pad_y - 300

    # Headline (big, bold, white) — shrink to fit within max 3 lines
    headline_lines, headline_font = _shrink_to_fit(
        headline, max_w=text_w, max_lines=3, start_size=72, min_size=44, bold=True, draw=draw,
    )

    # Subline (medium weight, muted-white) — optional, shrink to fit 2 lines
    subline_lines: list[str] = []
    subline_font = _load_font(28, bold=False)
    if subline:
        subline_lines, subline_font = _shrink_to_fit(
            subline, max_w=text_w, max_lines=2, start_size=34, min_size=22, bold=False, draw=draw,
        )

    # Compute total text-block height and anchor from bottom up
    line_gap = 8
    headline_h = sum(
        (draw.textbbox((0, 0), L, font=headline_font)[3] - draw.textbbox((0, 0), L, font=headline_font)[1])
        for L in headline_lines
    ) + line_gap * (len(headline_lines) - 1)
    subline_h = 0
    if subline_lines:
        subline_h = sum(
            (draw.textbbox((0, 0), L, font=subline_font)[3] - draw.textbbox((0, 0), L, font=subline_font)[1])
            for L in subline_lines
        ) + line_gap * (len(subline_lines) - 1)

    overline_bbox = draw.textbbox((0, 0), overline_text, font=overline_font)
    overline_h = overline_bbox[3] - overline_bbox[1]

    block_h = overline_h + 18 + headline_h + (16 + subline_h if subline_lines else 0)
    block_top = CANVAS_H - pad_y - block_h - 60  # 60 = footer space

    # Draw overline
    y = block_top
    draw.text((text_x, y), overline_text, font=overline_font, fill=DIM)
    y += overline_h + 18

    # Draw accent bar to the left of headline
    headline_start_y = y
    headline_end_y = y + headline_h
    draw.rectangle(
        [(accent_x, headline_start_y - 4), (accent_x + 4, headline_end_y + 4)],
        fill=ACCENT_RED,
    )

    # Draw headline lines
    for line in headline_lines:
        bbox = draw.textbbox((0, 0), line, font=headline_font)
        draw.text((text_x, y), line, font=headline_font, fill=WHITE)
        y += (bbox[3] - bbox[1]) + line_gap

    # Subline
    if subline_lines:
        y += 16 - line_gap
        for line in subline_lines:
            bbox = draw.textbbox((0, 0), line, font=subline_font)
            draw.text((text_x, y), line, font=subline_font, fill=MUTED)
            y += (bbox[3] - bbox[1]) + line_gap

    # Footer (UpliftAI.co left, @Vishalaii right)
    footer_font = _load_font(22, bold=True)
    footer_y = CANVAS_H - pad_y - 16
    brand_text = "UpliftAI.co"
    handle_text = "@Vishalaii"
    draw.text((pad_x, footer_y), brand_text, font=footer_font, fill=WHITE)
    handle_bbox = draw.textbbox((0, 0), handle_text, font=footer_font)
    draw.text(
        (CANVAS_W - pad_x - (handle_bbox[2] - handle_bbox[0]), footer_y),
        handle_text, font=footer_font, fill=WHITE,
    )

    # Save
    filename = f"{uuid.uuid4().hex[:12]}.png"
    full_path = OUTPUT_DIR / filename
    composed.convert("RGB").save(full_path, format="PNG", optimize=True)
    log.info("Generated post image %s using bg=%s", full_path, bg_path.name)
    return ImageResult(filename=filename, full_path=str(full_path))
