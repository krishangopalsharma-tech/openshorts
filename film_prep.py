"""Shared film layer for the Movie Shorts and Movie Recap modules.

A feature film arrives with its own subtitle file, so the transcription stage
of the main pipeline is skipped entirely: the SRT/VTT IS the transcript. This
module turns it into the internal transcript shape the rest of the tree
already understands (``{"language", "segments": [{"start", "end", "text",
"words"}]}``), so ``recut.virtual_transcript`` and
``subtitles.generate_ass_styled`` work on a film unchanged.

Standard-library only at import time (the CI environment has no ffmpeg, no
whisper); everything heavy is imported inside the function that needs it.

Pieces, in the order a session uses them:

- ``parse_subtitles`` / ``parse_srt`` / ``parse_vtt``: file -> cues.
- ``cues_to_transcript``: cues -> internal transcript, words spread evenly
  across each cue (a subtitle cue has no word timing; even spacing is what
  keeps per-word caption animation usable without an ASR pass).
- ``digest``: the compact time-indexed text the chat prompts carry. Drops
  cues under 3 words, merges cues under 1.2 s apart, buckets into 2-minute
  blocks. A two-hour film comes out around 8-12k words.
- ``detect_offset`` / ``probe_offset``: an SRT cut for a different release
  runs 2-25 s out and every beat inherits the error. ~60 s around the 25%
  mark is transcribed with a SMALL whisper build and matched against the cues
  by word text; the modal time difference is the offset.
- ``speed_command`` / ``run_speed`` / ``scale_transcript``: the 1.25x step.
  Speed happens BEFORE reframe (reframe_v2 emits sendcmd crop timelines and a
  PTS change afterwards lands every command on the wrong frame) and caption
  timings are divided by the factor afterwards, or they drift: invisible in
  the first 20 s, wrong by 120 s.
"""

import html
import math
import os
import re
import subprocess
import tempfile
from collections import Counter

SUBTITLE_EXTENSIONS = {".srt", ".vtt"}

DIGEST_BLOCK_SECONDS = 120.0
DIGEST_MIN_WORDS = 3
DIGEST_MERGE_GAP = 1.2

DEFAULT_SPEED = 1.25
# ffmpeg's atempo accepts 0.5-100 (older builds 0.5-2.0); we never need more.
SPEED_RANGE = (0.5, 2.0)

PROBE_SECONDS = 60.0
PROBE_AT_FRACTION = 0.25
PROBE_BIN_SECONDS = 0.25
PROBE_MAX_OFFSET = 60.0
PROBE_WHISPER_MODEL_ENV = "FILM_PROBE_WHISPER_MODEL"
PROBE_WHISPER_MODEL_DEFAULT = "small"


class SubtitleError(ValueError):
    """The subtitle file could not be read. Safe to surface as a 400 detail."""


# --- timestamps -------------------------------------------------------------

_TS_RE = re.compile(
    r"^\s*(?:(\d{1,3}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?\s*$")


