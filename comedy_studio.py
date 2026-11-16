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

# What kinds of shorts the chat model is asked to cut. Order is display order
# in the dashboard. Each entry: label, emoji (for the checkbox), what to look
# for in the source, how the Twitter/X caption should read, and fallback tags
# used when the model returns none. A film that is not a comedy (a true-story
# drama, a thriller) still has quotes, lessons and tense scenes worth cutting.
CATEGORIES: Dict[str, Dict[str, str]] = {
    "comedy": {
        "label": "Comedy", "emoji": "😂",
        "look_for": "jokes, comebacks, awkward silences, running gags, physical or situational comedy; start on the setup, end 0.5-1.5 s after the laugh lands",
        "caption": "amplify the joke without spoiling the punchline (😂 💀 😭)",
        "tags": "comedy, funny moments, funny scene",
    },
    "inspirational": {
        "label": "Inspirational", "emoji": "✨",
        "look_for": "a character overcoming doubt, a breakthrough, a never-give-up moment, an underdog proving people wrong",
        "caption": "make the viewer feel they can do it too (✨ 🔥 🙌)",
        "tags": "inspirational, inspiration, never give up",
    },
    "motivational": {
        "label": "Motivational", "emoji": "🔥",
        "look_for": "speeches, pep talks, hard-work and discipline moments, a mentor pushing someone, a line about chasing a dream",
        "caption": "short, punchy call to action (🔥 💪 🚀)",
        "tags": "motivation, motivational speech, mindset",
    },
    "quote": {
        "label": "Quotes", "emoji": "💬",
        "look_for": "one quotable line that stands alone (wisdom, a philosophy of life, a memorable one-liner) with just enough lead-in for it to land",
        "caption": "quote the line itself or frame it as a lesson (💬 🧠)",
        "tags": "movie quotes, best quotes, quotes",
    },
    "life_lesson": {
        "label": "Life Lessons", "emoji": "🧠",
        "look_for": "advice, regret, a hard truth about age, money, love or time, an older character teaching a younger one",
        "caption": "state the lesson in plain words (🧠 💯)",
        "tags": "life lessons, life advice, wisdom",
    },
    "emotional": {
        "label": "Emotional", "emoji": "😢",
        "look_for": "goodbyes, reunions, grief, sacrifice, a confession, the scene that makes people cry",
        "caption": "name the feeling, not the plot (😢 💔 🥹)",
        "tags": "emotional scene, sad, tearjerker",
    },
    "wholesome": {
        "label": "Wholesome", "emoji": "🥹",
        "look_for": "kindness from strangers, friendship, family warmth, small human moments that make people smile",
        "caption": "warm and simple (🥹 ❤️)",
        "tags": "wholesome, heartwarming, feel good",
    },
    "romance": {
        "label": "Romance", "emoji": "❤️",
        "look_for": "chemistry, first meetings, confessions, flirting, a couple's defining moment",
        "caption": "relatable couple energy (❤️ 😍)",
        "tags": "romance, love story, couple goals",
    },
    "mystery": {
        "label": "Mystery", "emoji": "🕵️",
        "look_for": "a clue, a strange discovery, an unanswered question; end on the question, not the answer",
        "caption": "pose the question the viewer now needs answered (🕵️ 👀 ❓)",
        "tags": "mystery, unsolved, detective",
    },
    "suspense": {
        "label": "Suspense / Thriller", "emoji": "😱",
        "look_for": "rising tension, a close call, a race against time, a standoff; end at the peak",
        "caption": "make the viewer hold their breath (😱 😬)",
        "tags": "thriller, suspense, intense scene",
    },
    "plot_twist": {
        "label": "Plot Twist / Reveal", "emoji": "🤯",
        "look_for": "a reveal or turn that changes how the scene reads; include enough setup that it lands",
        "caption": "tease the turn without saying it (🤯 😳)",
        "tags": "plot twist, reveal, mind blown",
    },
    "action": {
        "label": "Action / Thrill", "emoji": "💥",
        "look_for": "a chase, a race, a fight, a stunt, a speed or danger moment with a clear start and finish",
        "caption": "pure adrenaline (💥 🏎️ ⚡)",
        "tags": "action scene, adrenaline, epic",
    },
    "confrontation": {
        "label": "Confrontation / Savage", "emoji": "😤",
        "look_for": "arguments, standing up to someone, a savage reply, a power shift between two characters",
        "caption": "pick a side (😤 🔥)",
        "tags": "savage, confrontation, epic reply",
    },
    "iconic": {
        "label": "Iconic Scene", "emoji": "🎬",
        "look_for": "the scenes fans quote and rewatch, the ones that define the film or episode",
        "caption": "frame why it is legendary (🎬 🐐)",
        "tags": "iconic scene, best scene, classic",
    },
    "true_story": {
        "label": "True Story / Real Life", "emoji": "📜",
        "look_for": "moments that mirror what really happened to the real person, records, real stakes, 'this actually happened' beats",
        "caption": "lean on 'this really happened' (📜 🤯)",
        "tags": "true story, based on a true story, real life",
    },
    "horror": {
        "label": "Horror / Creepy", "emoji": "👻",
        "look_for": "jump scares, eerie silences, something wrong in the frame; end on the scare",
        "caption": "dare the viewer to keep watching (👻 😨)",
        "tags": "horror, creepy, scary scene",
    },
}

