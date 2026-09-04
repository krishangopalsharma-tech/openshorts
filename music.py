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
    return out


def build_audio_graph(spec, duration, voice=True):
    """The -filter_complex for input 0 = clip, input 1 = the looped track.

    ``duck`` scales the sidechain ratio: 0 leaves the music steady (ratio 1),
    100 pulls it down hard (ratio 20) whenever the voice is above threshold.
    ``normalize=0`` keeps the voice at unity instead of amix halving both.
    Produces ``[aout]``.
    """
    ratio = 1.0 + (spec["duck"] / 100.0) * 19.0
    fade = spec["fade_out"]
    fade_chain = (f",afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f}"
                  if fade > 0 and duration > fade else "")
    music = f"[1:a]volume={spec['volume_db']:.1f}dB,aresample=async=1[__m]"
    if not voice:
        return f"{music};[__m]atrim=0:{duration:.3f}{fade_chain}[aout]"
    return (
        f"[0:a]asplit=2[__v1][__v2];{music};"
        f"[__m][__v2]sidechaincompress=threshold=0.02:ratio={ratio:.2f}:attack=15:release=300[__md];"
        f"[__md]atrim=0:{duration:.3f}{fade_chain}[__mf];"
        f"[__v1][__mf]amix=inputs=2:duration=first:normalize=0[aout]"
    )


def apply_music(video_path, spec, output_path, music_dir=None):
    """Mix ``spec['track']`` under ``video_path`` into ``output_path``. Video
    is stream-copied. Returns True on success, False (nothing written) on
    any failure, the same fail-open contract as the other layers."""
    spec = normalize(spec)
    if not spec:
        return False
    track = resolve_track(spec["track"], music_dir)
    if not track:
        print(f"   ⚠️ Music track not found: {spec['track']}")
        return False
    duration = probe_duration(video_path)
    if not duration:
        return False
    voice = has_audio_stream(video_path)
    graph = build_audio_graph(spec, duration, voice=voice)
    seek = ["-ss", f"{spec['start']:.2f}"] if spec["start"] > 0 else []
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


def derived_name(clean_name):
    # Short prefix: filenames are byte-capped (255 on ext4) and every layer
    # nests the one below; resolution is by mtime, so no timestamp needed.
    return f"mu_{uuid.uuid4().hex[:6]}_{clean_name}"