def parse_timestamp(value):
    """``HH:MM:SS,mmm`` / ``HH:MM:SS.mmm`` / ``MM:SS`` / ``123.4`` -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    m = _TS_RE.match(text)
    if not m:
        raise SubtitleError(f"bad timestamp: {value!r}")
    hours, minutes, seconds, frac = m.groups()
    total = int(minutes) * 60 + int(seconds)
    if hours:
        total += int(hours) * 3600
    if frac:
        total += int(frac.ljust(3, "0")) / 1000.0
    return float(total)


def format_timestamp(seconds, hours=None):
    """Seconds -> ``MM:SS`` (or ``H:MM:SS`` when the value or ``hours`` asks)."""
    seconds = max(0.0, float(seconds))
    whole = int(seconds + 0.5)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if hours or (hours is None and h):
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# --- parsing ----------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")
_ARROW_RE = re.compile(r"\s*-->\s*")
_WS_RE = re.compile(r"\s+")


def _clean_cue_text(raw):
    text = _TAG_RE.sub("", raw)
    text = _ASS_TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        # Leading dialogue dashes are typography, not speech.
        line = re.sub(r"^-\s*", "", line)
        if line:
            lines.append(line)
    return _WS_RE.sub(" ", " ".join(lines)).strip()


def _blocks(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if lines:
            yield lines


def parse_srt(text):
    """SRT text -> [{"start", "end", "text"}, ...] sorted by start."""
    cues = []
    for lines in _blocks(text):
        # Optional numeric index line.
        if lines and lines[0].strip().isdigit() and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        timing = lines[0].strip()
        parts = _ARROW_RE.split(timing, maxsplit=1)
        if len(parts) != 2:
            continue
        try:
            start = parse_timestamp(parts[0])
            end = parse_timestamp(parts[1].split()[0])
        except SubtitleError:
            continue
        body = _clean_cue_text("\n".join(lines[1:]))
        if body and end > start:
            cues.append({"start": start, "end": end, "text": body})
    cues.sort(key=lambda c: c["start"])
    return cues


def parse_vtt(text):
    """WebVTT text -> cues. Header, NOTE/STYLE blocks and cue ids are skipped."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.lstrip().startswith("WEBVTT"):
        raise SubtitleError("not a WebVTT file (missing WEBVTT header)")
    cues = []
    for lines in _blocks(text):
        head = lines[0].strip()
        if head.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        # Cue identifier line before the timing.
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if "-->" not in lines[0]:
            continue
        parts = _ARROW_RE.split(lines[0].strip(), maxsplit=1)
        if len(parts) != 2:
            continue
        try:
            start = parse_timestamp(parts[0])
            end = parse_timestamp(parts[1].split()[0])
        except SubtitleError:
            continue
        body = _clean_cue_text("\n".join(lines[1:]))
        if body and end > start:
            cues.append({"start": start, "end": end, "text": body})
    cues.sort(key=lambda c: c["start"])
    return cues


def parse_subtitles(path):
    """Read an ``.srt`` or ``.vtt`` file (any common encoding) into cues."""
    ext = os.path.splitext(str(path))[1].lower()
    if ext not in SUBTITLE_EXTENSIONS:
        raise SubtitleError(f"unsupported subtitle type {ext or '(none)'}; use .srt or .vtt")
    with open(path, "rb") as fh:
        raw = fh.read()
    text = None
    # utf-16 only with a BOM: without one the codec happily "decodes" any
    # even-length cp1252 file into CJK noise and the cue parser sees nothing.
    encodings = ["utf-8-sig", "cp1252", "latin-1"]
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings.insert(0, "utf-16")
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        raise SubtitleError("could not decode subtitle file")
    cues = parse_vtt(text) if ext == ".vtt" else parse_srt(text)
    if not cues:
        raise SubtitleError("subtitle file has no cues")
    return cues


def shift_cues(cues, offset):
    """Return cues moved by ``offset`` seconds (positive = later), clamped at 0."""
    out = []
    for c in cues:
        start = max(0.0, c["start"] + offset)
        end = max(start, c["end"] + offset)
        if end > start:
            out.append({**c, "start": start, "end": end})
    return out


# --- internal transcript ----------------------------------------------------

def _split_words(text):
    return [w for w in text.split(" ") if w]


def cues_to_transcript(cues, language="en", offset=0.0):
    """Cues -> the whisper-shaped transcript the rest of the tree consumes.

    Each cue becomes one segment; its words are spread evenly across the cue
    (weighted by character count, so a long word gets a longer slot). Words
    carry whisper's leading-space convention (``" word"``), which is what
    ``subtitles.merge_continuation_words`` keys on.
    """
    segments = []
    for cue in shift_cues(cues, offset):
        words = _split_words(cue["text"])
        if not words:
            continue
        start, end = float(cue["start"]), float(cue["end"])
        span = max(0.05, end - start)
        weights = [max(1, len(w)) for w in words]
        total = float(sum(weights))
        t = start
        timed = []
        for w, weight in zip(words, weights):
            dur = span * weight / total
            timed.append({"word": " " + w, "start": round(t, 3),
                          "end": round(min(end, t + dur), 3)})
            t += dur
        segments.append({"start": start, "end": end, "text": cue["text"],
                         "words": timed})
    return {"language": language, "segments": segments,
            "source": "subtitles", "offset": float(offset)}