DEFAULT_CATEGORIES = ["comedy"]
# Twitter/X-style shorts stay under a minute (YouTube Shorts / Reels limit).
SHORT_MIN_SECONDS = 15
SHORT_MAX_SECONDS = 59
MAX_SHORTS = 20


def normalize_categories(raw: Any) -> list:
    """Return the selected category ids in registry order.

    Accepts a list or a comma-separated string. Empty means the original
    comedy-only behaviour; an unknown id raises ``ValueError`` so a typo is
    not silently turned into a different prompt.
    """
    if raw is None or raw == "" or raw == []:
        return list(DEFAULT_CATEGORIES)
    if isinstance(raw, str):
        items = [p for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ValueError("categories must be a list of category ids")
    wanted = {str(p).strip().lower() for p in items if str(p).strip()}
    unknown = sorted(wanted - set(CATEGORIES))
    if unknown:
        raise ValueError(f"Unknown short category: {', '.join(unknown)}")
    if not wanted:
        return list(DEFAULT_CATEGORIES)
    return [k for k in CATEGORIES if k in wanted]


def category_tags(categories) -> list:
    """Fallback YouTube tags for the selected categories (deduped, ordered)."""
    out, seen = [], set()
    for cid in normalize_categories(categories):
        for t in CATEGORIES[cid]["tags"].split(","):
            t = t.strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out


def shorts_count_band(base: tuple, duration: float, n_categories: int) -> tuple:
    """(min, max) shorts to ask for. More categories → more shorts, never
    more than MAX_SHORTS. A feature film has room for ~one short per 5 min."""
    base_min, base_max = base
    n = max(1, int(n_categories))
    by_length = int(float(duration or 0) // 300)
    hi = min(MAX_SHORTS, max(int(base_max), 4 * n, by_length))
    lo = min(hi, max(int(base_min), 2 * n))
    return lo, hi


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

    categories = normalize_categories(metadata.get("categories"))

    return {
        "categories": categories,
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
    categories = validated["categories"]
    if counts:
        min_clips, max_clips = counts
    else:
        min_clips, max_clips = shorts_count_band(
            manual_clips.chat_clip_counts(transcript, duration), duration, len(categories))
    only_comedy = categories == ["comedy"]
    kinds = ", ".join(CATEGORIES[c]["label"] for c in categories)

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

    lo_s, hi_s = SHORT_MIN_SECONDS, SHORT_MAX_SECONDS
    role = ("an elite comedy video editor and pop-culture researcher"
            if only_comedy else
            "an elite short-form video editor and film/TV researcher")
    first = categories[0]
    example_pov = "Zero survival instinct 😂" if first == "comedy" else f"This scene hits different {CATEGORIES[first]['emoji']}"

    lines = [
        f"You are {role} extracting viral Twitter/X-style shorts from {target_desc}.",
        f"SHORT TYPES WANTED: {kinds}",
        "",
        "=== SOURCE METADATA ===",
        meta_block,
        f"DURATION_SECONDS: {float(duration):.1f}",
        f"LANGUAGE: {(transcript or {}).get('language', 'en')}",
        f"TIMESTAMP_SYNC: {align_note}",
        "",
        "=== STEP 1: INTERNET DATABASE & POP-CULTURE RESEARCH ===",
        f"Before picking timestamps, research and recall internet databases (IMDb, Wikipedia, fan wikis, episode guides, quote databases, Reddit fan-favorite threads) for {target_desc}:",
        "- Recall the genre, the real story behind it (if based on true events), the main characters and the scenes and quotes fans remember most.",
        "- For each short type below, list the strongest candidate scenes from this specific title/episode, then find them in the transcript.",
        "- Do not force a type the source does not have: if a type has no strong moment, return fewer of it rather than a weak clip.",
        "",
        "=== SHORT TYPES (pick moments for EVERY type checked) ===",
    ]
    for cid in categories:
        c = CATEGORIES[cid]
        lines.append(f"- `{cid}` — {c['label']} {c['emoji']}: look for {c['look_for']}. Caption tone: {c['caption']}.")

    spread = (
        f"- Spread the picks across the {len(categories)} types (aim for at least 2 per type when the source has them); the best moments win any tie."
        if len(categories) > 1 else
        "- Every pick must be this type."
    )
    lines += [
        "",
        "=== STEP 2: MATCH AGAINST ALIGNED TRANSCRIPT TIMESTAMPS ===",
        f"Select between {min_clips} and {max_clips} standalone moments in total (NEVER more than {MAX_SHORTS}).",
        f"- HARD LENGTH RULE: every clip is {lo_s} to {hi_s} seconds long (end - start < 60). A clip of 60 s or more is rejected.",
        spread,
        "- Start each clip on the setup / the first line that makes the moment understandable (never mid-sentence).",
        "- End each clip 0.5–1.5 seconds after the payoff lands (the laugh, the line, the reveal, the reaction).",
        "- Every clip must work on its own for someone who has never seen the full episode/movie.",
        "- Do NOT return overlapping clips. Sort by start time.",
        "",
        "=== STEP 3: WRITE THE TWITTER/X 'POV' HEADER CAPTION, DESCRIPTION & YOUTUBE TAGS ===",
        "These clips render inside a Twitter/X post frame (profile header at top-left, caption text directly underneath, and a square-ish video frame below).",
        "For every clip, write:",
        "- `category`: one of the type ids above.",
        "- `title`: Short catchy YouTube Short title (max 70 characters).",
        "- `pov_text`: Punchy Twitter/X caption (3 to 10 words, with 1–2 fitting emojis) in the tone of its type. Do not give away the payoff word-for-word.",
        "- `description`: 1 sentence teasing the payoff + 3–5 relevant hashtags (e.g. #shorts plus the type).",
        "- `tags`: 8 to 15 comma-separated YouTube tags (no `#`), including the series/movie title, character names, actor names, and keywords for the clip's type.",
        "",
        "=== OUTPUT FORMAT (STRICT JSON ONLY) ===",
        "Reply ONLY with a JSON array of objects in this exact shape:",
        "```json",
        "[",
        "  {",
        '    "start": 124.5,',
        '    "end": 162.0,',
        f'    "category": "{first}",',
        '    "title": "Short internal/YouTube title",',
        f'    "pov_text": "{example_pov}",',
        f'    "description": "1 sentence teasing the payoff #shorts #{first.replace("_", "")}",',
        f'    "tags": "series or movie name, character name, {CATEGORIES[first]["tags"]}, shorts, viral",',
        '    "why_it_works": "1-sentence note on why this moment stands alone",',
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
