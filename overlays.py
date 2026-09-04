"""Logo and custom-text overlays on a finished clip.

Everything the user places on the preview (logos from the asset library,
free text blocks) is rasterised with PIL into ONE full-frame RGBA image and
composited with a single ffmpeg ``overlay`` pass. Geometry travels as
fractions of the frame (centre x/y, width) so the same overlay list lands in
the same relative spot on a 9:16, 1:1 or 16:9 render of the clip, and
"apply to all clips" needs no per-clip adjustment. Text uses the bundled
caption fonts (``fonts/``, caption_styles.list_fonts) through hooks.py's
emoji-aware PIL drawing, so the burned result matches the browser preview
that uses the same @font-face files.

This is the ``ov_<hex>_`` layer in app.py's chain: above music,
below the (legacy) hook and the captions.
"""
import hashlib
import os
import re
import subprocess
import threading
import time
import uuid

from PIL import Image, ImageDraw, ImageFont

import caption_styles
from ffmpeg_utils import video_encode_args, QUALITY_FAST, METADATA_SCRUB, probe_dimensions

OVERLAYS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "overlays")
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 8
MAX_TEXT_CHARS = 200
MAX_TEXT_LINES = 4
ALIGNS = ("left", "center", "right")

_assets_cache = {"stamp": None, "assets": []}
_assets_lock = threading.Lock()
_HEX = re.compile(r"^#?[0-9a-fA-F]{6}$")


# --------------------------------------------------------------------------- #
# Asset library (logos)
# --------------------------------------------------------------------------- #
def list_assets(overlays_dir=None):
    """``[{file, name, width, height}]`` for every image in the library."""
    overlays_dir = overlays_dir or OVERLAYS_DIR
    try:
        names = sorted((n for n in os.listdir(overlays_dir)
                        if os.path.splitext(n)[1].lower() in ALLOWED_IMAGE_EXTS), key=str.lower)
        stamp = (overlays_dir, os.stat(overlays_dir).st_mtime_ns, tuple(names))
    except OSError:
        return []
    with _assets_lock:
        if _assets_cache["stamp"] == stamp:
            return [dict(a) for a in _assets_cache["assets"]]
        known = {a["file"]: a for a in _assets_cache["assets"]}
        assets = []
        for name in names:
            prev = known.get(name)
            if prev:
                assets.append(prev)
                continue
            try:
                with Image.open(os.path.join(overlays_dir, name)) as im:
                    w, h = im.size
            except Exception:
                continue
            label = re.sub(r"^[0-9a-f]{12}_", "", os.path.splitext(name)[0])
            assets.append({"file": name, "name": label or name, "width": w, "height": h})
        _assets_cache["stamp"] = stamp
        _assets_cache["assets"] = assets
        return [dict(a) for a in assets]


def resolve_asset(file, overlays_dir=None):
    overlays_dir = overlays_dir or OVERLAYS_DIR
    name = os.path.basename(str(file or ""))
    if not name or os.path.splitext(name)[1].lower() not in ALLOWED_IMAGE_EXTS:
        return None
    candidate = os.path.abspath(os.path.join(overlays_dir, name))
    if not candidate.startswith(os.path.abspath(overlays_dir) + os.sep) or not os.path.isfile(candidate):
        return None
    return candidate