def transcript_duration(transcript):
    ends = [s["end"] for s in transcript.get("segments", [])]
    return max(ends) if ends else 0.0


# --- digest -----------------------------------------------------------------

def merge_cues(cues, gap=DIGEST_MERGE_GAP, min_words=DIGEST_MIN_WORDS):
    """Drop tiny cues and join cues that follow each other within ``gap`` s.

    Interjections ("No.", "Yes?") carry no story and cost prompt tokens; a line
    split across two cues for display reads as one sentence to the model.
    """
    merged = []
    for cue in cues:
        text = cue["text"].strip()
        if len(_split_words(text)) < min_words and not (
                merged and cue["start"] - merged[-1]["end"] <= gap):
            continue
        if merged and cue["start"] - merged[-1]["end"] <= gap:
            prev = merged[-1]
            prev["end"] = max(prev["end"], cue["end"])
            prev["text"] = (prev["text"] + " " + text).strip()
        else:
            merged.append({"start": cue["start"], "end": cue["end"], "text": text})
    return [c for c in merged if len(_split_words(c["text"])) >= min_words]


def digest(cues, start=None, end=None, block_seconds=DIGEST_BLOCK_SECONDS,
           gap=DIGEST_MERGE_GAP, min_words=DIGEST_MIN_WORDS):
    """Time-indexed text for the chat prompts.

    ``[00:14:00-00:16:00]`` block headers, one ``MM:SS text`` line per merged
    cue. ``start``/``end`` restrict it to one band (a Pass C or beats pass only
    needs its act). Returns a string.
    """
    lo = 0.0 if start is None else float(start)
    hi = math.inf if end is None else float(end)
    selected = [c for c in cues if c["end"] > lo and c["start"] < hi]
    merged = merge_cues(selected, gap=gap, min_words=min_words)
    lines = []
    block = None
    for cue in merged:
        b = int(cue["start"] // block_seconds)
        if b != block:
            block = b
            b_start = b * block_seconds
            lines.append(f"[{format_timestamp(b_start, hours=True)}-"
                         f"{format_timestamp(b_start + block_seconds, hours=True)}]")
        lines.append(f"{format_timestamp(cue['start'])} {cue['text']}")
    return "\n".join(lines)


def digest_word_count(text):
    return len(text.split())


# --- offset detection -------------------------------------------------------

_WORD_NORM_RE = re.compile(r"[^\w']+", re.UNICODE)


def _norm(word):
    return _WORD_NORM_RE.sub("", str(word)).lower()


def detect_offset(cues, asr_words, window_start, window_end,
                  bin_seconds=PROBE_BIN_SECONDS, max_offset=PROBE_MAX_OFFSET,
                  min_word_len=3):
    """Modal ``cue_time - asr_time`` over words the ASR heard inside the window.

    ``asr_words``: ``[{"word", "start", "end"}]`` in VIDEO time. Each heard word
    is matched by text against the cue words in ``[window_start - max_offset,
    window_end + max_offset]`` (cue words get the even spacing from
    ``cues_to_transcript``); every match votes for its time difference, and
    the fullest ``bin_seconds`` bin wins. Text anchoring beats a pure onset
    cross-correlation because a film's SRT and its audio share the words, not
    the envelope (score, effects and laughter are not in the SRT).

    Returns ``{"offset": s, "confidence": 0-1, "matches": n, "votes": n}``;
    ``offset`` is what to ADD to the SRT to line it up with the video, and
    ``confidence`` is the share of votes inside the winning bin ±1. A result
    with fewer than ~8 matches or confidence under ~0.3 is a guess and the
    caller should say so.
    """
    lo, hi = window_start - max_offset, window_end + max_offset
    band = [c for c in cues if c["end"] > lo and c["start"] < hi]
    transcript = cues_to_transcript(band)
    cue_index = {}
    for seg in transcript["segments"]:
        for w in seg["words"]:
            key = _norm(w["word"])
            if len(key) >= min_word_len:
                cue_index.setdefault(key, []).append(w["start"])

    votes = []
    matched = 0
    for w in asr_words:
        key = _norm(w.get("word", ""))
        if len(key) < min_word_len or key not in cue_index:
            continue
        try:
            t = float(w["start"])
        except (KeyError, TypeError, ValueError):
            continue
        matched += 1
        for ct in cue_index[key]:
            # offset = shift applied to the SRT: srt + offset == video.
            d = t - ct
            if abs(d) <= max_offset:
                votes.append(d)

    if not votes:
        return {"offset": 0.0, "confidence": 0.0, "matches": matched, "votes": 0}

    bins = Counter(int(math.floor(d / bin_seconds)) for d in votes)
    best_bin, _ = max(bins.items(), key=lambda kv: (kv[1], -abs(kv[0])))
    near = [d for d in votes
            if best_bin - 1 <= int(math.floor(d / bin_seconds)) <= best_bin + 1]
    offset = sum(near) / len(near)
    confidence = len(near) / float(len(votes))
    return {"offset": round(offset, 2), "confidence": round(confidence, 3),
            "matches": matched, "votes": len(votes)}


def probe_window(duration, probe_seconds=PROBE_SECONDS, at=PROBE_AT_FRACTION):
    """The ``[start, end]`` slice the offset probe transcribes."""
    duration = max(0.0, float(duration))
    probe = min(probe_seconds, duration)
    start = max(0.0, min(duration * at, duration - probe))
    return start, start + probe


def _default_transcriber(media_path):
    """Transcribe a short slice with a small whisper build.

    The singleton in transcribe_backends is keyed by model size, so pointing
    ``WHISPER_MODEL`` at ``small`` for this call swaps the resident model; the
    next pipeline transcription swaps it back. A 60 s probe does not justify
    loading large-v3 on an 8 GB card that is about to run YOLO and NVENC.
    """
    from transcribe_backends import transcribe_media
    small = os.environ.get(PROBE_WHISPER_MODEL_ENV, PROBE_WHISPER_MODEL_DEFAULT)
    previous = os.environ.get("WHISPER_MODEL")
    os.environ["WHISPER_MODEL"] = small
    try:
        return transcribe_media(media_path)
    finally:
        if previous is None:
            os.environ.pop("WHISPER_MODEL", None)
        else:
            os.environ["WHISPER_MODEL"] = previous


def probe_offset(video_path, cues, duration, transcriber=None, workdir=None,
                 probe_seconds=PROBE_SECONDS, at=PROBE_AT_FRACTION):
    """Cut ``probe_seconds`` of audio around the 25% mark, transcribe it, and
    run ``detect_offset``. ``transcriber(path) -> transcript`` is injectable
    so tests never touch whisper. Never raises: a failed probe reports
    offset 0 with confidence 0 and an ``error``; the user can still type one.
    """
    start, end = probe_window(duration, probe_seconds, at)
    transcriber = transcriber or _default_transcriber
    tmpdir = workdir or tempfile.mkdtemp(prefix="film_probe_")
    slice_path = os.path.join(tmpdir, "probe.wav")
    try:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", video_path,
               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", slice_path]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=300)
        transcript = transcriber(slice_path)
    except Exception as exc:  # noqa: BLE001 - advisory feature, never fatal
        return {"offset": 0.0, "confidence": 0.0, "matches": 0, "votes": 0,
                "window": [start, end], "error": str(exc)[:300]}
    finally:
        if workdir is None:
            try:
                os.remove(slice_path)
                os.rmdir(tmpdir)
            except OSError:
                pass
    words = []
    for seg in (transcript or {}).get("segments", []) or []:
        for w in seg.get("words", []) or []:
            try:
                words.append({"word": w.get("word", ""),
                              "start": float(w["start"]) + start,
                              "end": float(w["end"]) + start})
            except (KeyError, TypeError, ValueError):
                continue
    result = detect_offset(cues, words, start, end)
    result["window"] = [start, end]
    return result


# --- speed ------------------------------------------------------------------

def speed_command(input_path, output_path, factor=DEFAULT_SPEED, fps=None,
                  encode_args=None, audio_args=None):
    """ffmpeg argv for a uniform speed change of video AND audio.

    ``setpts=PTS/f`` on the picture, ``atempo=f`` on the sound (pitch kept).
    Loudness normalisation is deliberately NOT applied here: the mix step
    later in the chain normalises once, on the finished audio.
    """
    factor = float(factor)
    if not SPEED_RANGE[0] <= factor <= SPEED_RANGE[1]:
        raise ValueError(f"speed factor {factor} outside {SPEED_RANGE}")
    if encode_args is None:
        from ffmpeg_utils import QUALITY_FAST, video_encode_args
        encode_args = video_encode_args(QUALITY_FAST)
    if audio_args is None:
        audio_args = ["-c:a", "aac"]
    vf = f"setpts=PTS/{factor:.4f}"
    if fps:
        vf += f",fps={int(fps)}"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", input_path,
           "-filter:v", vf, "-filter:a", f"atempo={factor:.4f}",
           *encode_args, *audio_args, "-movflags", "+faststart", output_path]
    return cmd


