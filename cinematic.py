"""Cinematic look: static, user-chosen grade/glow/grain/vignette/gradient/
letterbox stack applied once per clip, at render time.

Ported from ClipForge's effects.py, which offers the same knobs as a
pre-generation choice (not a retroactive per-clip edit like subtitles/hooks
here) — so this module is applied the same way: one ffmpeg pass right after
the reframe, same call shape as main.py's apply_watermark, so it sits under
the watermark/hook/caption layers exactly like ClipForge sits it under
captions.
"""
import os
import re
import subprocess

from ffmpeg_utils import video_encode_args, QUALITY_FAST, METADATA_SCRUB

# Drop-in .cube LUTs (DaVinci Resolve / Premiere exports). Only a file that is
# actually listed here can be named, so a request can never point lut3d at an
# arbitrary path on disk. Bundle only LUTs whose licence allows it.
LUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "luts")
_LUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,95}$")

COLOR_GRADES = {
    "none": None,
    "warm": "eq=contrast=1.05:saturation=1.10,colorbalance=rm=0.08:gm=0.02:bm=-0.08",
    "cool": "eq=contrast=1.05:saturation=1.05,colorbalance=rm=-0.06:bm=0.10",
    "teal_orange": ("colorbalance=rs=0.12:gs=-0.02:bs=-0.12:rm=0.10:bm=-0.10:rh=-0.05:bh=0.10,"
                     "eq=saturation=1.15:contrast=1.08"),
    "vintage": "eq=contrast=0.92:saturation=0.75:brightness=0.02,colorbalance=rm=0.06:gm=0.03:bm=-0.04",
    "vibrant": "eq=contrast=1.12:saturation=1.35",
    "bw": "hue=s=0",
    # Film-module grades (docs/film-modules-plan.md): the two looks the
    # reference movie-shorts channel alternates between. Named presets rather
    # than model-generated chains because consistency across ~86 shots matters
    # more than cleverness.
    "washed": "eq=saturation=0.78:contrast=1.04:brightness=0.03",
    "cold": "eq=saturation=0.95:gamma_b=1.08:gamma_r=0.95",
    # Remaster / mobile-feed grades. `vibrance` lifts muted colours more than
    # already-strong ones, which tends to spare skin where eq=saturation
    # pushes it orange; it does not detect skin.
    "classic_remaster": ("eq=contrast=1.08:brightness=0.015:gamma=0.96,"
                         "vibrance=intensity=0.28,colorbalance=rm=0.03:gm=0.01:bm=-0.04"),
    # Curves approximation of a Kodak 2383 print look, not an emulation of
    # the stock: the real thing is a .cube LUT (see LUT_DIR).
    "film_print": ("curves=r='0/0 0.22/0.18 0.75/0.82 1/1'"
                   ":g='0/0 0.25/0.23 0.78/0.80 1/1'"
                   ":b='0/0 0.18/0.22 0.72/0.78 1/1',"
                   "colorbalance=rs=0.06:bs=-0.06:rm=0.04:bm=-0.04,vibrance=intensity=0.22"),
    "studio_pop": "curves=preset=lighter,vibrance=intensity=0.35,eq=contrast=1.07:gamma=0.97",
    "hdr_punch": ("curves=all='0/0 0.20/0.15 0.80/0.86 1/1',vibrance=intensity=0.30,"
                  "colorbalance=rs=-0.04:bs=0.06:rh=0.05:bh=-0.04"),
    "moody_cinema": ("curves=all='0/0.03 0.30/0.24 0.70/0.74 1/0.95',"
                     "colorbalance=rs=-0.08:bs=0.12,eq=saturation=0.90"),
}

GRADE_LABELS = {
    "none": "No grade",
    "warm": "Warm",
    "cool": "Cool",
    "teal_orange": "Teal & Orange",
    "vintage": "Vintage",
    "vibrant": "Vibrant",
    "bw": "Black & White",
    "washed": "Washed",
    "cold": "Cold",
    "classic_remaster": "Classic TV remaster",
    "film_print": "Film print (2383-style)",
    "studio_pop": "Studio pop",
    "hdr_punch": "HDR punch",
    "moody_cinema": "Moody cinema",
}

# Sharpening is its own control, not a grade, so any grade can be paired with
# any sharpness. Each entry is (denoise, sharpen): the denoise runs BEFORE the
# grade (cleaning tape/compression noise before contrast is added to it) and
# the sharpen AFTER it, but before glow and grain, because sharpening grain
# turns it into hard speckle. CAS (contrast adaptive sharpening) lifts
# low-contrast detail without the halos unsharp leaves on hard edges.
# Measured on a 10 s 1080x1920 clip, CPU: crisp 2.2 s, tv_remaster 4.2 s.
# nlmeans/bm3d were rejected: nlmeans took over two minutes on the same clip.
SHARPEN_PRESETS = {
    "none": (None, None),
    "crisp": (None, "cas=strength=0.45"),
    "studio_hd": (None, "cas=strength=0.65,unsharp=5:5:0.45:3:3:0.0"),
    "tv_remaster": ("hqdn3d=1.8:1.8:5:5", "cas=strength=0.55,unsharp=3:3:0.35:3:3:0.0"),
}