def save_asset(filename, fileobj, overlays_dir=None):
    """Store an uploaded logo. Named ``<sha1[:12]>_<safe stem>.<ext>`` so the
    same file uploaded twice is one entry. Raises ValueError on a bad file."""
    overlays_dir = overlays_dir or OVERLAYS_DIR
    os.makedirs(overlays_dir, exist_ok=True)
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in ALLOWED_IMAGE_EXTS:
        raise ValueError("Unsupported image type; use png, jpg or webp.")
    data = fileobj.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image is too large (max 5 MB).")
    if not data:
        raise ValueError("Empty file.")
    try:
        from io import BytesIO
        with Image.open(BytesIO(data)) as im:
            im.verify()
        with Image.open(BytesIO(data)) as im:
            w, h = im.size
    except Exception:
        raise ValueError("That file is not a readable image.")
    stem = re.sub(r"[^A-Za-z0-9 _().-]+", "", os.path.splitext(os.path.basename(filename))[0]).strip()[:60]
    stem = stem or "logo"
    name = f"{hashlib.sha1(data).hexdigest()[:12]}_{stem}{ext}"
    path = os.path.join(overlays_dir, name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return {"file": name, "name": stem, "width": w, "height": h}


# --------------------------------------------------------------------------- #
# Spec
# --------------------------------------------------------------------------- #
def _frac(v, lo, hi, default):
    try:
        return min(hi, max(lo, float(v)))
    except (TypeError, ValueError):
        return default


def _color(v, default):
    s = str(v or "").strip()
    return "#" + s.lstrip("#").upper() if _HEX.match(s) else default


def normalize(items):
    """Validated overlay list (max MAX_ITEMS). Geometry: ``x``/``y`` = centre
    as a fraction of the frame, ``w`` = width fraction; text ``size`` = font
    height as a fraction of the frame height. Unknown keys and unknown types
    are dropped, values clamped. An empty result means "no overlay layer"."""
    out = []
    if not isinstance(items, list):
        return out
    for raw in items[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "").lower()
        base = {
            "type": kind,
            "x": round(_frac(raw.get("x"), 0.0, 1.0, 0.5), 4),
            "y": round(_frac(raw.get("y"), 0.0, 1.0, 0.5), 4),
            "w": round(_frac(raw.get("w"), 0.02, 1.0, 0.3), 4),
            "opacity": round(_frac(raw.get("opacity"), 0.0, 1.0, 1.0), 3),
            "rotation": round(_frac(raw.get("rotation"), -180.0, 180.0, 0.0), 1),
        }
        if kind == "image":
            asset = os.path.basename(str(raw.get("asset") or ""))
            if not asset:
                continue
            base["asset"] = asset
            out.append(base)
        elif kind == "text":
            text = str(raw.get("text") or "").replace("\r", "")
            lines = [ln.strip() for ln in text.split("\n")][:MAX_TEXT_LINES]
            text = "\n".join(lines).strip()[:MAX_TEXT_CHARS]
            if not text:
                continue
            font = re.sub(r"[,{}\\\r\n\t]", "", str(raw.get("font_family") or "Roboto"))[:64] or "Roboto"
            align = str(raw.get("align") or "center").lower()
            base.update({
                "text": text,
                "size": round(_frac(raw.get("size"), 0.01, 0.3, 0.05), 4),
                "font_family": font,
                "color": _color(raw.get("color"), "#FFFFFF"),
                "bold": bool(raw.get("bold", True)),
                "uppercase": bool(raw.get("uppercase", False)),
                "outline_color": _color(raw.get("outline_color"), "#000000"),
                "outline": round(_frac(raw.get("outline"), 0.0, 40.0, 4.0), 1),
                "bg_color": _color(raw.get("bg_color"), "#000000"),
                "bg_opacity": round(_frac(raw.get("bg_opacity"), 0.0, 1.0, 0.0), 3),
                "align": align if align in ALIGNS else "center",
            })
            out.append(base)
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _rgba(hex_color, alpha=1.0):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(round(255 * alpha)))


def _font_path(family, bold):
    """A bundled TTF for the family; Roboto Bold/Regular as the fallback."""
    fonts_dir = caption_styles.FONTS_DIR
    file = caption_styles.font_file_for(family)
    if file and bold and "Regular" in file:
        cand = os.path.join(fonts_dir, file.replace("Regular", "Bold"))
        if os.path.exists(cand):
            return cand
    if file:
        return os.path.join(fonts_dir, file)
    return os.path.join(fonts_dir, "Roboto-Bold.ttf" if bold else "Roboto-Regular.ttf")


def _wrap(draw, text, font, emoji_font, max_width, hooks):
    lines = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            continue
        cur = ""
        for word in words:
            if hooks._measure_width(draw, word, font, emoji_font) > max_width:
                if cur:
                    lines.append(cur)
                    cur = ""
                pieces = hooks._break_long_word(draw, word, font, emoji_font, max_width)
                lines.extend(pieces[:-1])
                cur = pieces[-1] if pieces else ""
                continue
            trial = f"{cur} {word}".strip()
            if cur and hooks._measure_width(draw, trial, font, emoji_font) > max_width:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
    return lines[:MAX_TEXT_LINES * 2]


