"""The curated music library for the film modules: scan, tag, pick.

**Music is never generated and never fetched during a render.** The library
is a folder of tracks the user downloaded and vetted by ear,
``assets/music/<mood>/``, and the render path only reads ``manifest.json``
and picks a file. Two reasons: the render box's GPU is spoken for (YOLO,
MediaPipe, NVENC), and a downloaded track comes with a licence you can point
at, which is what a channel that may be monetised needs.

``python -m music_library scan`` is the offline step: duration, BPM, beat
grid, loudness, dynamics, loop safety and the licence sidecar per track, plus
a loudness-normalised copy under ``.normalised/`` so one ducking threshold
works on every bed. It prints flags and deletes nothing.

``pick()`` never fails a render over music: mood, then the adjacent mood,
then anything, and the result says which happened so the dashboard can show
the search recipe (``MOOD_RECIPES``) for the mood that came back thin.
"""

import json
import os
import subprocess
import sys
import time
from urllib.parse import quote

import numpy as np

from music import ALLOWED_MUSIC_EXTS, MUSIC_DIR

MANIFEST_NAME = "manifest.json"
NORMALISED_DIR = ".normalised"
NORMALISE_FILTER = "loudnorm=I=-18:TP=-2.0:LRA=11"
SR = 22050

MOODS = ("tense", "ominous", "sad", "epic", "romantic", "eerie", "driving")
FALLBACK_MOOD = {"tense": "ominous", "ominous": "tense", "sad": "romantic",
                 "romantic": "sad", "eerie": "ominous", "driving": "epic", "epic": "driving"}

# One row per mood: what to type into a stock-music search, what to listen
# for, and the BPM band that sustains 1.4 s cutting. The table, the hint
# endpoint and the dashboard all read this one dict.
MOOD_RECIPES = {
    "tense": {"primary": ["tension underscore", "suspense instrumental"],
              "alternates": ["thriller background", "ticking clock", "pulse"],
              "instruments": "pizzicato / staccato strings, low synth ostinato", "bpm": [100, 130]},
    "ominous": {"primary": ["dark cinematic", "dark ambient"],
                "alternates": ["foreboding", "sinister underscore", "dread drone"],
                "instruments": "sub-bass drone, cello, industrial hits", "bpm": [70, 100]},
    "sad": {"primary": ["emotional piano", "sad cinematic"],
            "alternates": ["melancholy instrumental", "sorrow strings"],
            "instruments": "solo piano, sustained strings", "bpm": [60, 90]},
    "epic": {"primary": ["epic underscore", "orchestral epic loop"],
             "alternates": ["heroic instrumental", "hybrid orchestral"],
             "instruments": "orchestra + percussion", "bpm": [90, 140]},
    "romantic": {"primary": ["romantic piano", "warm emotional"],
                 "alternates": ["tender instrumental", "nostalgic strings"],
                 "instruments": "piano, soft strings, guitar", "bpm": [70, 100]},
    "eerie": {"primary": ["eerie ambient", "unsettling instrumental"],
              "alternates": ["creepy background", "mystery drone", "music box"],
              "instruments": "atonal textures, bowed metal, high drone", "bpm": None},
    "driving": {"primary": ["driving percussion", "action underscore"],
                "alternates": ["chase instrumental", "relentless drums", "hybrid action"],
                "instruments": "drums, percussive synth, ostinato", "bpm": [120, 150]},
}
BED_TERMS = ["instrumental", "no vocals", "underscore", "background", "loop", "seamless", "ambient"]
AVOID_TERMS = ["trailer", "drop", "song", "vocal"]
PIXABAY_SEARCH = "https://pixabay.com/music/search/{query}/"
# Where a bed can come from with a licence you can point at. Terms change:
# verify the licence page once, record it in the track's sidecar JSON.
SOURCES = [
    {"name": "Pixabay Music", "url": "https://pixabay.com/music/", "search": PIXABAY_SEARCH,
     "licence": "Pixabay Content License", "attribution_required": False,
     "note": "commercial use ok, no attribution; user-uploaded, so not a Content ID guarantee"},
    {"name": "YouTube Audio Library", "url": "https://studio.youtube.com/channel/UC/music",
     "search": None, "licence": "YouTube Audio Library", "attribution_required": False,
     "note": "inside YouTube Studio; filter by mood + 'attribution not required'; safest against Content ID on YouTube"},
    {"name": "Free Music Archive", "url": "https://freemusicarchive.org/search/?quicksearch={query}",
     "search": "https://freemusicarchive.org/search/?quicksearch={query}", "licence": "varies (CC BY / CC0)",
     "attribution_required": True, "note": "check each track's CC licence; CC BY needs the credit line in the description"},
    {"name": "Incompetech (Kevin MacLeod)", "url": "https://incompetech.com/music/royalty-free/music.html",
     "search": None, "licence": "CC BY 4.0", "attribution_required": True,
     "note": "large, mood-tagged, credit required unless you buy a licence"},
    {"name": "CreatorMix", "url": "https://creatormix.com/", "search": None, "licence": "verify on site",
     "attribution_required": None, "note": "browse by category; read the licence page before building on it"},
    {"name": "Uppbeat (free tier)", "url": "https://uppbeat.io/browse/music", "search": None,
     "licence": "Uppbeat free licence", "attribution_required": True,
     "note": "credit line required on the free tier; paid tier issues Content ID clearance"},
]

