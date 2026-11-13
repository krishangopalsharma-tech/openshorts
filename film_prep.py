"""Film layer for Movie Recap: subtitles in, transcript and digest out.

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


ANCHOR_MIN = 5


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
        for k, w in enumerate(seg["words"]):
            key = _norm(w["word"])
            if len(key) >= min_word_len:
                # k == 0: the cue's first word starts exactly at the cue
                # start; later words only have the even spread's guess.
                cue_index.setdefault(key, []).append((w["start"], k == 0))

    votes = []
    matched = 0
    for wi, w in enumerate(asr_words):
        key = _norm(w.get("word", ""))
        if len(key) < min_word_len or key not in cue_index:
            continue
        try:
            t = float(w["start"])
        except (KeyError, TypeError, ValueError):
            continue
        matched += 1
        for ct, first in cue_index[key]:
            # offset = shift applied to the SRT: srt + offset == video.
            d = t - ct
            if abs(d) <= max_offset:
                votes.append((d, wi, first))

    if not votes:
        return {"offset": 0.0, "confidence": 0.0, "matches": matched, "votes": 0}

    # A cue's first word carries its true time; the spread guesses the
    # rest, so a first-word vote counts three times when picking the bin.
    bins = Counter()
    for d, _, first in votes:
        bins[int(math.floor(d / bin_seconds))] += 3 if first else 1
    best_bin, _ = max(bins.items(), key=lambda kv: (kv[1], -abs(kv[0])))
    near = [v for v in votes
            if best_bin - 1 <= int(math.floor(v[0] / bin_seconds)) <= best_bin + 1]
    # The even spread puts a cue's later words late, which biased every
    # offset ~0.25 s early on real audio (14-nov-2026); a cue's first word
    # has its true time, so it decides whenever enough of them matched.
    anchors = [d for d, _, first in near if first]
    pick = anchors if len(anchors) >= ANCHOR_MIN else [d for d, _, _ in near]
    offset = sum(pick) / len(pick)
    confidence = len(near) / float(len(votes))
    # Share of the HEARD words that agree. ``confidence`` divides by every
    # candidate vote, and a common word ("that", "the") votes once per
    # occurrence in the band, so a correct offset scored 0.3-0.45 on real
    # speech. Agreement per word is what says the offset is real.
    support = len({wi for _, wi, _ in near}) / float(matched) if matched else 0.0
    return {"offset": round(offset, 2), "confidence": round(confidence, 3),
            "support": round(support, 3), "anchors": len(anchors),
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


# --- sync over the whole film (14-nov-2026) ---------------------------------
#
# One 60 s probe at 25% finds a constant offset and nothing else. Two real
# failures it cannot see: DRIFT (an SRT timed for a 25 fps release on a
# 23.976 fps file runs 4% fast: 0 s out at the start, ~5 s by the end of a
# two-hour film) and a JUMP (an SRT from another cut is right until a removed
# scene, then off by that scene's length). So the same probe runs at five
# points and a model is fitted to the points that heard enough words:
# constant, linear (offset + drift) or piecewise (straight lines between the
# points, for a jump). ``apply_sync`` moves every cue by the model's offset at
# that cue's time.

SYNC_FRACTIONS = (0.1, 0.3, 0.5, 0.7, 0.9)
SYNC_MIN_SUPPORT = 0.4        # share of heard words agreeing with the offset
SYNC_JUMP_SECONDS = 1.0       # two neighbouring points this far apart = a jump to locate
SYNC_BISECT_STEPS = 3         # probes spent narrowing each jump
SYNC_BISECT_SECONDS = 30.0
SYNC_SECONDS = 45.0
SYNC_MIN_MATCHES = 8
SYNC_MIN_CONFIDENCE = 0.5
SYNC_CONSTANT_SPAN = 0.25     # a linear fit moving less than this over the film is constant
SYNC_LINEAR_RESIDUAL = 0.35   # worst point further than this from the line -> piecewise


def _point_ok(r):
    return (not r.get("error") and r.get("matches", 0) >= SYNC_MIN_MATCHES
            and (r.get("confidence", 0) >= SYNC_MIN_CONFIDENCE or r.get("support", 0) >= SYNC_MIN_SUPPORT))


def fit_sync(points, duration):
    """Model from probe points ``[{"t", "offset", "ok"}]`` (``t`` in SRT time).

    ``{"kind": "none"|"constant"|"linear"|"piecewise", "offset", "rate",
    "knots": [[t, offset]], "residual"}``. ``offset`` is the offset at t=0
    (constant/linear), ``rate`` seconds of offset per second of film."""
    good = sorted(((float(p["t"]), float(p["offset"])) for p in points if p.get("ok")), key=lambda x: x[0])
    if not good:
        return {"kind": "none", "offset": 0.0, "rate": 0.0, "knots": [], "residual": None}
    if len(good) == 1:
        return {"kind": "constant", "offset": round(good[0][1], 3), "rate": 0.0,
                "knots": [[round(good[0][0], 2), round(good[0][1], 3)]], "residual": 0.0}
    n = len(good)
    mt = sum(t for t, _ in good) / n
    mo = sum(o for _, o in good) / n
    var = sum((t - mt) ** 2 for t, _ in good)
    rate = sum((t - mt) * (o - mo) for t, o in good) / var if var > 0 else 0.0
    a = mo - rate * mt
    residual = max(abs(o - (a + rate * t)) for t, o in good)
    knots = [[round(t, 2), round(o, 3)] for t, o in good]
    span = abs(rate) * max(1.0, float(duration))
    if residual <= SYNC_LINEAR_RESIDUAL and span < SYNC_CONSTANT_SPAN:
        med = sorted(o for _, o in good)[n // 2] if n % 2 else sum(sorted(o for _, o in good)[n // 2 - 1:n // 2 + 1]) / 2
        return {"kind": "constant", "offset": round(med, 3), "rate": 0.0, "knots": knots,
                "residual": round(max(abs(o - med) for _, o in good), 3)}
    if residual <= SYNC_LINEAR_RESIDUAL:
        return {"kind": "linear", "offset": round(a, 3), "rate": round(rate, 7), "knots": knots,
                "residual": round(residual, 3)}
    return {"kind": "piecewise", "offset": round(good[0][1], 3), "rate": 0.0, "knots": knots,
            "residual": round(residual, 3)}


def sync_offset_at(sync, t):
    """The model's offset (seconds to ADD) at SRT time ``t``."""
    kind = (sync or {}).get("kind")
    if kind == "linear":
        return sync["offset"] + sync["rate"] * t
    if kind == "piecewise":
        knots = sync["knots"]
        if t <= knots[0][0]:
            return knots[0][1]
        for (t0, o0), (t1, o1) in zip(knots, knots[1:]):
            if t <= t1:
                # A jump is a step at one place, not a ramp: hold the earlier
                # offset until the midpoint between the two probes, then switch.
                return o0 if t < (t0 + t1) / 2 else o1
        return knots[-1][1]
    if kind == "constant":
        return sync["offset"]
    return 0.0


