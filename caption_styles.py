"""Caption looks: the preset library, the per-render override schema and the
bundled font registry. Ported from ClipForge (app/captions.py, app/models.py,
app/fonts.py) so OpenShorts clips can wear the same creator styles.

Two layers, same as ClipForge: a preset (server-side, every key guaranteed)
and an optional override dict the user layers on top for one render. Both are
plain data here; ``subtitles.generate_ass_styled`` turns the merged config
into an ASS file. Values are tuned for a 1080x1920 frame and rescaled by the
renderer, so one preset looks the same on a 9:16, 1:1 or 16:9 clip.

Unlike ClipForge, where one style applied to every clip of a job before
generation, this feeds ``/api/subtitle`` — a per-clip, post-generation restyle
— and the chosen ``{preset, overrides}`` is persisted on the clip so later
re-renders (format change, cinematic look, trim) put the same captions back.
"""
import os
import re
import struct
import threading

_BASE_PRESET = {
    "font_family": "Roboto",
    "bold": True,
    "font_size": 90,
    "primary_color": "#FFFFFF",
    "highlight_color": "#FFD400",
    "outline_color": "#000000",
    "outline": 5,
    "shadow": 1,
    "position": "bottom",
    "karaoke": False,
    "uppercase": True,
    "animation": "none",
    "tracking": 0,
    "underline": False,
    "strikethrough": False,
    "max_lines": 2,
    "max_chars": 22,
    "background_enabled": False,
    "background_color": "#000000",
    "trending": False,
}


def _P(label, **kw):
    preset = dict(_BASE_PRESET)
    preset.update(kw)
    preset["label"] = label
    return preset


# Verbatim from ClipForge's STYLE_PRESETS (20 looks). Ids are part of the API:
# the dashboard stores them in localStorage and metadata.json carries them.
STYLE_PRESETS = {
    "bold_white": _P("Bold White", highlight_color="#FFFFFF", font_size=96, max_chars=20),
    "karaoke_yellow": _P("Karaoke Yellow", karaoke=True, font_size=92, highlight_color="#FFE600"),
    "minimal": _P("Minimal", bold=False, uppercase=False, font_size=64, outline=1, shadow=2,
                  highlight_color="#FFFFFF", max_chars=28),

    "hormozi_green": _P("Hormozi Green", trending=True, font_family="Montserrat",
                        animation="highlight", highlight_color="#27E36B", font_size=94,
                        outline=6, position="center", max_lines=2, max_chars=16),
    "hormozi_yellow": _P("Hormozi Yellow", trending=True, font_family="Montserrat",
                         animation="highlight", highlight_color="#FFD400", font_size=94,
                         outline=6, position="center", max_lines=2, max_chars=16),
    "beast_red": _P("Beast Pop", trending=True, font_family="Anton", bold=False,
                    animation="highlight", highlight_color="#FF3B30", font_size=108,
                    outline=7, position="center", max_lines=2, max_chars=15),
    "raj_clean": _P("Raj Shamani Clean", trending=True, font_family="Poppins",
                    animation="highlight", uppercase=False, highlight_color="#FFC400",
                    font_size=78, outline=4, max_chars=26),
    "alex_caps": _P("Alex Bold Caps", trending=True, font_family="Montserrat",
                    animation="highlight", highlight_color="#22D3EE", font_size=92,
                    outline=6, position="center", max_chars=17),
    "one_word_punch": _P("One-Word Punch", trending=True, font_family="Anton", bold=False,
                         animation="one_word", font_size=132, outline=8, position="center"),
    "word_reveal": _P("Word Reveal", trending=True, font_family="Montserrat",
                      animation="word_reveal", highlight_color="#FFFFFF", font_size=90, outline=5),
    "bebas_clean": _P("Bebas Clean", trending=True, font_family="Bebas Neue", bold=False,
                      font_size=110, outline=4, highlight_color="#FFFFFF", tracking=2, max_chars=22),
    "comic_bangers": _P("Comic Punch", trending=True, font_family="Bangers", bold=False,
                        primary_color="#FFE600", highlight_color="#FFFFFF", font_size=104,
                        outline=6, max_chars=20),
    "slab_impact": _P("Slab Impact", trending=True, font_family="Alfa Slab One", bold=False,
                      animation="highlight", highlight_color="#FFD400", font_size=84, outline=6),
    "marker_note": _P("Marker", trending=True, font_family="Permanent Marker", bold=False,
                      uppercase=False, highlight_color="#FFD400", font_size=82, outline=5),
    "serif_elegant": _P("Serif Elegant", trending=True, font_family="DM Serif Display", bold=False,
                        uppercase=False, highlight_color="#FFD400", font_size=88, outline=2, shadow=3,
                        max_chars=30),
    "neon_pop": _P("Neon Pop", trending=True, font_family="Luckiest Guy", bold=False,
                   animation="highlight", highlight_color="#22D3EE", outline_color="#101018",
                   font_size=92, outline=6, position="center"),
    "boxed_tiktok": _P("Boxed", trending=True, font_family="Roboto", background_enabled=True,
                       background_color="#000000", highlight_color="#FFFFFF", font_size=78,
                       outline=6, shadow=0, max_chars=24),
    "oswald_news": _P("Oswald News", trending=True, font_family="Oswald",
                      animation="highlight", highlight_color="#FFD400", font_size=86, outline=4),
    "green_word": _P("Green Word", trending=True, font_family="Poppins",
                     animation="highlight", highlight_color="#27E36B", font_size=84, outline=5),
}