def run_speed(input_path, output_path, factor=DEFAULT_SPEED, fps=None, runner=None):
    cmd = speed_command(input_path, output_path, factor, fps=fps)
    runner = runner or (lambda c: subprocess.run(c, check=True, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.PIPE))
    runner(cmd)
    return output_path


def scale_transcript(transcript, factor):
    """Divide every time in a transcript by ``factor`` (after ``run_speed``)."""
    factor = float(factor)
    out = {k: v for k, v in transcript.items() if k != "segments"}
    out["segments"] = []
    for seg in transcript.get("segments", []):
        new = dict(seg)
        new["start"] = round(float(seg["start"]) / factor, 3)
        new["end"] = round(float(seg["end"]) / factor, 3)
        new["words"] = [
            {**w, "start": round(float(w["start"]) / factor, 3),
             "end": round(float(w["end"]) / factor, 3)}
            for w in seg.get("words", []) or []
        ]
        out["segments"].append(new)
    return out


def scale_ranges(ranges, factor):
    """Same for ``[{"start","end"}]`` lists (layout ranges, beat boundaries)."""
    factor = float(factor)
    return [{**r, "start": float(r["start"]) / factor, "end": float(r["end"]) / factor}
            for r in ranges]


# --- beats -> shots ---------------------------------------------------------
#
# The model plans BEATS (8-15 s of story). The renderer owns rhythm: each beat
# becomes 5-8 shots of 1.2-1.8 s finished, and what makes one continuous take
# read as several angles is that every shot gets a different crop scale. The
# reference channel's ~86 cuts per two minutes are mostly this, not 86 scenes.

