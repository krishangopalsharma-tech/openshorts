"""Turn a transcript someone already has into the shape main.py consumes.

YouTube hands out a transcript for most videos, and transcribing the same
hour of audio again costs 25 minutes of GPU for a file we were given. The
pipeline already knows how to reuse one — ``main.py --transcript`` skips
transcription entirely — but the only way to produce that file was to let
OpenShorts transcribe it first, which is the thing being avoided.

Two inputs are accepted, because they are the two a person actually has:

* **Whisper JSON** — what this pipeline itself writes (``transcript.json``,
  the Thumbnail Studio handover, a checkpoint from an interrupted run).
  Passed through with its word timings intact, so captions burned from it
  are exactly as precise as a fresh transcription.
* **Plain timestamped text** — what YouTube's transcript panel puts on the
  clipboard: ``0:14`` and the line, over and over. Cue-level timings only,
  so words are spread across each cue the way ``film_prep`` spreads subtitle
  cues. Good enough to pick moments from; looser than whisper for captions.

Nothing here calls a model, touches the disk, or raises for bad input: a
transcript that cannot be read is a ``TranscriptImportError`` carrying a
sentence the dashboard can show, because the person can fix it and retry.
"""

import json
import re

import film_prep

# A cue with no words contributes nothing and breaks the even-spread maths.
MIN_CUE_SECONDS = 0.2
# Trailing cue with no successor to bound it. YouTube lines run ~2-6 s.
DEFAULT_TAIL_SECONDS = 4.0


class TranscriptImportError(ValueError):
    """Unusable input. The message is shown to the user verbatim."""


# ``0:14``, ``00:14``, ``1:02:33``, ``[00:14]``, ``(1:02:33)``, ``00:00:14.500``.
# Anchored at the start of a line: a timestamp in the middle of a sentence is
# speech ("we did it at 3:30"), not a cue boundary, and treating it as one
# splits a line in half.
_TS = re.compile(
    r'^[\s\[\(\-•]*'
    r'(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?'
    r'[\s\]\)\-:]*'
)


def _seconds(h, m, s, frac):
    total = int(m) * 60 + int(s)
    if h:
        total += int(h) * 3600
    if frac:
        total += float(f"0.{frac}")
    return float(total)


def looks_like_whisper_json(text):
    """True when the text parses as JSON carrying a ``segments`` list."""
    stripped = (text or "").lstrip()
    if not stripped.startswith("{") and not stripped.startswith("["):
        return False
    try:
        data = json.loads(stripped)
    except ValueError:
        return False
    return isinstance(data, dict) and isinstance(data.get("segments"), list)


def parse_whisper_json(text):
    """Normalize a whisper-shaped transcript, keeping word timings.

    Accepts what this pipeline writes and what faster-whisper's own dumps
    look like. Segments missing ``words`` are kept — ``main.py`` only needs
    words for captions, and the moment picker reads the text.
    """
    try:
        data = json.loads(text)
    except ValueError as e:
        raise TranscriptImportError(f"That JSON could not be read ({e}).")
    if not isinstance(data, dict):
        raise TranscriptImportError("The JSON must be an object with a 'segments' list.")

    raw = data.get("segments")
    if not isinstance(raw, list) or not raw:
        raise TranscriptImportError("The JSON has no 'segments' — is it a transcript?")

    segments = []
    for seg in raw:
        if not isinstance(seg, dict):
            continue
        text_value = str(seg.get("text") or "").strip()
        if not text_value:
            continue
        try:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            end = start + MIN_CUE_SECONDS
        words = []
        for w in (seg.get("words") or []):
            if not isinstance(w, dict):
                continue
            token = w.get("word", w.get("text"))
            if token is None:
                continue
            try:
                w_start = float(w.get("start", start))
                w_end = float(w.get("end", w_start))
            except (TypeError, ValueError):
                continue
            # whisper's leading-space convention is what
            # subtitles.merge_continuation_words keys on.
            token = str(token)
            if not token.startswith(" "):
                token = " " + token.lstrip()
            words.append({"word": token, "start": round(w_start, 3),
                          "end": round(max(w_end, w_start), 3)})
        segments.append({"start": start, "end": end,
                         "text": text_value, "words": words})

    if not segments:
        raise TranscriptImportError("Every segment in that JSON was empty.")

    language = str(data.get("language") or "en")
    return {"language": language, "segments": segments,
            "text": " ".join(s["text"] for s in segments),
            "source": "imported_json"}