DEFAULT_PRESET = "bold_white"

# ClipForge's five "built-in themes": override bundles the UI applies over the
# current preset rather than presets of their own.
THEMES = [
    {"id": "classic", "label": "Classic", "color": "#FFFFFF", "font": "Roboto",
     "overrides": {"font_family": "Roboto", "primary_color": "#FFFFFF", "highlight_color": "#FFD400",
                   "animation": "none", "karaoke": False, "bold": True, "uppercase": True,
                   "glow_enabled": False}},
    {"id": "clean", "label": "Clean", "color": "#D8D8E6", "font": "Poppins",
     "overrides": {"font_family": "Poppins", "primary_color": "#FFFFFF", "highlight_color": "#FFFFFF",
                   "animation": "none", "karaoke": False, "bold": False, "uppercase": False,
                   "glow_enabled": False}},
    {"id": "bold", "label": "Bold", "color": "#B39DFF", "font": "Montserrat",
     "overrides": {"font_family": "Montserrat", "primary_color": "#FFFFFF", "highlight_color": "#7C4DFF",
                   "animation": "highlight", "karaoke": False, "bold": True, "uppercase": True,
                   "glow_enabled": False}},
    {"id": "neon", "label": "Neon", "color": "#22D3EE", "font": "Poppins",
     "overrides": {"font_family": "Poppins", "primary_color": "#FFFFFF", "highlight_color": "#22D3EE",
                   "animation": "highlight", "karaoke": False, "bold": True, "uppercase": True,
                   "glow_enabled": True, "glow_color": "#22D3EE", "glow_intensity": 12}},
    {"id": "typewriter", "label": "Typewriter", "color": "#27E36B", "font": "JetBrains Mono",
     "overrides": {"font_family": "JetBrains Mono", "primary_color": "#27E36B", "highlight_color": "#27E36B",
                   "animation": "word_reveal", "karaoke": False, "bold": False, "uppercase": False,
                   "glow_enabled": False}},
]

SWATCHES = ["#FFFFFF", "#FFD400", "#FFB020", "#FF3B30", "#FF2D78",
            "#27E36B", "#22D3EE", "#3B82F6", "#7C4DFF", "#000000"]

ANIMATIONS = ("none", "highlight", "word_reveal", "one_word", "karaoke")
POSITIONS = ("top", "center", "bottom")