SHARPEN_LABELS = {
    "none": "No sharpening",
    "crisp": "Crisp",
    "studio_hd": "Studio HD",
    "tv_remaster": "Retro TV remaster (denoise + sharpen)",
}


def list_luts():
    """Names (file stems) of the .cube files in LUT_DIR, sorted."""
    try:
        names = os.listdir(LUT_DIR)
    except OSError:
        return []
    out = []
    for n in names:
        stem, ext = os.path.splitext(n)
        if ext.lower() == ".cube" and _LUT_NAME_RE.match(stem)                 and os.path.isfile(os.path.join(LUT_DIR, n)):
            out.append(stem)
    return sorted(out, key=str.lower)


def lut_path(name):
    """Absolute path of a listed LUT, or None. The listing is the allowlist."""
    if not name or name == "none" or name not in list_luts():
        return None
    for n in os.listdir(LUT_DIR):
        stem, ext = os.path.splitext(n)
        if stem == name and ext.lower() == ".cube":
            return os.path.join(LUT_DIR, n)
    return None


def _filter_path(path):
    """A path as a filtergraph option value. Forward slashes, then the
    characters the filter-args parser treats specially are escaped, then the
    whole thing is single-quoted for the graph parser. The drive colon on
    Windows (F:) is what breaks an unescaped path."""
    p = path.replace("\\", "/")
    p = p.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return f"'{p}'"


def options_for_api():
    """What the look modal offers. Static apart from the LUT listing."""
    return {
        "grades": [{"value": k, "label": GRADE_LABELS.get(k, k)} for k in COLOR_GRADES],
        "sharpen": [{"value": k, "label": SHARPEN_LABELS.get(k, k)} for k in SHARPEN_PRESETS],
        "luts": list_luts(),
    }

DEFAULTS = dict(
    color_grade="none",
    sharpen="none",
    lut="none",
    glow=False, glow_strength=0.5,
    grain=False, grain_strength=0.5,
    vignette=False, vignette_strength=0.5,
    bottom_gradient=False, bottom_gradient_height=0.35, bottom_gradient_strength=0.6,
    top_gradient=False, top_gradient_height=0.2, top_gradient_strength=0.4,
    letterbox=False, letterbox_size=0.5,
)

_BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
_FLOAT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, float)}


def _to_bool(val):
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def normalize(effects):
    """Merge a caller dict onto DEFAULTS, dropping unknown keys and clamping
    ranges to [0, 1] so a bad request degrades to 'no effect' rather than a
    broken filtergraph."""
    out = dict(DEFAULTS)
    if not isinstance(effects, dict):
        return out
    for key in DEFAULTS:
        if key not in effects or effects[key] is None:
            continue
        val = effects[key]
        if key in _BOOL_KEYS:
            out[key] = _to_bool(val)
        elif key in _FLOAT_KEYS:
            try:
                out[key] = max(0.0, min(1.0, float(val)))
            except (TypeError, ValueError):
                pass
        elif key == "color_grade":
            out[key] = val if val in COLOR_GRADES else "none"
        elif key == "sharpen":
            out[key] = val if val in SHARPEN_PRESETS else "none"
        elif key == "lut":
            out[key] = val if isinstance(val, str) and lut_path(val) else "none"
    return out


def is_noop(effects):
    e = normalize(effects)
    return (e["color_grade"] == "none" and e["sharpen"] == "none"
            and e["lut"] == "none" and not e["glow"] and not e["grain"]
            and not e["vignette"] and not e["bottom_gradient"]
            and not e["top_gradient"] and not e["letterbox"])


def _gradient_bands(y_top, band_h, width, height, strength, edge_at_bottom):
    """Stack thin, increasingly-opaque drawbox strips so the scrim fades in
    instead of ending in a hard line. edge_at_bottom=True darkens toward the
    bottom of the band (a bottom-of-frame scrim); False darkens toward the top
    (a top-of-frame scrim)."""
    bands = 7
    band_h = max(bands, band_h)
    step = band_h / bands
    filters = []
    for i in range(bands):
        frac = (i + 1) / bands  # 1/bands .. 1.0
        alpha = min(0.85, strength * frac)
        y = y_top + int(i * step) if edge_at_bottom else y_top + band_h - int((i + 1) * step)
        h = int(step) + 1
        filters.append(f"drawbox=x=0:y={y}:w={width}:h={h}:color=black@{alpha:.3f}:t=fill")
    return filters