def render_text_block(item, vw, vh):
    """RGBA image of one text item at its box width (``w`` * vw)."""
    import hooks
    box_w = max(8, int(round(item["w"] * vw)))
    px = max(8, int(round(item["size"] * vh)))
    text = item["text"].upper() if item.get("uppercase") else item["text"]
    try:
        font = ImageFont.truetype(_font_path(item["font_family"], item.get("bold", True)), px)
    except Exception:
        font = ImageFont.load_default()
    emoji_font = hooks._load_emoji_font(px)
    stroke = int(round(item["outline"] * vh / 1920.0))
    pad_x = int(round(px * 0.35))
    pad_y = int(round(px * 0.25))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = _wrap(probe, text, font, emoji_font, max(1, box_w - 2 * pad_x - 2 * stroke), hooks)
    if not lines:
        return None
    line_h = int(round(px * 1.2))
    height = line_h * len(lines) + 2 * pad_y + 2 * stroke
    img = Image.new("RGBA", (box_w, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if item["bg_opacity"] > 0:
        radius = int(round(px * 0.25))
        draw.rounded_rectangle((0, 0, box_w - 1, height - 1), radius=radius,
                               fill=_rgba(item["bg_color"], item["bg_opacity"]))
    fill = _rgba(item["color"])
    outline = (_rgba(item["outline_color"]), stroke) if stroke > 0 else None
    y = pad_y + stroke
    for line in lines:
        lw = hooks._measure_width(draw, line, font, emoji_font)
        if item["align"] == "left":
            x = pad_x + stroke
        elif item["align"] == "right":
            x = box_w - pad_x - stroke - lw
        else:
            x = (box_w - lw) / 2.0
        hooks._draw_mixed(img, draw, (x, y), line, font, emoji_font, fill, outline=outline)
        y += line_h
    return img


def render_image_item(item, vw, overlays_dir=None):
    path = resolve_asset(item["asset"], overlays_dir)
    if not path:
        return None
    with Image.open(path) as im:
        im = im.convert("RGBA")
        target_w = max(4, int(round(item["w"] * vw)))
        scale = target_w / float(im.width)
        return im.resize((target_w, max(1, int(round(im.height * scale)))), Image.LANCZOS)


def _place(canvas, img, item):
    if item.get("opacity", 1.0) < 1.0:
        alpha = img.getchannel("A").point(lambda a: int(a * item["opacity"]))
        img.putalpha(alpha)
    if item.get("rotation"):
        img = img.rotate(-item["rotation"], expand=True, resample=Image.BICUBIC)
    cx = item["x"] * canvas.width
    cy = item["y"] * canvas.height
    x = int(round(cx - img.width / 2.0))
    y = int(round(cy - img.height / 2.0))
    canvas.alpha_composite(img, (x, y)) if (0 <= x and 0 <= y and x + img.width <= canvas.width
                                            and y + img.height <= canvas.height) else _paste_clipped(canvas, img, x, y)


def _paste_clipped(canvas, img, x, y):
    """alpha_composite needs the source inside the canvas; crop what hangs off."""
    left, top = max(0, -x), max(0, -y)
    right = min(img.width, canvas.width - x)
    bottom = min(img.height, canvas.height - y)
    if right <= left or bottom <= top:
        return
    canvas.alpha_composite(img.crop((left, top, right, bottom)), (x + left, y + top))


def render_layer(items, vw, vh, overlays_dir=None):
    """One full-frame RGBA image with every item composited, or None."""
    items = normalize(items)
    if not items:
        return None
    canvas = Image.new("RGBA", (vw, vh), (0, 0, 0, 0))
    drawn = 0
    for item in items:
        try:
            img = (render_image_item(item, vw, overlays_dir) if item["type"] == "image"
                   else render_text_block(item, vw, vh))
        except Exception as e:
            print(f"   ⚠️ Overlay item skipped ({item['type']}): {e}")
            img = None
        if img is None:
            continue
        _place(canvas, img, item)
        drawn += 1
    return canvas if drawn else None


def apply_overlays(video_path, items, output_path, overlays_dir=None):
    """Composite the overlay layer onto ``video_path`` into ``output_path``.
    Returns True on success, False (nothing written) on failure or when there
    is nothing to draw, the fail-open contract of every layer."""
    dims = probe_dimensions(video_path)
    if not dims:
        return False
    layer = render_layer(items, dims[0], dims[1], overlays_dir)
    if layer is None:
        return False
    png = output_path + ".overlay.png"
    tmp = output_path + ".tmp.mp4"
    try:
        layer.save(png)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-i", png,
               "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto[v]",
               "-map", "[v]", "-map", "0:a?",
               *video_encode_args(QUALITY_FAST), "-c:a", "copy", *METADATA_SCRUB,
               "-movflags", "+faststart", tmp]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
        if result.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, output_path)
            return True
        err = (result.stderr or b"").decode(errors="ignore")[-300:]
        print(f"   ⚠️ Overlay pass failed (clip kept as is): {err}")
        return False
    finally:
        for p in (png, tmp):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def derived_name(clean_name):
    return f"ov_{uuid.uuid4().hex[:6]}_{clean_name}"