SHOT_FINISHED_RANGE = (1.2, 1.8)   # seconds, finished timeline
SHOT_TARGET_FINISHED = 1.4
SHOTS_PER_BEAT_RANGE = (5, 8)
# Cycled, not random, so the edit stays deliberate. 1.70 is the extreme close
# crop that defines the look; never above SCALE_MAX (artefacts at 720p source).
CROP_LADDER = (1.00, 1.45, 1.15, 1.70, 1.25, 1.55)
SCALE_MAX = 1.8
# Where the crop window sits inside the frame, as fractions of the slack
# (0.5/0.5 = centred). Drifts between a face-height centre and thirds; the
# reframe stage later tracks the face inside whatever this leaves.
ANCHORS = ((0.5, 0.4), (0.38, 0.4), (0.62, 0.4), (0.5, 0.5), (0.45, 0.35), (0.55, 0.45))
COMPOSITE_SHARE = 0.12   # of shots, on key beats with composite_ok
KEY_BEAT_STRETCH = 1.15  # key beats and a beat's first shot run a little longer
LEAD_SHOT_STRETCH = 1.15


def _rng(seed):
    import random
    # random.Random only seeds from int/str/bytes; a (seed, index) tuple is
    # flattened to a string so shot jitter is reproducible per beat.
    return random.Random("|".join(str(p) for p in (seed if isinstance(seed, tuple) else (seed,))))


