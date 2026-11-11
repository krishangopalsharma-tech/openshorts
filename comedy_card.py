"""Twitter/X post-style comedy video card renderer and FFmpeg compositor.

Renders a 1080x1920 (9:16) white card with:
  - Top-left circular channel profile avatar
  - Bold channel display name + blue verified checkmark badge + @handle
  - Left-aligned POV / comedy caption text (with inline color emoji support)
  - Rounded-corner square-ish (4:5) transparent cutout window where the
    face-tracked / split-screen reframed video plays.
"""
import json
import os
import subprocess
import tempfile
import uuid
from PIL import Image, ImageChops, ImageDraw, ImageFont

from ffmpeg_utils import METADATA_SCRUB, QUALITY, video_encode_args
from hooks import (
    _break_long_word,
    _draw_mixed,
    _load_emoji_font,
    _measure_width,
)

CANVAS_W = 1080
CANVAS_H = 1920

# Video window geometry matching the Twitter/X comedy reference card
BOX_W = 992
BOX_X = (CANVAS_W - BOX_W) // 2  # 44 px white margin on left and right
DEFAULT_BOX_Y = 468
DEFAULT_BOX_H = 1220
CORNER_RADIUS = 18

# Colors
WHITE = (255, 255, 255, 255)
TEXT_INK = (15, 20, 25, 255)       # Twitter/X #0F1419
TEXT_MUTED = (83, 100, 113, 255)   # Twitter/X #536471
VERIFIED_BLUE = (29, 155, 240, 255)  # Twitter/X #1D9BF0

