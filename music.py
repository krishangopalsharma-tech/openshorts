"""Background music: the track library and the one ffmpeg pass that mixes a
track under a finished clip's voice.

Ported from ClipForge (app/music.py for the library, clipper._music_audio_graph
for the mix), with the loudness exposed in dB instead of 0-100. The mix is
"reels-style": the voice is split, one copy keys a ``sidechaincompress`` that
ducks the music whenever someone talks, the other copy is mixed back at unity
(``amix normalize=0``), so the dialogue always cuts through and the music fills
the gaps. The track loops for the clip's length, starts ``start`` seconds in,
and fades out over the last second.

In OpenShorts this is a post-generation layer (``mu_<hex>_<clean>``,
see app.py's layer chain): the video stream is copied, only the audio is
re-encoded, so a music change costs a couple of seconds per clip and never
touches the pixels under the captions.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

MUSIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "music")

ALLOWED_MUSIC_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}
# Video containers are accepted too: only the audio is kept (ripped to .m4a).
ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MAX_TRACK_BYTES = 40 * 1024 * 1024

DEFAULTS = {"volume_db": -18.0, "duck": 70.0, "start": 0.0, "fade_out": 1.0}
VOLUME_DB_RANGE = (-40.0, 0.0)

_tracks_cache = {"stamp": None, "tracks": []}
_tracks_lock = threading.Lock()


def _pretty(name):
    stem = os.path.splitext(name)[0]
    # Pixabay-style "artist-title-123456": drop the trailing id.
    stem = re.sub(r"[-_]\d{4,}$", "", stem)
    return re.sub(r"[_\-]+", " ", stem).strip() or stem


def probe_duration(path, timeout=30):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path], stderr=subprocess.STDOUT, timeout=timeout)
        return round(float(out.decode().strip()), 2)
    except Exception:
        return None


def has_audio_stream(path, timeout=30):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "csv=p=0", path], stderr=subprocess.STDOUT, timeout=timeout)
        return bool(out.decode().strip())
    except Exception:
        return True  # assume voice is there; the mix graph tolerates it


def list_tracks(music_dir=None):
    """``[{name, file, duration}]`` for every track in the library, sorted by
    name. Scans the folder (so files pasted in by hand show up) and caches on
    its mtime so the endpoint does not ffprobe every track per call."""
    music_dir = music_dir or MUSIC_DIR
    try:
        names = sorted((n for n in os.listdir(music_dir)
                        if os.path.splitext(n)[1].lower() in ALLOWED_MUSIC_EXTS),
                       key=str.lower)
        stamp = (music_dir, os.stat(music_dir).st_mtime_ns, tuple(names))
    except OSError:
        return []
    with _tracks_lock:
        if _tracks_cache["stamp"] == stamp:
            return [dict(t) for t in _tracks_cache["tracks"]]
        known = {t["file"]: t for t in _tracks_cache["tracks"]}
        tracks = []
        for name in names:
            prev = known.get(name)
            duration = prev["duration"] if prev else probe_duration(os.path.join(music_dir, name))
            tracks.append({"name": _pretty(name), "file": name, "duration": duration})
        _tracks_cache["stamp"] = stamp
        _tracks_cache["tracks"] = tracks
        return [dict(t) for t in tracks]


def resolve_track(track, music_dir=None):
    """Absolute path of a library track, or None. Only the bare filename is
    trusted: a path never escapes the library."""
    music_dir = music_dir or MUSIC_DIR
    name = os.path.basename(str(track or ""))
    if not name or os.path.splitext(name)[1].lower() not in ALLOWED_MUSIC_EXTS:
        return None
    candidate = os.path.abspath(os.path.join(music_dir, name))
    if not candidate.startswith(os.path.abspath(music_dir) + os.sep) or not os.path.isfile(candidate):
        return None
    return candidate


def _safe_stem(name):
    stem = os.path.splitext(os.path.basename(name))[0]
    stem = re.sub(r"[^A-Za-z0-9 _().-]+", "", stem).strip()[:80]
    return stem or f"track_{uuid.uuid4().hex[:6]}"


def _unique_path(music_dir, stem, ext):
    dest = os.path.join(music_dir, f"{stem}{ext}")
    i = 2
    while os.path.exists(dest):
        dest = os.path.join(music_dir, f"{stem} ({i}){ext}")
        i += 1
    return dest


def save_track(filename, fileobj, music_dir=None):
    """Store an uploaded track. Audio files are kept as-is; a video file has
    its audio ripped to .m4a and the video is never stored. Returns the new
    ``{name, file, duration}`` entry. Raises ValueError for a bad type/size."""
    music_dir = music_dir or MUSIC_DIR
    os.makedirs(music_dir, exist_ok=True)
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_MUSIC_EXTS and ext not in ALLOWED_VIDEO_EXTS:
        raise ValueError("Unsupported file type; use mp3, m4a, wav, aac, ogg, flac or a video file.")
    stem = _safe_stem(filename)
    tmp = os.path.join(music_dir, f".upload_{uuid.uuid4().hex}{ext}")
    size = 0
    with open(tmp, "wb") as out:
        while True:
            chunk = fileobj.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_TRACK_BYTES:
                out.close()
                os.remove(tmp)
                raise ValueError("Track is too large (max 40 MB).")
            out.write(chunk)
    try:
        if ext in ALLOWED_VIDEO_EXTS:
            dest = _unique_path(music_dir, stem, ".m4a")
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-vn", "-c:a", "aac", "-b:a", "192k", dest],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
            if result.returncode != 0 or not os.path.exists(dest):
                raise ValueError("Could not extract audio from that video.")
        else:
            dest = _unique_path(music_dir, stem, ext)
            shutil.move(tmp, dest)
        if probe_duration(dest) is None:
            os.remove(dest)
            raise ValueError("That file is not a readable audio track.")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    name = os.path.basename(dest)
    return {"name": _pretty(name), "file": name, "duration": probe_duration(dest)}


def normalize(spec):
    """Validated music spec or None when there is no track. Unknown keys are
    dropped, numbers clamped: volume_db -40..0, duck 0..100, start >= 0."""
    if not isinstance(spec, dict):
        return None
    track = os.path.basename(str(spec.get("track") or "").strip())
    if not track:
        return None
    out = {"track": track}
    try:
        db = float(spec.get("volume_db", DEFAULTS["volume_db"]))
    except (TypeError, ValueError):
        db = DEFAULTS["volume_db"]
    out["volume_db"] = round(min(VOLUME_DB_RANGE[1], max(VOLUME_DB_RANGE[0], db)), 1)
    try:
        duck = float(spec.get("duck", DEFAULTS["duck"]))
    except (TypeError, ValueError):
        duck = DEFAULTS["duck"]
    out["duck"] = round(min(100.0, max(0.0, duck)), 1)
    try:
        start = float(spec.get("start", DEFAULTS["start"]))
    except (TypeError, ValueError):
        start = 0.0
    out["start"] = round(max(0.0, start), 2)
    try:
        fade = float(spec.get("fade_out", DEFAULTS["fade_out"]))
    except (TypeError, ValueError):
        fade = DEFAULTS["fade_out"]
    out["fade_out"] = round(min(5.0, max(0.0, fade)), 2)
    # Optional sidechain shape (MIX_PROFILES). Absent = "voice", the reels
    # mix; a film montage stores "dialogue" so the bed dips under the film's
    # lines instead of vanishing.
    profile = str(spec.get("profile") or "").strip()
    if profile in MIX_PROFILES and profile != "voice":
        out["profile"] = profile
    # Climax tail (film_montage): from ``climax_at`` seconds into the clip the
    # clip's own audio is muted and the bed rises to ``climax_db``; with
    # ``drop_at`` (seconds into the TRACK, from analyze_track) the start
    # offset is solved so the drop lands on ``climax_at``.
    try:
        climax_at = float(spec.get("climax_at")) if spec.get("climax_at") is not None else None
    except (TypeError, ValueError):
        climax_at = None
    if climax_at is not None and climax_at > 0:
        out["climax_at"] = round(climax_at, 3)
        try:
            cdb = float(spec.get("climax_db", CLIMAX_DB_DEFAULT))
        except (TypeError, ValueError):
            cdb = CLIMAX_DB_DEFAULT
        out["climax_db"] = round(min(VOLUME_DB_RANGE[1], max(out["volume_db"], cdb)), 1)
        try:
            drop = float(spec["drop_at"]) if spec.get("drop_at") is not None else None
        except (TypeError, ValueError):
            drop = None
        if drop is not None and drop >= 0:
            out["drop_at"] = round(drop, 3)
    return out


CLIMAX_DB_DEFAULT = -3.0
CLIMAX_RAMP_SECONDS = 0.35   # the bed swells over this, ending on the drop


def aligned_start(drop_at, climax_at, track_duration):
    """Seconds into the track to start at so ``drop_at`` plays at clip time
    ``climax_at``. The track loops (``-stream_loop -1``), so a drop earlier
    in the track than the climax is in the clip wraps around."""
    if not track_duration or track_duration <= 0:
        return max(0.0, drop_at - climax_at)
    return round((drop_at - climax_at) % track_duration, 3)


# Sidechain shapes. ``voice`` is the reels mix a talking head wants: the
# track all but vanishes under speech (ratio up to 20). ``dialogue`` is the
# movie-shorts bed (docs/film-modules-plan.md): film dialogue keys it, the bed
# should DIP, not vanish (ratio capped at 6), and release sits at 260 ms —
# under ~150 the bed pumps between words, over ~500 it never recovers between
# lines. ``narration`` is the recap voiceover over a kept dialogue bed.
MIX_PROFILES = {
    "voice": {"threshold": 0.02, "attack": 15, "release": 300, "max_ratio": 20.0},
    "dialogue": {"threshold": 0.05, "attack": 8, "release": 260, "max_ratio": 6.0},
    "narration": {"threshold": 0.03, "attack": 5, "release": 300, "max_ratio": 12.0},
}


def build_audio_graph(spec, duration, voice=True, profile="voice"):
    """The -filter_complex for input 0 = clip, input 1 = the looped track.

    ``duck`` scales the sidechain ratio: 0 leaves the music steady (ratio 1),
    100 pulls it down to the profile's ``max_ratio`` whenever the voice is
    above threshold. ``normalize=0`` keeps the voice at unity instead of amix
    halving both. Produces ``[aout]``.
    """
    p = MIX_PROFILES.get(profile) or MIX_PROFILES["voice"]
    ratio = 1.0 + (spec["duck"] / 100.0) * (p["max_ratio"] - 1.0)
    fade = spec["fade_out"]
    fade_chain = (f",afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f}"
                  if fade > 0 and duration > fade else "")
    climax_at = spec.get("climax_at")
    swell = ""
    mute = ""
    if climax_at is not None and 0 < climax_at < duration:
        # Linear gain from 1 to the climax level over the ramp that ENDS on
        # climax_at, then held. Single quotes keep the commas inside the
        # expression out of the filtergraph parser.
        gain = 10 ** ((spec.get("climax_db", CLIMAX_DB_DEFAULT) - spec["volume_db"]) / 20.0)
        r = CLIMAX_RAMP_SECONDS
        swell = (f",volume='1+{gain - 1.0:.4f}*clip((t-{climax_at - r:.3f})/{r:.3f},0,1)'"
                 f":eval=frame")
        mute = f"volume=0:enable='gte(t,{climax_at:.3f})',"
    music = f"[1:a]volume={spec['volume_db']:.1f}dB,aresample=async=1[__m]"
    if not voice:
        return f"{music};[__m]atrim=0:{duration:.3f}{swell}{fade_chain}[aout]"
    return (
        f"[0:a]{mute}asplit=2[__v1][__v2];{music};"
        f"[__m][__v2]sidechaincompress=threshold={p['threshold']}:ratio={ratio:.2f}"
        f":attack={p['attack']}:release={p['release']}[__md];"
        f"[__md]atrim=0:{duration:.3f}{swell}{fade_chain}[__mf];"
        f"[__v1][__mf]amix=inputs=2:duration=first:normalize=0[aout]"
    )


def apply_music(video_path, spec, output_path, music_dir=None, profile="voice",
                track_path=None):
    """Mix ``spec['track']`` under ``video_path`` into ``output_path``. Video
    is stream-copied. Returns True on success, False (nothing written) on
    any failure, the same fail-open contract as the other layers.

    ``track_path`` bypasses the library lookup (the film modules pick from
    their own mood-sorted manifest); ``profile`` picks the sidechain shape."""
    spec = normalize(spec if track_path is None else {**(spec or {}), "track": os.path.basename(track_path)})
    if not spec:
        return False
    track = track_path if track_path and os.path.exists(track_path) else resolve_track(spec["track"], music_dir)
    if not track:
        print(f"   ⚠️ Music track not found: {spec['track']}")
        return False
    duration = probe_duration(video_path)
    if not duration:
        return False
    voice = has_audio_stream(video_path)
    graph = build_audio_graph(spec, duration, voice=voice, profile=profile)
    start = spec["start"]
    if spec.get("climax_at") is not None and spec.get("drop_at") is not None:
        start = aligned_start(spec["drop_at"], spec["climax_at"], probe_duration(track))
    seek = ["-ss", f"{start:.3f}"] if start > 0 else []
    tmp = output_path + ".tmp.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-i", video_path,
           "-stream_loop", "-1", *seek, "-i", track,
           "-filter_complex", graph,
           "-map", "0:v:0", "-map", "[aout]",
           "-t", f"{duration:.3f}",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", tmp]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
    if result.returncode == 0 and os.path.exists(tmp):
        os.replace(tmp, output_path)
        return True
    err = (result.stderr or b"").decode(errors="ignore")[-300:]
    print(f"   ⚠️ Music mix failed (clip kept as is): {err}")
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


# --- beat / drop analysis ---------------------------------------------------------
#
# No librosa: the two numbers the climax tail needs (where the drop is, how
# long a beat is) come from an energy envelope in numpy. Cached per file
# (name + size + mtime) in ``<music dir>/.analysis.json`` so a track is
# decoded once.

ANALYSIS_SR = 11025
ANALYSIS_HOP = 256            # ~23 ms frames
TEMPO_RANGE_BPM = (70.0, 180.0)
_analysis_lock = threading.Lock()


def _decode_mono(path, sr=ANALYSIS_SR, max_seconds=600):
    import numpy as np
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-t", str(max_seconds), "-ac", "1", "-ar", str(sr),
           "-f", "f32le", "-"]
    raw = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=300).stdout
    return np.frombuffer(raw, dtype=np.float32)


def analyze_samples(y, sr=ANALYSIS_SR, hop=ANALYSIS_HOP):
    """``{"duration", "beat_period", "bpm", "drop_at"}`` from mono samples.

    beat period: autocorrelation of the onset envelope (positive difference
    of log frame energy) over 70-180 bpm. drop: the frame where the mean
    energy of the next 2 s most exceeds the previous 4 s, searched away from
    the first and last 4 s, then snapped to the strongest onset within one
    beat. Either value is None when the audio is too short or flat."""
    import numpy as np
    duration = len(y) / float(sr)
    out = {"duration": round(duration, 3), "beat_period": None, "bpm": None, "drop_at": None}
    n = len(y) // hop
    if n < 64:
        return out
    frames = y[: n * hop].reshape(n, hop)
    energy = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)) + 1e-6
    loge = np.log(energy)
    onset = np.maximum(0.0, np.diff(loge, prepend=loge[0]))
    onset = onset - onset.mean()
    fps = sr / float(hop)

    lo = int(fps * 60.0 / TEMPO_RANGE_BPM[1])
    hi = int(fps * 60.0 / TEMPO_RANGE_BPM[0])
    if n > hi * 4:
        ac = np.correlate(onset, onset, mode="full")[n - 1: n - 1 + hi + 1]
        if ac[0] > 0:
            lag = lo + int(np.argmax(ac[lo: hi + 1]))
            frac = 0.0
            if 0 < lag < len(ac) - 1:
                # Parabolic peak: a 23 ms hop quantises the lag ~2%, which
                # drifts a beat by a frame every few cuts.
                a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
                den = a - 2 * b + c
                if den < 0:
                    frac = max(-0.5, min(0.5, 0.5 * (a - c) / den))
            period = float((lag + frac) / fps)
            out["beat_period"] = round(period, 4)
            out["bpm"] = round(60.0 / period, 1)

    pre, post, edge = int(4 * fps), int(2 * fps), int(4 * fps)
    if n > pre + post + 2 * edge:
        c = np.concatenate([[0.0], np.cumsum(energy)])
        idx = np.arange(max(pre, edge), n - max(post, edge))
        before = (c[idx] - c[idx - pre]) / pre
        after = (c[idx + post] - c[idx]) / post
        ratio = after / before
        if ratio.size and float(ratio.max()) > 1.25:
            f = int(idx[int(np.argmax(ratio))])
            # Snap to the hit that starts the step, never back a beat: a
            # symmetric +-1 beat window picked the last pre-drop kick on a
            # real phonk track (60.1 s where the bed jumped at 60.5 s).
            w = int((out["beat_period"] or 0.5) * fps)
            a, b = max(0, f - int(0.15 * fps)), min(n, f + w // 2 + 1)
            f = a + int(np.argmax(onset[a:b]))
            out["drop_at"] = round(f / fps, 3)
    return out


def analyze_track(path):
    """Cached :func:`analyze_samples` for a track file; ``{}`` on failure."""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    cache_path = os.path.join(os.path.dirname(path), ".analysis.json")
    key = f"{os.path.basename(path)}|{st.st_size}|{int(st.st_mtime)}"
    with _analysis_lock:
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}
        if key in cache:
            return cache[key]
    try:
        result = analyze_samples(_decode_mono(path))
    except Exception as exc:  # noqa: BLE001
        print(f"   ⚠️ Track analysis failed for {path}: {exc}")
        return {}
    with _analysis_lock:
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}
        cache = {k: v for k, v in cache.items() if not k.startswith(os.path.basename(path) + "|")}
        cache[key] = result
        try:
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, indent=1)
            os.replace(tmp, cache_path)
        except OSError:
            pass
    return result


def derived_name(clean_name):
    # Short prefix: filenames are byte-capped (255 on ext4) and every layer
    # nests the one below; resolution is by mtime, so no timestamp needed.
    return f"mu_{uuid.uuid4().hex[:6]}_{clean_name}"