def apply_sync(cues, sync):
    """Cues moved by the model's offset at each cue's start, clamped at 0."""
    if not sync or sync.get("kind") in (None, "none"):
        return [dict(c) for c in cues]
    out = []
    for c in cues:
        off = sync_offset_at(sync, c["start"])
        start = max(0.0, c["start"] + off)
        end = max(start, c["end"] + off)
        if end > start:
            out.append({**c, "start": round(start, 3), "end": round(end, 3)})
    return out


def probe_sync(video_path, cues, duration, transcriber=None, fractions=SYNC_FRACTIONS,
               probe_seconds=SYNC_SECONDS, on_point=None):
    """Run ``probe_offset`` at every fraction and fit the model. Never raises.

    ``{"points": [{"at", "t", "offset", "confidence", "matches", "ok",
    "error"?}], "model": fit_sync(...)}``; ``at`` is the probe's centre in
    video time, ``t`` the same moment in SRT time (``at - offset``), which is
    what the model is a function of. ``on_point(i, point)`` reports progress."""
    duration = float(duration)

    def probe_at(frac, seconds):
        r = probe_offset(video_path, cues, duration, transcriber=transcriber,
                         probe_seconds=seconds, at=frac)
        w0, w1 = r.get("window") or (0.0, 0.0)
        at = (w0 + w1) / 2.0
        point = {"at": round(at, 2), "t": round(at - float(r.get("offset") or 0.0), 2),
                 "offset": r.get("offset", 0.0), "confidence": r.get("confidence", 0.0),
                 "support": r.get("support", 0.0),
                 "matches": r.get("matches", 0), "ok": _point_ok(r)}
        if r.get("error"):
            point["error"] = r["error"]
        points.append(point)
        if on_point:
            on_point(len(points) - 1, point)
        return point

    points = []
    for frac in fractions:
        probe_at(frac, probe_seconds)

    # A jump between two neighbouring good points is somewhere in the gap;
    # the piecewise model would switch at the midpoint and leave everything
    # between the midpoint and the real cut off by the jump (3 s on a live
    # test, 14-nov-2026). Halve the gap a few times to put the switch near
    # where the cut really is.
    if duration > 0 and fit_sync(points, duration)["kind"] == "piecewise":
        good = sorted((p for p in points if p["ok"]), key=lambda p: p["at"])
        for a, b in zip(good, good[1:]):
            if abs(b["offset"] - a["offset"]) < SYNC_JUMP_SECONDS:
                continue
            lo, hi = a, b
            for _ in range(SYNC_BISECT_STEPS):
                if hi["at"] - lo["at"] < 2 * SYNC_BISECT_SECONDS:
                    break
                mid = probe_at(((lo["at"] + hi["at"]) / 2.0 - SYNC_BISECT_SECONDS / 2.0) / duration,
                               SYNC_BISECT_SECONDS)
                mid["refine"] = True
                if not mid["ok"]:
                    break
                if abs(mid["offset"] - lo["offset"]) < abs(mid["offset"] - hi["offset"]):
                    lo = mid
                else:
                    hi = mid
    points.sort(key=lambda p: p["at"])
    return {"points": points, "model": fit_sync(points, duration)}