def parse_timestamped_text(text, language="en"):
    """``0:14`` + a line of speech, repeated — YouTube's transcript panel.

    Both layouts that panel produces are handled: the timestamp and its text
    on ONE line (``0:14 so I said``), and the timestamp on its own line with
    the speech under it, which is what a plain copy out of the panel gives.
    A cue runs until the next timestamp, so nothing has to be guessed except
    the last one.
    """
    cues = []
    pending_start = None
    pending_text = []

    def flush(next_start=None):
        if pending_start is None:
            return
        body = " ".join(part.strip() for part in pending_text if part.strip())
        body = re.sub(r"\s+", " ", body).strip()
        if not body:
            return
        end = next_start if next_start is not None else pending_start + DEFAULT_TAIL_SECONDS
        if end <= pending_start:
            end = pending_start + MIN_CUE_SECONDS
        cues.append({"start": pending_start, "end": end, "text": body})

    for line in (text or "").splitlines():
        if not line.strip():
            continue
        match = _TS.match(line)
        if match:
            start = _seconds(*match.groups())
            flush(start)
            pending_start = start
            pending_text = [line[match.end():]]
        elif pending_start is not None:
            pending_text.append(line)
        # A line before the first timestamp is a header ("Transcript",
        # "English (auto-generated)") — dropped rather than dated to 0.
    flush()

    if not cues:
        raise TranscriptImportError(
            "No timestamps found. Lines should start with a time, like "
            "'0:14 what was said'.")

    # Cues carry no word timings, so film_prep spreads each cue's words
    # across its span weighted by length — the same treatment a film's SRT
    # gets, and the reason captions from this are looser than whisper's.
    transcript = film_prep.cues_to_transcript(cues, language=language)
    transcript["text"] = " ".join(c["text"] for c in cues)
    transcript["source"] = "imported_text"
    return transcript


_SRT_TIMING = re.compile(
    r"\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}"
)


def looks_like_vtt(text):
    clean = (text or "").lstrip("\ufeff \t\r\n")
    return clean.startswith("WEBVTT")


def looks_like_srt(text):
    return bool(_SRT_TIMING.search(text or ""))


def parse_vtt_transcript(text, language="en"):
    """WebVTT subtitle file (.vtt). Cues are converted to transcript segments."""
    try:
        cues = film_prep.parse_vtt(text)
    except film_prep.SubtitleError as e:
        raise TranscriptImportError(f"That WebVTT file could not be read: {e}")
    except Exception as e:
        raise TranscriptImportError(f"That WebVTT file could not be read ({e}).")
    if not cues:
        raise TranscriptImportError("No subtitle cues found in that WebVTT file.")
    transcript = film_prep.cues_to_transcript(cues, language=language)
    transcript["text"] = " ".join(c["text"] for c in cues)
    transcript["source"] = "imported_vtt"
    return transcript


def parse_srt_transcript(text, language="en"):
    """SubRip subtitle file (.srt). Cues are converted to transcript segments."""
    try:
        cues = film_prep.parse_srt(text)
    except film_prep.SubtitleError as e:
        raise TranscriptImportError(f"That SRT file could not be read: {e}")
    except Exception as e:
        raise TranscriptImportError(f"That SRT file could not be read ({e}).")
    if not cues:
        raise TranscriptImportError("No subtitle cues found in that SRT file.")
    transcript = film_prep.cues_to_transcript(cues, language=language)
    transcript["text"] = " ".join(c["text"] for c in cues)
    transcript["source"] = "imported_srt"
    return transcript


def load_transcript(text, filename=""):
    """Whichever format this is, as a whisper-shaped transcript.

    Accepts Whisper JSON, WebVTT (.vtt), SubRip (.srt), or plain timestamped text.
    The filename only breaks ties: content decides, so a ``.txt`` holding
    JSON or SRT still imports properly.
    """
    if not (text or "").strip():
        raise TranscriptImportError("That file is empty.")
    ext = (filename or "").lower().split("?")[0]
    if looks_like_whisper_json(text) or ext.endswith(".json"):
        return parse_whisper_json(text)
    if looks_like_vtt(text) or ext.endswith(".vtt"):
        return parse_vtt_transcript(text)
    if ext.endswith(".srt") or looks_like_srt(text):
        return parse_srt_transcript(text)
    return parse_timestamped_text(text)


def describe(transcript):
    """One line for the dashboard: how much was imported, and how precise."""
    segments = transcript.get("segments") or []
    words = sum(len(s.get("words") or []) for s in segments)
    end = max((s["end"] for s in segments), default=0.0)
    kind = ("word timings" if transcript.get("source") == "imported_json" and words
            else "line timings")
    return (f"{len(segments)} lines, {end / 60:.0f} min, {kind}")