_BOLD_FONT_CANDIDATES = [
    "C:\\Windows\\Fonts\\segoeuib.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    os.path.join("fonts", "Montserrat-ExtraBold.ttf"),
    os.path.join("fonts", "Montserrat.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

_REGULAR_FONT_CANDIDATES = [
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    os.path.join("fonts", "Montserrat.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _load_font(candidates, size):
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(draw, text, font, emoji_font, max_width, max_lines=3):
    """Wrap text into lines that fit within max_width."""
    raw_lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not raw_lines:
        return []
    lines = []
    for raw in raw_lines:
        words = raw.split()
        current = ""
        for word in words:
            if _measure_width(draw, word, font, emoji_font) > max_width:
                if current:
                    lines.append(current)
                    current = ""
                pieces = _break_long_word(draw, word, font, emoji_font, max_width)
                lines.extend(pieces[:-1])
                current = pieces[-1] if pieces else ""
                continue
            candidate = f"{current} {word}".strip() if current else word
            if _measure_width(draw, candidate, font, emoji_font) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines[:max_lines]


def _render_avatar(size, avatar_path=None, channel_name="C"):
    """Return an RGBA circular avatar of (size, size)."""
    scale = 4
    big = size * scale
    mask = Image.new("L", (big, big), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.ellipse((0, 0, big - 1, big - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)

    src = None
    if avatar_path and os.path.exists(avatar_path):
        try:
            raw = Image.open(avatar_path).convert("RGBA")
            rw, rh = raw.size
            side = min(rw, rh)
            left = (rw - side) // 2
            top = (rh - side) // 2
            src = raw.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
        except Exception:
            src = None

    if src is None:
        src = Image.new("RGBA", (size, size), (29, 155, 240, 255))
        sdraw = ImageDraw.Draw(src)
        initial = (channel_name or "C").strip()[:1].upper() or "C"
        font = _load_font(_BOLD_FONT_CANDIDATES, int(size * 0.48))
        bbox = sdraw.textbbox((0, 0), initial, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        sdraw.text(
            ((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
            initial,
            font=font,
            fill=(255, 255, 255, 255),
        )

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(src, (0, 0), mask)
    return out


def _render_verified_badge(size=38):
    """Return an anti-aliased Twitter/X blue verified checkmark badge."""
    scale = 4
    s = size * scale
    badge = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(badge)

    pad = int(s * 0.06)
    draw.ellipse((pad, pad, s - pad - 1, s - pad - 1), fill=VERIFIED_BLUE)

    # Crisp white checkmark inside the blue circle
    p1 = (int(s * 0.28), int(s * 0.52))
    p2 = (int(s * 0.44), int(s * 0.67))
    p3 = (int(s * 0.73), int(s * 0.35))
    stroke = max(4, int(s * 0.11))
    draw.line([p1, p2, p3], fill=(255, 255, 255, 255), width=stroke, joint="curve")
    r = stroke // 2
    for px, py in (p1, p2, p3):
        draw.ellipse((px - r, py - r, px + r, py + r), fill=(255, 255, 255, 255))

    return badge.resize((size, size), Image.LANCZOS)


def render_card_overlay(
    output_png,
    channel_name="Cheerful Videos",
    handle="@Cheerfulvids",
    pov_text="",
    verified=True,
    avatar_path=None,
):
    """Render the 1080x1920 RGBA Twitter/X comedy card PNG with a transparent
    rounded-corner cutout for the video frame.

    Returns ``{"x": int, "y": int, "w": int, "h": int, "radius": int}``
    describing the video cutout window.
    """
    card = Image.new("RGBA", (CANVAS_W, CANVAS_H), WHITE)
    draw = ImageDraw.Draw(card)

    header_x = 76
    avatar_y = 196
    avatar_size = 112

    # 1. Circular Profile Avatar
    avatar_img = _render_avatar(avatar_size, avatar_path=avatar_path, channel_name=channel_name)
    card.alpha_composite(avatar_img, (header_x, avatar_y))

    # 2. Bold Channel Name + Verified Badge + @handle
    name_font = _load_font(_BOLD_FONT_CANDIDATES, 42)
    handle_font = _load_font(_REGULAR_FONT_CANDIDATES, 33)
    pov_font = _load_font(_REGULAR_FONT_CANDIDATES, 44)

    name_emoji_font = _load_emoji_font(42)
    pov_emoji_font = _load_emoji_font(44)

    text_x = header_x + avatar_size + 24
    name_y = avatar_y + 8
    clean_name = (channel_name or "Comedy Clips").strip()
    _draw_mixed(card, draw, (text_x, name_y), clean_name, name_font, name_emoji_font, TEXT_INK)

    if verified:
        name_w = int(_measure_width(draw, clean_name, name_font, name_emoji_font))
        badge_size = 38
        badge_img = _render_verified_badge(badge_size)
        badge_x = min(CANVAS_W - header_x - badge_size, text_x + name_w + 10)
        badge_y = name_y + 6
        card.alpha_composite(badge_img, (badge_x, badge_y))

    clean_handle = (handle or "").strip()
    if clean_handle and not clean_handle.startswith("@"):
        clean_handle = "@" + clean_handle
    if clean_handle:
        handle_y = name_y + 54
        draw.text((text_x, handle_y), clean_handle, font=handle_font, fill=TEXT_MUTED)

    # 3. POV / Comedy Caption Text below the profile header
    pov_y = avatar_y + avatar_size + 38
    max_pov_w = CANVAS_W - header_x * 2
    pov_lines = _wrap_text(draw, pov_text, pov_font, pov_emoji_font, max_pov_w, max_lines=3)
    line_h = 56
    for idx, line in enumerate(pov_lines):
        _draw_mixed(
            card,
            draw,
            (header_x, pov_y + idx * line_h),
            line,
            pov_font,
            pov_emoji_font,
            TEXT_INK,
        )

    # 4. Rounded-corner square-ish (4:5) video window cutout
    if pov_lines:
        box_y = pov_y + len(pov_lines) * line_h + 28
    else:
        box_y = avatar_y + avatar_size + 44
    box_y = max(DEFAULT_BOX_Y, box_y)
    if box_y % 2 == 1:
        box_y += 1

    box_w = BOX_W
    box_x = BOX_X
    box_h = min(DEFAULT_BOX_H, CANVAS_H - box_y - 160)
    if box_h % 2 == 1:
        box_h -= 1

    # Supersampled rounded rectangle alpha cutout so corners are anti-aliased
    ss = 4
    hole = Image.new("L", (box_w * ss, box_h * ss), 255)
    hdraw = ImageDraw.Draw(hole)
    hdraw.rounded_rectangle(
        (0, 0, box_w * ss - 1, box_h * ss - 1),
        radius=CORNER_RADIUS * ss,
        fill=0,
    )
    hole = hole.resize((box_w, box_h), Image.LANCZOS)

    alpha = card.getchannel("A")
    alpha.paste(hole, (box_x, box_y))
    card.putalpha(alpha)

    os.makedirs(os.path.dirname(os.path.abspath(output_png)), exist_ok=True)
    card.save(output_png, "PNG")
    return {
        "x": box_x,
        "y": box_y,
        "w": box_w,
        "h": box_h,
        "radius": CORNER_RADIUS,
    }


def build_ffmpeg_filtergraph(box):
    """Build the FFmpeg -filter_complex string that places the reframed video
    inside ``box`` and composites the 1080x1920 RGBA card PNG over it."""
    x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    return (
        f"color=c=white:s={CANVAS_W}x{CANVAS_H}[bg];"
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{h}:(iw-{w})/2:(ih-{h})/2,setsar=1[vid];"
        f"[bg][vid]overlay={x}:{y}:shortest=1[base];"
        f"[base][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
    )


def apply_comedy_card(
    video_path,
    output_path=None,
    channel_name="Cheerful Videos",
    handle="@Cheerfulvids",
    pov_text="",
    verified=True,
    avatar_path=None,
):
    """Composite ``video_path`` into the Twitter/X comedy layout card.

    When ``output_path`` is None or equals ``video_path``, replaces
    ``video_path`` atomically in place.
    """
    in_place = not output_path or os.path.abspath(output_path) == os.path.abspath(video_path)
    out_dir = os.path.dirname(os.path.abspath(output_path or video_path))
    uid = uuid.uuid4().hex[:8]
    card_png = os.path.join(out_dir, f".comedy_card_{uid}.png")
    target_path = (
        os.path.join(out_dir, f".comedy_out_{uid}.mp4")
        if in_place
        else output_path
    )

    try:
        box = render_card_overlay(
            card_png,
            channel_name=channel_name,
            handle=handle,
            pov_text=pov_text,
            verified=verified,
            avatar_path=avatar_path,
        )
        fg = build_ffmpeg_filtergraph(box)
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            video_path,
            "-i",
            card_png,
            "-filter_complex",
            fg,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            *video_encode_args(QUALITY),
            "-c:a",
            "copy",
            *METADATA_SCRUB,
            "-movflags",
            "+faststart",
            target_path,
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if in_place:
            os.replace(target_path, video_path)
            return video_path
        return target_path
    finally:
        for tmp in (card_png, target_path if in_place else None):
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
