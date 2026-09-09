"""A record of the sources clips have already been generated from.

A 72-minute upload costs ~25 minutes of transcription plus a render per clip,
so re-submitting the same video by accident is the most expensive mistake the
UI allows. This keeps a small append-only index of finished jobs and matches a
new submission against it, so the dashboard can say "clips already generated
from this video" before spending the GPU again.

It is deliberately a JSON file rather than a table: the cloud build already has
`UserVideo` in Postgres, but that is per signed-in user, holds finished CLIPS
rather than sources, and does not exist at all in a self-host install — which
is where the whole pipeline runs on one person's machine and the mistake
actually happens.

Nothing here blocks a job. `find()` reports; the caller decides.
"""

import json
import os
import re
import time
import unicodedata

# Keep the newest N. This file is read on every submit and is only ever an
# advisory, so it is not worth growing without bound.
MAX_ENTRIES = 500

_YOUTUBE_ID = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/))"
    r"([A-Za-z0-9_-]{11})"
)

# A duration read back from a container is not bit-exact, so compare with a
# tolerance rather than for equality.
DURATION_TOLERANCE_S = 1.0


def youtube_id(url):
    """The 11-character video id in a YouTube URL, or None."""
    if not url:
        return None
    m = _YOUTUBE_ID.search(str(url))
    return m.group(1) if m else None


def normalize_title(title):
    """A comparable form of a video title.

    Case, punctuation and spacing only. It deliberately does NOT strip things
    like "(HD)", "1080p" or "full episode": those look like noise but they are
    often the only difference between two genuinely different uploads, and a
    false "you already did this" is worse than a missed one — it trains the
    user to click through the warning.
    """
    if not title:
        return ""
    text = os.path.splitext(str(title))[0]
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def fingerprint(title=None, url=None, size_bytes=None, duration_seconds=None):
    """The identity of one source, as much of it as the caller knows."""
    return {
        "title": (str(title).strip() if title else "") or "",
        "title_key": normalize_title(title),
        "youtube_id": youtube_id(url),
        "url": (str(url).strip() if url else "") or "",
        "size_bytes": int(size_bytes) if size_bytes else None,
        "duration_seconds": (round(float(duration_seconds), 2)
                             if duration_seconds else None),
    }


def _match_reason(fp, entry):
    """Why these two are the same video, or None.

    Three independent signals, strongest first. A YouTube id is exact. A title
    is what the user recognises and what they asked to match on. Size plus
    duration catches the same file submitted under a different name, which a
    title alone cannot.
    """
    if fp.get("youtube_id") and fp["youtube_id"] == entry.get("youtube_id"):
        return "youtube_id"
    if fp.get("title_key") and fp["title_key"] == entry.get("title_key"):
        return "title"
    size, e_size = fp.get("size_bytes"), entry.get("size_bytes")
    dur, e_dur = fp.get("duration_seconds"), entry.get("duration_seconds")
    if size and e_size and size == e_size:
        # Size alone is a weak signal on small files, so require the duration
        # too whenever both sides know it.
        if dur and e_dur:
            return "size_and_duration" if abs(dur - e_dur) <= DURATION_TOLERANCE_S else None
        return "size"
    return None


def load(index_path):
    """Every recorded source, newest first. A missing or corrupt index is an
    empty list: this feature must never be the reason a submit fails."""
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    entries = data.get("sources") if isinstance(data, dict) else data
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def find(index_path, fp):
    """Recorded sources that look like ``fp``, newest first.

    Each match carries ``match_reason`` so the UI can say how confident it is.
    """
    out = []
    for entry in load(index_path):
        reason = _match_reason(fp, entry)
        if reason:
            out.append({**entry, "match_reason": reason})
    return out


def record_many(index_path, items):
    """Seed the index from jobs already finished on disk, in ONE write.

    Used at startup: without it the history starts empty, and the first thing
    it would fail to warn about is the video the user already cut yesterday —
    which is the whole case this feature exists for. ``items`` is an iterable
    of (fp, job_id, clip_count, created_at); anything already in the index, or
    already claimed by an earlier item, is skipped, so this is idempotent and
    never overwrites a newer real run with an older backfilled one.

    Returns how many entries were added.
    """
    entries = load(index_path)
    added = 0
    for fp, job_id, clip_count, created_at in items:
        if not (fp.get("title_key") or fp.get("youtube_id") or fp.get("size_bytes")):
            continue
        if any(e.get("job_id") == job_id or _match_reason(fp, e) for e in entries):
            continue
        entries.append({**fp, "job_id": job_id,
                        "clip_count": int(clip_count or 0),
                        "created_at": float(created_at or time.time())})
        added += 1
    if not added:
        return 0
    entries.sort(key=lambda e: e.get("created_at") or 0, reverse=True)
    _write(index_path, entries[:MAX_ENTRIES])
    return added


def _write(index_path, entries):
    os.makedirs(os.path.dirname(index_path) or ".", exist_ok=True)
    tmp = f"{index_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"sources": entries}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, index_path)   # atomic: a crash cannot leave half a file


def record(index_path, fp, job_id, clip_count):
    """Remember that ``job_id`` produced ``clip_count`` clips from ``fp``.

    Re-running the same source replaces its entry rather than adding a second
    one, so the warning always names the most recent run. Returns True when the
    index was written.
    """
    if not (fp.get("title_key") or fp.get("youtube_id") or fp.get("size_bytes")):
        return False   # nothing identifying to match on later
    entry = {
        **fp,
        "job_id": job_id,
        "clip_count": int(clip_count or 0),
        "created_at": time.time(),
    }
    kept = [e for e in load(index_path)
            if e.get("job_id") != job_id and not _match_reason(fp, e)]
    entries = [entry] + kept
    entries.sort(key=lambda e: e.get("created_at") or 0, reverse=True)
    entries = entries[:MAX_ENTRIES]
    try:
        _write(index_path, entries)
        return True
    except OSError as e:
        print(f"⚠️ Could not write the source history: {e}")
        return False