def _stretches(start, end, cuts, min_len):
    """Split ``[start, end]`` at real scene cuts, absorbing slivers shorter
    than ``min_len`` into their neighbour."""
    points = sorted({start, end, *[c for c in (cuts or []) if start < c < end]})
    out = []
    for a, b in zip(points, points[1:]):
        if out and (b - a) < min_len:
            out[-1] = (out[-1][0], b)
        elif not out or (out[-1][1] - out[-1][0]) >= min_len:
            out.append((a, b))
        else:
            out[-1] = (out[-1][0], b)
    return out or [(start, end)]


def _subdivide(length, target, lo, hi, rng, lead_stretch=1.0):
    """Shot lengths (source seconds) summing exactly to ``length``, each inside
    ``[lo, hi]`` where the arithmetic allows, jittered so the rhythm is not
    metronomic. The first shot is stretched by ``lead_stretch``."""
    if length <= hi:
        return [length]
    n = max(1, int(round(length / target)))
    n = max(n, int(math.ceil(length / hi)))
    n = min(n, int(math.floor(length / lo)) or 1)
    weights = [rng.uniform(0.85, 1.15) for _ in range(n)]
    weights[0] *= lead_stretch
    total = sum(weights)
    lengths = [length * w / total for w in weights]
    # Clamp into the band and spread the difference evenly; ``n`` was chosen
    # so the mean sits inside the band, so this converges in a few passes.
    for _ in range(8):
        lengths = [min(hi, max(lo, ln)) for ln in lengths]
        drift = length - sum(lengths)
        if abs(drift) < 1e-6:
            break
        lengths = [ln + drift / n for ln in lengths]
    lengths[-1] += length - sum(lengths)
    return lengths


def expand_beat(beat, beat_index, *, speed=DEFAULT_SPEED, scene_cuts=None,
                ladder_offset=0, anchor_offset=0, seed=0, durations=None):
    """One beat -> its shots (source time).

    ``beat``: ``{"start", "end", "weight", "composite_ok", "captions"}``.
    ``scene_cuts``: real cut times inside the beat; they become boundaries
    first (free and always correct). ``durations``: an explicit list of
    source-second shot lengths from the beat scheduler (film_beat_sync); when
    given the free jitter is skipped. Returns ``(shots, next_ladder_offset,
    next_anchor_offset)`` so a plan cycles the ladder ACROSS beats instead of
    restarting at 1.00 on every one (which would put a wide shot on every
    beat boundary and read as a template).
    """
    start, end = float(beat["start"]), float(beat["end"])
    key = beat.get("weight") == "key"
    stretch = KEY_BEAT_STRETCH if key else 1.0
    lo, hi = SHOT_FINISHED_RANGE[0] * speed, SHOT_FINISHED_RANGE[1] * speed
    target = SHOT_TARGET_FINISHED * speed * stretch
    rng = _rng((seed, beat_index))

    pieces = []
    if durations:
        t = start
        for d in durations:
            if t >= end - 1e-6:
                break
            pieces.append((t, min(end, t + float(d))))
            t = min(end, t + float(d))
        if t < end - 0.3:
            pieces.append((t, end))
    else:
        first = True
        for a, b in _stretches(start, end, scene_cuts, lo):
            for ln in _subdivide(b - a, target, lo, hi, rng,
                                 lead_stretch=LEAD_SHOT_STRETCH if first else 1.0):
                pieces.append((a, a + ln))
                a += ln
            first = False

    # Which shot carries the beat's key line: the one under the first caption.
    key_at = None
    caps = beat.get("captions") or []
    if caps:
        try:
            key_at = float(caps[0].get("at", caps[0]) if isinstance(caps[0], dict) else caps[0])
        except (TypeError, ValueError):
            key_at = None

    shots = []
    li, ai = ladder_offset, anchor_offset
    prev_scale = None
    for si, (a, b) in enumerate(pieces):
        scale = CROP_LADDER[li % len(CROP_LADDER)]
        li += 1
        if scale == prev_scale:  # never two identical scales adjacent
            scale = CROP_LADDER[li % len(CROP_LADDER)]
            li += 1
        anchor = ANCHORS[ai % len(ANCHORS)]
        ai += 1
        shots.append({
            "beat": beat_index,
            "index": si,
            "start": round(a, 3),
            "end": round(b, 3),
            "scale": min(SCALE_MAX, scale),
            "anchor": [anchor[0], anchor[1]],
            "key_line": bool(key_at is not None and a <= key_at < b),
            "composite": False,
            "blur": None,
        })
        prev_scale = scale
    return shots, li, ai


