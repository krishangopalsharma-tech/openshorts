"""Dedicated Comedy Section backend: metadata hard-gate, Whisper transcript
verification/alignment, internet-database + transcript prompt builder, and
Twitter/X comedy card job launcher.
"""
import json
import os
import uuid
from typing import Any, Dict, Optional

import film_prep
import jsonfile
import manual_clips
import transcript_import

COMEDY_PROFILE_FILE = ".comedy_profile.json"
COMEDY_AVATAR_FILE = ".comedy_avatar.png"

DEFAULT_PROFILE = {
    "channel_name": "Cheerful Videos",
    "handle": "@Cheerfulvids",
    "verified": True,
    "burn_subtitles": False,
}


def validate_comedy_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Validate and normalize movie / web-series metadata.

    Hard gate: raises ``ValueError`` if required fields are missing so the
    comedy prompt refuses to build when metadata is forgotten.
    """
    if not isinstance(metadata, dict):
        raise ValueError("Missing required comedy metadata: please enter the Movie Title or Series Title, Season, and Episode.")

    content_type = str(metadata.get("content_type") or "").strip().lower()
    if content_type not in ("series", "movie"):
        # Infer only if unambiguous, otherwise default to series when season/episode keys exist
        if "season" in metadata or "episode" in metadata:
            content_type = "series"
        else:
            content_type = "movie"

    title = str(metadata.get("title") or "").strip()
    if not title:
        label = "Series Title" if content_type == "series" else "Movie Title"
        raise ValueError(f"Missing required comedy metadata: {label} (title) is required before building the prompt.")

    season = str(metadata.get("season") or "").strip()
    episode = str(metadata.get("episode") or "").strip()
    episode_title = str(metadata.get("episode_title") or "").strip()
    year = str(metadata.get("year") or "").strip()
    notes = str(metadata.get("notes") or "").strip()

    if content_type == "series":
        if not season:
            raise ValueError("Missing required comedy metadata: Season number (season) is required for a web series.")
        if not episode:
            raise ValueError("Missing required comedy metadata: Episode number (episode) is required for a web series.")

    return {
        "content_type": content_type,
        "title": title,
        "season": season,
        "episode": episode,
        "episode_title": episode_title,
        "year": year,
        "notes": notes,
    }


def format_metadata_header(meta: Dict[str, str]) -> str:
    """Format validated metadata into a structured header block for the AI prompt."""
    validated = validate_comedy_metadata(meta)
    if validated["content_type"] == "series":
        ep_line = f"Season {validated['season']}, Episode {validated['episode']}"
        if validated["episode_title"]:
            ep_line += f' ("{validated["episode_title"]}")'
        header = [
            "CONTENT_TYPE: Web Series / TV Episode",
            f"SERIES_TITLE: {validated['title']}",
            f"EPISODE_INFO: {ep_line}",
        ]
    else:
        title_line = validated["title"]
        if validated["year"]:
            title_line += f" ({validated['year']})"
        header = [
            "CONTENT_TYPE: Movie / Film",
            f"MOVIE_TITLE: {title_line}",
        ]
    if validated["notes"]:
        header.append(f"ADDITIONAL_CONTEXT: {validated['notes']}")
    return "\n".join(header)


def build_comedy_prompt(
    transcript: Dict[str, Any],
    duration: float,
    metadata: Dict[str, Any],
    counts: Optional[tuple] = None,
) -> str:
    """Build the internet-database + Whisper-aligned transcript comedy prompt.

    Raises ``ValueError`` if ``metadata`` is incomplete.
    """
    validated = validate_comedy_metadata(metadata)
    meta_block = format_metadata_header(validated)
    min_clips, max_clips = counts or manual_clips.chat_clip_counts(transcript, duration)

    if validated["content_type"] == "series":
        target_desc = (
            f'"{validated["title"]}" — Season {validated["season"]}, '
            f'Episode {validated["episode"]}'
            + (f' ("{validated["episode_title"]}")' if validated["episode_title"] else "")
        )
    else:
        target_desc = (
            f'"{validated["title"]}"'
            + (f' ({validated["year"]})' if validated["year"] else "")
        )

    alignment = (transcript or {}).get("alignment") or {}
    align_note = (
        f"Whisper Audio Verification: Verified (offset {alignment.get('offset', 0.0):+.2f}s, "
        f"confidence {int(round(float(alignment.get('confidence', 1.0)) * 100))}%)"
        if alignment.get("verified")
        else "Whisper Audio Verification: Direct Whisper audio timestamps"
    )

    lines = [
        f"You are an elite comedy video editor and pop-culture researcher extracting viral Twitter/X-style comedy clips from {target_desc}.",
        "",
        "=== SOURCE METADATA ===",
        meta_block,
        f"DURATION_SECONDS: {float(duration):.1f}",
        f"LANGUAGE: {(transcript or {}).get('language', 'en')}",
        f"TIMESTAMP_SYNC: {align_note}",
        "",
        "=== STEP 1: INTERNET DATABASE & POP-CULTURE RESEARCH ===",
        f"Before picking timestamps, research and recall internet databases (IMDb, Wikipedia, fan wikis, episode guides, quote databases, Reddit fan-favorite threads) for {target_desc}:",
        "- Identify the funniest standalone scenes, cold opens, running gags, awkward silences, physical/situational comedy beats, and iconic quotes from this specific title/episode.",
        "- Understand who is speaking in each scene and why the setup + punchline works for a viewer scrolling Twitter/X, Reels, or Shorts.",
        "",
        "=== STEP 2: MATCH AGAINST ALIGNED TRANSCRIPT TIMESTAMPS ===",
        f"Select between {min_clips} and {max_clips} of the funniest standalone comedy moments (each 15 to 60 seconds long):",
        "- Start each clip right on the comedic setup or escalating tension (never mid-sentence).",
        "- End each clip 0.5–1.5 seconds after the punchline, comeback, or comedic reaction lands.",
        "- Every clip must work on its own for someone who has never seen the full episode/movie.",
        "- Do NOT return overlapping clips.",
        "",
        "=== STEP 3: WRITE THE TWITTER/X 'POV' HEADER CAPTION ===",
        "These clips render inside a Twitter/X post frame (profile header at top-left, caption text directly underneath, and a square-ish video frame below).",
        "For every clip, write a punchy `pov_text` (3 to 10 words, with 1–2 fitting emojis like 😂, 💀, 😭):",
        '- Examples: "Zero survival instinct 😂", "POV: Your boss discovers the internet 💀", "He really doubled down on the lie 😭"',
        "- Make `pov_text` amplify the joke without spoiling the final punchline word-for-word.",
        "",
        "=== OUTPUT FORMAT (STRICT JSON ONLY) ===",
        "Reply ONLY with a JSON array of objects in this exact shape:",
        "```json",
        "[",
        "  {",
        '    "start": 124.5,',
        '    "end": 162.0,',
        '    "title": "Short internal/YouTube title",',
        '    "pov_text": "Zero survival instinct 😂",',
        '    "why_funny": "1-sentence note on the comedic beat from this episode/movie",',
        '    "predicted_score": 95',
        "  }",
        "]",
        "```",
        "",
        "=== WHISPER-ALIGNED TRANSCRIPT ([start-end in seconds] spoken text) ===",
    ]

    for seg in (transcript or {}).get("segments", []):
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"[{float(seg['start']):.1f}-{float(seg['end']):.1f}] {text}")

    return "\n".join(lines) + "\n"


def _extract_cues_from_raw(raw_text: str):
    """Parse raw SRT/VTT/JSON/timestamp text (or a subtitle file path on disk)
    into ``[{"start", "end", "text"}]`` cues."""
    if raw_text and "\n" not in raw_text and os.path.isfile(raw_text):
        try:
            cues = film_prep.parse_subtitles(raw_text)
            if cues:
                return cues, None
        except Exception:
            with open(raw_text, "r", encoding="utf-8", errors="replace") as f:
                raw_text = f.read()
    parsed = transcript_import.load_transcript(raw_text)
    segs = parsed.get("segments", [])
    return [
        {"start": float(s["start"]), "end": float(s["end"]), "text": str(s.get("text") or "").strip()}
        for s in segs
        if str(s.get("text") or "").strip()
    ], parsed


def verify_and_align_transcript(
    video_path: str,
    raw_transcript_text: str,
    duration: float,
    manual_offset: Optional[float] = None,
    prober=None,
) -> Dict[str, Any]:
    """Parse an uploaded/pasted transcript (.srt, .vtt, .txt, .json), verify it
    against the video's audio track with Whisper, and shift all timestamps by
    the detected offset so cuts and captions stay in sync.
    """
    cues, parsed_full = _extract_cues_from_raw(raw_transcript_text)
    if not cues:
        raise ValueError("Could not parse any timestamped lines or subtitle cues from the provided transcript.")

    prober_fn = prober or film_prep.probe_offset
    if manual_offset is not None:
        offset = round(float(manual_offset), 2)
        probe_info = {
            "verified": True,
            "offset": offset,
            "confidence": 1.0,
            "matches": len(cues),
            "votes": len(cues),
            "manual": True,
        }
    else:
        probe = prober_fn(video_path, cues, duration)
        raw_offset = float(probe.get("offset") or 0.0)
        confidence = float(probe.get("confidence") or 0.0)
        # Apply automatic shift when confidence >= 0.25 or in unit tests; always report probe
        offset = round(raw_offset, 2) if confidence >= 0.25 else 0.0
        probe_info = {
            "verified": not bool(probe.get("error")),
            "offset": offset,
            "detected_offset": round(raw_offset, 2),
            "confidence": round(confidence, 3),
            "matches": int(probe.get("matches") or 0),
            "votes": int(probe.get("votes") or 0),
            "error": probe.get("error"),
            "manual": False,
        }

    aligned = film_prep.cues_to_transcript(cues, offset=offset)
    if parsed_full and parsed_full.get("language"):
        aligned["language"] = parsed_full["language"]
    aligned["alignment"] = probe_info
    return aligned


def write_comedy_prompt_files(
    output_dir: str,
    transcript: Dict[str, Any],
    duration: float,
    metadata: Dict[str, Any],
) -> str:
    """Validate metadata, write transcript.json + comedy_metadata.json +
    paste_into_chat.txt into ``output_dir``, and return the prompt path."""
    validated = validate_comedy_metadata(metadata)
    os.makedirs(output_dir, exist_ok=True)
    prompt_text = build_comedy_prompt(transcript, duration, validated)

    jsonfile.dump_atomic(os.path.join(output_dir, "transcript.json"), transcript)
    jsonfile.dump_atomic(os.path.join(output_dir, "comedy_metadata.json"), validated, indent=2)

    prompt_path = os.path.join(output_dir, "paste_into_chat.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt_text)
    return prompt_path


def load_profile(output_root: str) -> Dict[str, Any]:
    path = os.path.join(output_root, COMEDY_PROFILE_FILE)
    prof = dict(DEFAULT_PROFILE)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                prof.update({k: saved[k] for k in DEFAULT_PROFILE if k in saved})
        except Exception:
            pass
    avatar_path = os.path.join(output_root, COMEDY_AVATAR_FILE)
    prof["has_avatar"] = os.path.exists(avatar_path)
    prof["avatar_path"] = avatar_path if prof["has_avatar"] else None
    return prof


def save_profile(output_root: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    os.makedirs(output_root, exist_ok=True)
    prof = load_profile(output_root)
    for k in ("channel_name", "handle", "verified", "burn_subtitles"):
        if k in patch and patch[k] is not None:
            prof[k] = bool(patch[k]) if k in ("verified", "burn_subtitles") else str(patch[k]).strip()
    path = os.path.join(output_root, COMEDY_PROFILE_FILE)
    jsonfile.dump_atomic(
        path,
        {k: prof[k] for k in ("channel_name", "handle", "verified", "burn_subtitles")},
        indent=2,
    )
    return load_profile(output_root)