# --- transcript from the audio when there is no SRT ---------------------------

WHISPER_CUE_MAX_WORDS = 10
WHISPER_CUE_MAX_SECONDS = 5.0


def transcript_to_cues(transcript, max_words=WHISPER_CUE_MAX_WORDS, max_seconds=WHISPER_CUE_MAX_SECONDS):
    """Whisper segments -> subtitle-sized cues, split on word timings so a
    long segment becomes several lines a caption can carry."""
    cues = []
    for seg in (transcript or {}).get("segments", []) or []:
        words = [w for w in (seg.get("words") or []) if str(w.get("word", "")).strip()]
        if not words:
            text = " ".join(str(seg.get("text") or "").split())
            if text and float(seg.get("end", 0)) > float(seg.get("start", 0)):
                cues.append({"start": round(float(seg["start"]), 3), "end": round(float(seg["end"]), 3), "text": text})
            continue
        cur = []
        for w in words:
            if cur and (len(cur) >= max_words or float(w["end"]) - float(cur[0]["start"]) > max_seconds):
                cues.append(_cue_from_words(cur))
                cur = []
            cur.append(w)
        if cur:
            cues.append(_cue_from_words(cur))
    return [c for c in cues if c["end"] > c["start"] and c["text"]]


def _cue_from_words(words):
    text = " ".join(" ".join(str(w["word"]).strip() for w in words).split())
    return {"start": round(float(words[0]["start"]), 3), "end": round(float(words[-1]["end"]), 3), "text": text}


def write_srt(cues, path):
    def ts(t):
        ms = int(round(float(t) * 1000))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        sec, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
    rows = []
    for i, c in enumerate(cues, 1):
        rows += [str(i), f"{ts(c['start'])} --> {ts(c['end'])}", c["text"], ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(rows))
    return path


def transcribe_film(video_path, transcriber=None):
    """The whole film through whisper (the pipeline's default model), as
    cues. Slow on purpose-built hardware too: ~1/10 of the runtime on an
    8 GB card; the caller runs it in the background."""
    if transcriber is None:
        from transcribe_backends import transcribe_media
        transcriber = transcribe_media
    return transcript_to_cues(transcriber(video_path))


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