MIN_TRACK_SECONDS = 60.0
LOOP_SAFE_DB = 6.0        # first vs last 2 s within this many dB
TOO_DYNAMIC_DB = 20.0     # 5th percentile more than this below the median
CROSSFADE_SECONDS = 1.5


# --- io ---------------------------------------------------------------------

def decode_audio(path, sr=SR, timeout=600):
    """Mono float32 samples via ffmpeg. Raises RuntimeError on failure."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr),
           "-f", "f32le", "-"]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError((res.stderr or b"").decode("utf-8", "replace")[-300:] or "no audio")
    return np.frombuffer(res.stdout, dtype=np.float32)


def manifest_path(music_dir=None):
    return os.path.join(music_dir or MUSIC_DIR, MANIFEST_NAME)


def load_manifest(music_dir=None):
    try:
        with open(manifest_path(music_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("tracks"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"tracks": [], "scanned_at": None}


def save_manifest(manifest, music_dir=None):
    path = manifest_path(music_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    os.replace(tmp, path)


def _sidecar(path):
    """``<track>.json`` next to the file: source, licence, attribution, url."""
    side = os.path.splitext(path)[0] + ".json"
    try:
        with open(side, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def list_track_files(music_dir=None):
    """``[(mood, path)]``; tracks in the root folder get mood ``any``."""
    root = music_dir or MUSIC_DIR
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if os.path.isdir(full):
            if name.startswith("."):
                continue
            mood = name.lower()
            for f in sorted(os.listdir(full)):
                if os.path.splitext(f)[1].lower() in ALLOWED_MUSIC_EXTS:
                    out.append((mood, os.path.join(full, f)))
        elif os.path.splitext(name)[1].lower() in ALLOWED_MUSIC_EXTS:
            out.append(("any", full))
    return out


# --- analysis ---------------------------------------------------------------

def _db(x):
    return 20.0 * np.log10(max(float(x), 1e-9))


def analyse_samples(y, sr=SR):
    """Per-track numbers from the samples alone (no file access)."""
    import beat_grid
    y = np.asarray(y, dtype=np.float32)
    duration = len(y) / float(sr)
    if duration <= 0:
        return {"duration": 0.0, "flags": ["empty"]}
    win = int(0.4 * sr)
    n = max(1, len(y) // win)
    rms = np.sqrt(np.mean(y[:n * win].reshape(n, win) ** 2, axis=1) + 1e-12)
    rms_db = 20.0 * np.log10(rms)
    median_db = float(np.median(rms_db))
    p5_db = float(np.percentile(rms_db, 5))
    head = _db(np.sqrt(np.mean(y[:int(2 * sr)] ** 2) + 1e-12))
    tail = _db(np.sqrt(np.mean(y[-int(2 * sr):] ** 2) + 1e-12))
    loop_safe = abs(head - tail) <= LOOP_SAFE_DB and duration >= 8
    grid = beat_grid.analyse(y, sr)
    flags = []
    if duration < MIN_TRACK_SECONDS:
        flags.append(f"short ({duration:.0f} s)")
    if median_db - p5_db > TOO_DYNAMIC_DB:
        flags.append(f"too dynamic (5th pct {median_db - p5_db:.0f} dB under median)")
    if duration < 120 and not loop_safe:
        flags.append("under 2 min and not loop-safe")
    return {
        "duration": round(duration, 2),
        "loudness_db": round(median_db, 1),
        "p5_db": round(p5_db, 1),
        "loop_safe": bool(loop_safe),
        "bpm": grid["bpm"],
        "beat_confidence": grid["beat_confidence"],
        "downbeat_phase": grid["downbeat_phase"],
        "tempo_strength": grid["tempo_strength"],
        "energy_mean": round(float(np.mean(grid["energy"])), 4) if grid["energy"] else 0.0,
        "beats": grid["beats"],
        "energy": grid["energy"],
        "flags": flags,
    }


def _normalise_copy(path, music_dir, runner=None):
    out_dir = os.path.join(music_dir, NORMALISED_DIR)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".m4a")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", path, "-vn", "-af", NORMALISE_FILTER,
           "-c:a", "aac", "-b:a", "192k", out]
    run = runner or (lambda c: subprocess.run(c, check=True, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.PIPE, timeout=600))
    run(cmd)
    return out


def scan(music_dir=None, decoder=None, normalise=True, runner=None, log=print, force=False):
    """Analyse every track under ``music_dir`` and write the manifest. Tracks
    whose file has not changed since the last scan keep their entry (and
    their ``uses`` count); ``force`` re-analyses everything."""
    music_dir = music_dir or MUSIC_DIR
    decoder = decoder or decode_audio
    previous = {t["path"]: t for t in load_manifest(music_dir)["tracks"]}
    tracks = []
    for mood, path in list_track_files(music_dir):
        rel = os.path.relpath(path, music_dir).replace(os.sep, "/")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        old = previous.get(rel)
        if old and not force and abs(old.get("mtime", -1) - mtime) < 1e-6:
            tracks.append(old)
            continue
        try:
            y = decoder(path)
            info = analyse_samples(y)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the scan
            log(f"⚠️  {rel}: could not analyse ({exc})")
            continue
        side = _sidecar(path)
        entry = {
            "path": rel, "file": os.path.basename(path), "mood": mood, "mtime": mtime,
            "source": side.get("source"), "licence": side.get("licence"),
            "attribution_required": bool(side.get("attribution_required", False)),
            "attribution": side.get("attribution"), "url": side.get("url"),
            "uses": int(old.get("uses", 0)) if old else 0,
            **info,
        }
        if normalise:
            try:
                norm = _normalise_copy(path, music_dir, runner=runner)
                entry["normalised"] = os.path.relpath(norm, music_dir).replace(os.sep, "/")
            except Exception as exc:  # noqa: BLE001
                log(f"⚠️  {rel}: normalisation failed ({exc}); using the original")
        tracks.append(entry)
        flags = f"  ⚑ {'; '.join(info['flags'])}" if info.get("flags") else ""
        log(f"🎵 {rel}: {info['duration']:.0f} s, {info['bpm']:.0f} BPM, "
            f"conf {info['beat_confidence']:.2f}, {info['loudness_db']:.0f} dB{flags}")
    manifest = {"tracks": tracks, "scanned_at": time.time(), "moods": list(MOODS)}
    save_manifest(manifest, music_dir)
    return manifest


# --- selection --------------------------------------------------------------

def _fits(track, duration, beat_sync):
    import beat_grid
    reasons = []
    if track.get("duration", 0) < duration and not track.get("loop_safe"):
        reasons.append("too short and not loop-safe")
    if beat_sync:
        bpm = track.get("bpm") or 0
        if not beat_grid.BEAT_BPM_RANGE[0] <= bpm <= beat_grid.BEAT_BPM_RANGE[1]:
            reasons.append(f"bpm {bpm:.0f} outside {beat_grid.BEAT_BPM_RANGE}")
        if (track.get("beat_confidence") or 0) < beat_grid.BEAT_CONFIDENCE_MIN:
            reasons.append(f"beat confidence {track.get('beat_confidence', 0):.2f} < {beat_grid.BEAT_CONFIDENCE_MIN}")
    return reasons


def _rank(track, duration):
    """Lower is better: least used first, then a track long enough to cover
    the short outright, then the 100-140 BPM sweet spot."""
    covers = 0 if track.get("duration", 0) >= duration else 1
    bpm = track.get("bpm") or 0
    sweet = 0 if 100 <= bpm <= 140 else 1
    return (int(track.get("uses", 0)), covers, sweet, track.get("path", ""))


def pick(mood, duration, beat_sync=True, music_dir=None, manifest=None):
    """Choose a bed. Returns None only when the library is empty. Otherwise a
    dict with ``track`` (manifest entry), ``path`` (normalised copy when it
    exists), ``mood_used``, ``fallback`` (bool), ``tier`` (``beat`` when the
    track passed the beat filters, else ``energy``/``free``) and ``reasons``
    explaining why the asked-for mood did not fit."""
    import beat_grid
    music_dir = music_dir or MUSIC_DIR
    manifest = manifest or load_manifest(music_dir)
    tracks = manifest.get("tracks") or []
    if not tracks:
        return None
    mood = (mood or "").lower()
    order = [m for m in (mood, FALLBACK_MOOD.get(mood)) if m] + ["any"] + [m for m in MOODS if m not in (mood, FALLBACK_MOOD.get(mood))]
    reasons = {}
    for strict in (True, False):  # first honour the beat filters, then relax them
        for m in order:
            pool = [t for t in tracks if t.get("mood") == m]
            fitting = []
            for t in pool:
                why = _fits(t, duration, beat_sync and strict)
                if why:
                    reasons[t["path"]] = why
                else:
                    fitting.append(t)
            if fitting:
                best = sorted(fitting, key=lambda t: _rank(t, duration))[0]
                tier = beat_grid.tier_for(best) if beat_sync else "free"
                if not strict and tier == "beat":
                    tier = "energy"
                rel = best.get("normalised") or best["path"]
                return {
                    "track": best, "path": os.path.join(music_dir, rel.replace("/", os.sep)),
                    "mood_used": m, "fallback": m != mood, "tier": tier,
                    "loop": best.get("duration", 0) < duration,
                    "reasons": reasons,
                    "hint": music_hint(mood) if m != mood else None,
                }
    return None


def record_use(track_path, music_dir=None):
    """Bump ``uses`` so the strongest bed does not land on every video."""
    music_dir = music_dir or MUSIC_DIR
    manifest = load_manifest(music_dir)
    for t in manifest["tracks"]:
        if t["path"] == track_path:
            t["uses"] = int(t.get("uses", 0)) + 1
            break
    save_manifest(manifest, music_dir)
    return manifest


def music_hint(mood):
    """The search recipe for a mood, with a prefilled Pixabay link."""
    recipe = MOOD_RECIPES.get((mood or "").lower())
    if not recipe:
        return {"mood": mood, "primary": [], "alternates": [], "instruments": "", "bpm": None,
                "bed_terms": BED_TERMS, "avoid_terms": AVOID_TERMS, "pixabay": None}
    query = recipe["primary"][0]
    return {
        "mood": mood, **recipe,
        "bed_terms": BED_TERMS, "avoid_terms": AVOID_TERMS,
        "pixabay": PIXABAY_SEARCH.format(query=quote(query)),
        "sources": [{**s, "search": s["search"].format(query=quote(query)) if s.get("search") else None}
                    for s in SOURCES],
        "folder": f"assets/music/{mood}/",
        "sidecar": {"source": "pixabay", "licence": "Pixabay Content License",
                    "attribution_required": False, "url": "https://pixabay.com/music/..."},
    }


def upload_track(filename, fileobj, mood, meta=None, music_dir=None, normalise=True, log=None):
    """Put one uploaded track into ``<music_dir>/<mood>/``, write its licence
    sidecar, and analyse it into the manifest (only the new file is decoded;
    unchanged tracks keep their entries). Returns the manifest entry."""
    import music
    music_dir = music_dir or MUSIC_DIR
    mood = (mood or "any").lower()
    if mood != "any" and mood not in MOODS:
        raise ValueError(f"mood must be one of {', '.join(MOODS)} or any")
    folder = music_dir if mood == "any" else os.path.join(music_dir, mood)
    saved = music.save_track(filename, fileobj, music_dir=folder)   # ValueError on a bad file
    path = os.path.join(folder, saved["file"])
    meta = {k: v for k, v in (meta or {}).items()
            if k in ("source", "licence", "attribution_required", "attribution", "url") and v not in (None, "")}
    if meta:
        with open(os.path.splitext(path)[0] + ".json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
    manifest = scan(music_dir, normalise=normalise, log=log or (lambda *_: None))
    rel = os.path.relpath(path, music_dir).replace(os.sep, "/")
    entry = next((t for t in manifest["tracks"] if t["path"] == rel), None)
    if entry is None:
        raise ValueError("the track was saved but could not be analysed; is it a readable audio file?")
    return {k: v for k, v in entry.items() if k not in ("beats", "energy")}


def library_summary(music_dir=None, manifest=None):
    manifest = manifest or load_manifest(music_dir)
    counts = {m: 0 for m in MOODS}
    counts["any"] = 0
    for t in manifest.get("tracks", []):
        counts[t.get("mood", "any")] = counts.get(t.get("mood", "any"), 0) + 1
    return {"tracks": len(manifest.get("tracks", [])), "by_mood": counts,
            "scanned_at": manifest.get("scanned_at"),
            "thin": [m for m in MOODS if counts.get(m, 0) < 3]}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("scan", "summary", "hint"):
        print("usage: python -m music_library scan [--force] [--no-normalise] | summary | hint <mood>")
        return 2
    if argv[0] == "scan":
        scan(force="--force" in argv, normalise="--no-normalise" not in argv)
        print(json.dumps(library_summary(), indent=1))
    elif argv[0] == "summary":
        print(json.dumps(library_summary(), indent=1))
    else:
        print(json.dumps(music_hint(argv[1] if len(argv) > 1 else ""), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