# The nine-dot placement grid the UI offers, as (pos_x, pos_y) percentages.
POSITION_GRID = [[8, 14], [50, 14], [92, 14], [8, 50], [50, 50], [92, 50],
                 [8, 88], [50, 88], [92, 88]]

# Override schema, mirroring ClipForge's CaptionOverrides pydantic model:
# ("float", lo, hi) | ("int", lo, hi) | ("bool",) | ("color",) | ("enum", (...)) | ("str",)
OVERRIDE_SPEC = {
    "pos_x": ("float", 0, 100),
    "pos_y": ("float", 0, 100),
    # Fine adjustment in % of the frame, applied on top of the anchor (or the
    # pin): "center, then a bit lower" keeps centre alignment and moves down.
    "offset_x": ("float", -50, 50),
    "offset_y": ("float", -50, 50),
    "rotation": ("float", -180, 180),
    "outline_width": ("float", 0, 40),
    "outline_color": ("color",),
    "shadow_enabled": ("bool",),
    "shadow_distance": ("float", 0, 40),
    "shadow_color": ("color",),
    "shadow_opacity": ("float", 0, 100),
    "background_enabled": ("bool",),
    "background_color": ("color",),
    "background_opacity": ("float", 0, 100),
    "glow_enabled": ("bool",),
    "glow_color": ("color",),
    "glow_intensity": ("float", 0, 30),
    "max_lines": ("int", 1, 2),
    "max_chars": ("int", 8, 48),
    "animation": ("enum", ANIMATIONS),
    "font_family": ("str",),
    "bold": ("bool",),
    "uppercase": ("bool",),
    "primary_color": ("color",),
    "highlight_color": ("color",),
    "font_scale": ("float", 0.4, 2.5),
    "tracking": ("float", 0, 40),
    "underline": ("bool",),
    "strikethrough": ("bool",),
    "karaoke": ("bool",),
    "position": ("enum", POSITIONS),
}

_HEX = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _to_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def normalize_overrides(raw):
    """Keep only known override keys with valid values, clamped to range.

    A malformed override degrades to "not set" rather than reaching the ASS
    writer, the same contract cinematic.normalize has: bad input never breaks
    a filtergraph, it just does less.
    """
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        spec = OVERRIDE_SPEC.get(key)
        if spec is None or val is None:
            continue
        kind = spec[0]
        try:
            if kind == "float":
                out[key] = min(spec[2], max(spec[1], float(val)))
            elif kind == "int":
                out[key] = min(spec[2], max(spec[1], int(round(float(val)))))
            elif kind == "bool":
                out[key] = _to_bool(val)
            elif kind == "color":
                s = str(val).strip()
                if _HEX.match(s):
                    out[key] = "#" + s.lstrip("#").upper()
            elif kind == "enum":
                s = str(val).strip().lower()
                if s in spec[1]:
                    out[key] = s
            elif kind == "str":
                s = str(val).strip()[:64]
                # Font names travel into an ASS Style line: no commas, braces
                # or control characters.
                s = re.sub(r"[,{}\\\r\n\t]", "", s)
                if s:
                    out[key] = s
        except (TypeError, ValueError):
            continue
    return out


def get_preset(preset_id):
    return dict(STYLE_PRESETS.get(preset_id or "", STYLE_PRESETS[DEFAULT_PRESET]))


def is_known_preset(preset_id):
    return preset_id in STYLE_PRESETS


def merge(preset_id, overrides=None):
    """The effective caption config: preset with normalized overrides on top.

    ``animation: "karaoke"`` is how the UI spells the libass \\k fill; the
    renderer keys it off the ``karaoke`` flag, so it is folded in here.
    """
    cfg = get_preset(preset_id)
    cfg.pop("label", None)
    cfg.pop("trending", None)
    ov = normalize_overrides(overrides)
    if ov.get("animation") == "karaoke":
        ov["karaoke"] = True
        ov["animation"] = "none"
    elif "animation" in ov and ov["animation"] != "none" and "karaoke" not in ov:
        # A timed animation and the karaoke fill fight over the same words.
        cfg["karaoke"] = False
    cfg.update(ov)
    return cfg


