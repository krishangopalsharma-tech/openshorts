"""Two channel profiles — India (Hindi/Hinglish) and USA (English) — picked by the
video's language. EDIT THE TEXT BELOW to match your channels.

How a profile gets picked (first match wins):
  1. main.py --audience in|us          (per run; recommended — also fixes transcription)
  2. AUDIENCE=in|us in .env / env var
  3. the transcript language            (hinglish, hi, ur, ... -> "in"; en -> "us")

Where it is used:
  - --transcribe-only writes paste_into_chat.txt = this profile's prompt + transcript
  - the normal Gemini pipeline gets the profile's context block on top of its prompts
  - CLI runs: the profile's TRANSCRIBE_LANGUAGE, TRANSCRIBE_PROMPT and clip length (env wins)
"""
import os

PROFILES = {
    "in": {
        "label": "India - Hindi / Hinglish",
        "languages": {"hinglish", "hi", "ur", "pa", "mr", "gu", "bn", "ne", "ta", "te", "kn", "ml", "as", "sa"},
        "clip_seconds": (20, 40),
        # Becomes TRANSCRIBE_LANGUAGE: Hindi decode, romanised by translit.py.
        "transcribe_language": "hinglish",
        # Becomes TRANSCRIBE_PROMPT: names / words Whisper keeps getting wrong.
        "vocabulary": "",
        # NOTE: everything below is deliberately about the LANGUAGE and about
        # what any clip has to do to stand alone. It says nothing about what
        # this channel is or who watches it, because nobody has measured that
        # yet. Add a "WHAT WINS" section here from your own numbers — the top
        # and bottom clips by viewed-vs-swiped — and not from taste.
        "context": """AUDIENCE: Hindi / Hinglish speakers watching Shorts on a phone.

WHAT THE CLIP MUST DO:
- Stand alone. Someone who has seen nothing else must understand it.
- Contain its own payoff. On comedy that means the full setup AND the
  punchline, ending 1-2 s after the laugh; a clip that cuts before the payoff
  is worthless however good the setup was.
- Open on the line that carries the moment. Never on a greeting, applause, a
  host introduction, a sponsor read or filler.

COPY STYLE (overrides any other language rule):
- Title and hook in Hinglish written in ROMAN script, the way people type it
  in a chat. Not pure English, not Devanagari.
- The hook names the situation or tension of THIS clip, not the show or the
  topic in general.
- Max 1 emoji. No "POV:" and no translated English templates.
- Hashtags: 3-5, drawn from what the clip is actually about.
- Never put words in a real person's mouth that they did not say.""",
    },
    "us": {
        "label": "USA - English",
        "languages": {"en"},
        "clip_seconds": (25, 50),
        "transcribe_language": "en",
        "vocabulary": "",
        # Same note as the India profile: no claim here about the channel's
        # subject or audience, because none has been measured.
        "context": """AUDIENCE: English speakers watching Shorts on a phone.

WHAT THE CLIP MUST DO:
- One idea per clip, and it must be clear without the rest of the video.
- Open on the idea itself, not on background or context-setting.
- Land a concrete payoff: a number, a reveal, a name, or something the viewer
  could repeat to someone else.
- Cut rambling setups.

COPY STYLE (overrides any other language rule):
- Plain English, sentence case. Curiosity-driven but truthful: no fake claims,
  no ALL CAPS, 0-1 emoji.
- Hook: 3-7 words, a question or a statement about THIS moment specifically.
- Hashtags: 3-5, drawn from what the clip is actually about. No #fyp or
  #viral spam.
- Money, insurance, health or legal topics: no promises, no guarantees, no
  "you should buy/cancel X" framing.
- Never put words in a real person's mouth that they did not say.""",
    },
}

SHARED_RULES = """CLIP RULES:
- Each clip {lo} to {hi} seconds. Times in SECONDS from the start of the video, taken from the transcript lines.
- 2-SECOND RULE: the first line must stop a cold viewer from scrolling.
- STANDS ALONE: someone who has seen nothing else must understand it. If it needs context, move the START earlier - never cut the payoff.
- PAYOFF INSIDE: the punchline / reveal / conclusion must be inside the clip.
- No two clips that make the same point or tell the same joke.
- Skip intros, outros, sponsor reads and filler.
- Pick {min_clips} to {max_clips} clips. Quality over quantity: never pad with a clip you would not post yourself."""

OUTPUT_FORMAT = """COPY FIELDS:
- title: max 70 characters.  - hook: on-screen text for the first seconds, max 8 words.
- description: 1 sentence teasing the payoff + the hashtags.  - tags: 8-15 comma-separated YouTube tags, no "#".
- reason: one line on why this moment works (for me, not for viewers).

Return ONLY this JSON, best clip first, no other text:
{"shorts": [
  {"start": 123.4, "end": 158.9, "score": 85, "title": "...", "hook": "...", "description": "...", "tags": "...", "reason": "..."}
]}"""


def pick(language=None, override=None):
    """Profile key ('in' / 'us') or None when nothing matches (generic behaviour)."""
    choice = (override or os.environ.get("AUDIENCE") or "").strip().lower()
    if choice in PROFILES:
        return choice
    lang = (language or "").strip().lower()
    for key, profile in PROFILES.items():
        if lang in profile["languages"]:
            return key
    return None


def apply_transcription_settings(key):
    """Per-run defaults for a CLI job. Anything already in the env (a dashboard
    job, .env, or -e on the command line) wins."""
    if key not in PROFILES:
        return
    profile = PROFILES[key]
    os.environ.setdefault("TRANSCRIBE_LANGUAGE", profile["transcribe_language"])
    if profile["vocabulary"]:
        os.environ.setdefault("TRANSCRIBE_PROMPT", profile["vocabulary"])
    lo, hi = profile["clip_seconds"]
    os.environ.setdefault("CLIP_MIN_SECONDS", str(lo))
    os.environ.setdefault("CLIP_MAX_SECONDS", str(hi))


def chat_prompt(key, clip_counts=None):
    """Full prompt for Claude.ai / ChatGPT, placed above the transcript.

    ``clip_counts`` is the (min, max) band this video's length justifies —
    the same one the Gemini detail pass gets. It used to be a hardcoded
    "3 to 8" whatever the source, so a 60-minute episode was asked for fewer
    clips through the chat route than through the dashboard (6-12 there),
    which also made the two routes incomparable for the picked_by A/B: one
    picker was simply allowed more clips than the other.
    """
    lo, hi = PROFILES[key]["clip_seconds"] if key in PROFILES else (20, 45)
    min_clips, max_clips = clip_counts or (3, 8)
    parts = ["You are a senior YouTube Shorts editor. Below is a timestamped transcript of a long video. "
             "Read ALL of it, then pick the moments most likely to go viral as standalone Shorts."]
    if key in PROFILES:
        parts.append(PROFILES[key]["context"])
    parts += [SHARED_RULES.format(lo=lo, hi=hi,
                                  min_clips=min_clips, max_clips=max_clips),
              OUTPUT_FORMAT]
    return "\n\n".join(parts)


def gemini_block(key):
    """Context put on top of OpenShorts' own Gemini prompts ('' when no profile)."""
    if key not in PROFILES:
        return ""
    return ("AUDIENCE PROFILE - these rules take priority over any conflicting rule below.\n\n"
            + PROFILES[key]["context"] + "\n\n---\n\n")