def build_filter_complex(effects, width, height):
    """ffmpeg filter_complex graph string ending in ``[fx]``, or None when
    every toggle is off (caller should skip the ffmpeg pass entirely)."""
    e = normalize(effects)
    if is_noop(e):
        return None

    graph = []
    cur = "0:v"
    idx = [0]

    def emit(filt):
        out = f"fxs{idx[0]}"
        idx[0] += 1
        graph.append(f"[{cur}]{filt}[{out}]")
        return out

    # denoise -> LUT -> grade -> sharpen, then glow/grain/vignette on top.
    denoise, sharpen = SHARPEN_PRESETS.get(e["sharpen"], (None, None))
    pre = []
    if denoise:
        pre.append(denoise)
    lut = lut_path(e["lut"])
    if lut:
        pre.append(f"lut3d=file={_filter_path(lut)}")
    grade = COLOR_GRADES.get(e["color_grade"])
    if grade:
        pre.append(grade)
    if sharpen:
        pre.append(sharpen)
    if pre:
        cur = emit(",".join(pre))

    if e["glow"]:
        sigma = 4 + e["glow_strength"] * 16       # 4-20
        opacity = 0.25 + e["glow_strength"] * 0.45  # 0.25-0.70
        graph.append(f"[{cur}]split[fxg0][fxg1]")
        graph.append(f"[fxg1]gblur=sigma={sigma:.1f}[fxgb]")
        out = f"fxs{idx[0]}"
        idx[0] += 1
        graph.append(f"[fxg0][fxgb]blend=all_mode=screen:all_opacity={opacity:.2f}[{out}]")
        cur = out

    post = []
    if e["grain"]:
        strength = int(4 + e["grain_strength"] * 26)  # 4-30
        post.append(f"noise=alls={strength}:allf=t")
    if e["vignette"]:
        denom = 7.0 - 3.5 * e["vignette_strength"]  # 7.0 (subtle) .. 3.5 (strong)
        post.append(f"vignette=angle=PI/{denom:.2f}")
    if post:
        cur = emit(",".join(post))

    if e["bottom_gradient"]:
        band_h = max(1, int(height * e["bottom_gradient_height"]))
        bands = _gradient_bands(height - band_h, band_h, width, height,
                                 e["bottom_gradient_strength"], edge_at_bottom=True)
        cur = emit(",".join(bands))

    if e["top_gradient"]:
        band_h = max(1, int(height * e["top_gradient_height"]))
        bands = _gradient_bands(0, band_h, width, height,
                                e["top_gradient_strength"], edge_at_bottom=False)
        cur = emit(",".join(bands))

    if e["letterbox"]:
        bar_h = max(1, int(height * (0.05 + e["letterbox_size"] * 0.09)))  # 5%-14% of height
        bars = (f"drawbox=x=0:y=0:w={width}:h={bar_h}:color=black:t=fill,"
                f"drawbox=x=0:y={height - bar_h}:w={width}:h={bar_h}:color=black:t=fill")
        cur = emit(bars)

    graph.append(f"[{cur}]copy[fx]")
    return ";".join(graph)


def apply_cinematic_effects(video_path, effects, output_path=None):
    """Burns the cinematic look into ``video_path``. Returns True on success,
    False (input kept untouched) on any failure — matches apply_watermark's
    fail-open contract so a bad filter never kills the job.

    In place by default (the generation-time path, where the clip is about to
    become the canonical file anyway). With ``output_path`` the graded copy is
    written there and the input is left alone: the post-generation look
    endpoint uses that to keep the clean clip for later re-styling, the same
    way captions and hooks live in ``subtitled_``/``hooked_`` derivatives.
    """
    if is_noop(effects):
        return False

    try:
        probe = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", video_path],
            stderr=subprocess.STDOUT, timeout=60,
        ).decode().strip().split("x")
        width, height = int(probe[0]), int(probe[1])
    except Exception as ex:
        print(f"   ⚠️ Could not probe clip for cinematic effects ({ex}); clip kept plain.")
        return False

    graph = build_filter_complex(effects, width, height)
    if graph is None:
        return False

    tmp_path = (output_path or video_path) + ".fx.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
           "-filter_complex", graph, "-map", "[fx]", "-map", "0:a?",
           *video_encode_args(QUALITY_FAST), "-c:a", "copy", *METADATA_SCRUB,
           "-movflags", "+faststart", tmp_path]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
    if result.returncode == 0 and os.path.exists(tmp_path):
        os.replace(tmp_path, output_path or video_path)
        return True
    err = (result.stderr or b"").decode(errors="ignore")[-300:]
    print(f"   ⚠️ Cinematic effects pass failed (clip kept plain): {err}")
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    return False