def presets_for_api():
    out = []
    for pid, preset in STYLE_PRESETS.items():
        entry = dict(preset)
        entry["id"] = pid
        out.append(entry)
    return out


def themes_for_api():
    return [dict(t) for t in THEMES]


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# Caption language -> a typeface that has the glyphs. libass falls back to
# something, but usually not something legible for Arabic-script or Devanagari.
LANG_DEFAULT_FONT = {
    "ur": "Noto Nastaliq Urdu",
    "ar": "Noto Naskh Arabic",
    "fa": "Noto Naskh Arabic",
    "hi": "Noto Sans Devanagari",
    "mr": "Noto Sans Devanagari",
    "ne": "Noto Sans Devanagari",
}

_fonts_cache = {"stamp": None, "fonts": []}
_fonts_lock = threading.Lock()


def family_from_sfnt(data):
    """Read the family name out of a TTF/OTF ``name`` table, or None.

    Typographic Family (nameID 16) wins over Family (nameID 1): styled cuts of
    one family ("Poppins Bold") report the plain family there, which is the
    name libass matches the ASS Fontname against.
    """
    try:
        offset = 0
        if data[:4] == b"ttcf":
            offset = struct.unpack(">I", data[12:16])[0]
        num_tables = struct.unpack(">H", data[offset + 4:offset + 6])[0]
        name_off = None
        rec = offset + 12
        for _ in range(num_tables):
            tag = data[rec:rec + 4]
            toff = struct.unpack(">I", data[rec + 8:rec + 12])[0]
            if tag == b"name":
                name_off = toff
                break
            rec += 16
        if name_off is None:
            return None
        count, string_off = struct.unpack(">HH", data[name_off + 2:name_off + 6])
        strings = name_off + string_off
        family_1 = None
        for i in range(count):
            r = name_off + 6 + i * 12
            platform, _enc, _lang, name_id, length, off = struct.unpack(">HHHHHH", data[r:r + 12])
            if name_id not in (1, 16):
                continue
            raw = data[strings + off:strings + off + length]
            try:
                text = (raw.decode("utf-16-be") if platform in (0, 3) else raw.decode("latin-1")).strip()
            except Exception:
                continue
            if not text:
                continue
            if name_id == 16:
                return text
            if family_1 is None:
                family_1 = text
        return family_1
    except Exception:
        return None


def list_fonts(fonts_dir=None):
    """``[{"family", "file"}]`` for every TTF/OTF in the fonts directory.

    Families are sniffed from the font files themselves (the filename is only
    the fallback) because that is the string libass resolves; cached on the
    directory's mtime so the endpoint does not reopen thirty files per call.
    """
    fonts_dir = fonts_dir or FONTS_DIR
    try:
        names = sorted(n for n in os.listdir(fonts_dir)
                       if n.lower().endswith((".ttf", ".otf")))
        stamp = (fonts_dir, os.stat(fonts_dir).st_mtime_ns, tuple(names))
    except OSError:
        return []
    with _fonts_lock:
        if _fonts_cache["stamp"] == stamp:
            return list(_fonts_cache["fonts"])
        seen = {}
        for name in names:
            path = os.path.join(fonts_dir, name)
            family = None
            try:
                with open(path, "rb") as f:
                    family = family_from_sfnt(f.read())
            except OSError:
                pass
            family = family or re.sub(r"[-_](Regular|Bold)$", "", os.path.splitext(name)[0])
            # One entry per family: a Regular+Bold pair is one choice in the UI.
            if family not in seen or "Regular" in name:
                seen[family] = {"family": family, "file": name}
        fonts = sorted(seen.values(), key=lambda e: e["family"].lower())
        _fonts_cache["stamp"] = stamp
        _fonts_cache["fonts"] = fonts
        return list(fonts)


def font_file_for(family, fonts_dir=None):
    for entry in list_fonts(fonts_dir):
        if entry["family"].lower() == str(family or "").lower():
            return entry["file"]
    return None