def expand_plan(beats, *, speed=DEFAULT_SPEED, scene_cuts_for=None, seed=0,
                composite_share=COMPOSITE_SHARE, schedule=None):
    """Every beat -> shots, ladder and anchors cycling across the plan, a
    composite quota placed on key beats that allow it.

    ``scene_cuts_for(start, end) -> [times]`` is injectable (the default is no
    internal cuts; film_render wires scene_detection in). ``schedule`` maps a
    beat index to explicit source durations (beat-sync mode).
    """
    shots = []
    li = ai = 0
    for i, beat in enumerate(beats):
        cuts = scene_cuts_for(beat["start"], beat["end"]) if scene_cuts_for else None
        durations = schedule.get(i) if schedule else None
        got, li, ai = expand_beat(beat, i, speed=speed, scene_cuts=cuts,
                                  ladder_offset=li, anchor_offset=ai, seed=seed,
                                  durations=durations)
        shots.extend(got)

    quota = int(round(len(shots) * composite_share))
    candidates = [s for s in shots
                  if beats[s["beat"]].get("weight") == "key"
                  and beats[s["beat"]].get("composite_ok", True)
                  and not s["key_line"] and s["index"] > 0]
    if len(candidates) < quota:
        candidates += [s for s in shots
                       if beats[s["beat"]].get("composite_ok", True)
                       and s not in candidates and not s["key_line"] and s["index"] > 0]
    rng = _rng((seed, "composite"))
    for s in rng.sample(candidates, min(quota, len(candidates))):
        s["composite"] = True
    for n, s in enumerate(shots):
        s["id"] = n
    return shots


def shots_summary(shots, speed=DEFAULT_SPEED):
    lengths = [s["end"] - s["start"] for s in shots]
    if not lengths:
        return {"shots": 0}
    finished = [ln / speed for ln in lengths]
    return {
        "shots": len(shots),
        "source_seconds": round(sum(lengths), 2),
        "finished_seconds": round(sum(finished), 2),
        "mean_shot_finished": round(sum(finished) / len(finished), 3),
        "min_shot_finished": round(min(finished), 3),
        "max_shot_finished": round(max(finished), 3),
        "composites": sum(1 for s in shots if s["composite"]),
        "scales": sorted({s["scale"] for s in shots}),
    }


def crop_filter(scale, anchor):
    """ffmpeg ``crop`` for a static punch-in: window = frame / scale, placed
    at ``anchor`` fractions of the slack. scale 1.0 is a no-op."""
    scale = max(1.0, min(SCALE_MAX, float(scale)))
    if scale <= 1.0 + 1e-9:
        return None
    ax, ay = (max(0.0, min(1.0, float(v))) for v in anchor)
    return (f"crop=trunc(iw/{scale:.3f}/2)*2:trunc(ih/{scale:.3f}/2)*2:"
            f"trunc((iw-iw/{scale:.3f})*{ax:.3f}/2)*2:trunc((ih-ih/{scale:.3f})*{ay:.3f}/2)*2")
