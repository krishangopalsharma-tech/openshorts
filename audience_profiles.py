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
        "context": """AUDIENCE: Indian YouTube Shorts viewers, 18-35, watching on mobile, Hindi/Hinglish speakers.

WHAT WINS WITH THIS AUDIENCE:
- Relatable desi situations: family, shaadi, office, boss, salary, padosi, school, exams, cricket, Bollywood.
- Banter, roasts, savage replies, and "yeh kya bol diya" moments. The viewer should want to forward it on WhatsApp.
- Emotion over information: big laugh, shock, pride, nostalgia.
- COMEDY: the clip MUST contain the full setup AND the punchline, ending 1-2 s after the laugh. A clip that cuts before the payoff is worthless.
- Open on the setup line or the most outrageous line. Never on a greeting, applause, host intro or filler.

COPY STYLE (overrides any other language rule):
- title and hook in natural Hinglish written in ROMAN script, the way friends text each other
  (e.g. "Jab boss ne salary ka sawaal pucha 😂"). Not pure English, not Devanagari.
- The hook names the situation or tension of THIS clip, not the show or the topic in general.
- Max 1 emoji. No "POV:" or translated English templates.
- Hashtags: 3-5, mix Hinglish and English, e.g. #comedy #hindicomedy #funny plus 1-2 topic tags.
- Never put words in a real person's mouth that they did not say.""",
    },
    "us": {
        "label": "USA - English",
        "languages": {"en"},
        "clip_seconds": (25, 50),
        "transcribe_language": "en",
        "vocabulary": "",
        "context": """AUDIENCE: US YouTube Shorts viewers watching on mobile, American English.

WHAT WINS WITH THIS AUDIENCE:
- One clear idea per clip, delivered fast: a surprising fact, a counterintuitive claim, a story with a twist, or a strong opinion.
- Open on the claim or the conflict itself ("Most people pay twice for this..."), never on background or context-setting.
- The payoff must be concrete: a number, a reveal, a name, or a takeaway the viewer can repeat to a friend.
- Cut rambling setups. US viewers swipe away within 1-2 seconds of a slow start.

COPY STYLE (overrides any other language rule):
- Plain American English, sentence case. Curiosity-driven but truthful: no fake claims, no ALL CAPS, 0-1 emoji.
- Hook: 3-7 words, a question or bold statement about THIS moment specifically.
- Hashtags: 3-5 topical tags (e.g. #personalfinance #history #science). No #fyp or #viral spam.
- Money, insurance, health or legal topics: no promises, guarantees, or "you should buy/cancel X" advice framing.""",
    },
}

SHARED_RULES = """CLIP RULES:
- Each clip {lo} to {hi} seconds. Times in SECONDS from the start of the video, taken from the transcript lines.
- 2-SECOND RULE: the first line must stop a cold viewer from scrolling.
- STANDS ALONE: someone who has seen nothing else must understand it. If it needs context, move the START earlier - never cut the payoff.
- PAYOFF INSIDE: the punchline / reveal / conclusion must be inside the clip.
- No two clips that make the same point or tell the same joke.
- Skip intros, outros, sponsor reads and filler.
- Pick 3 to 8 clips. Quality over quantity: never pad with a clip you would not post yourself."""

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


def chat_prompt(key):
    """Full prompt for Claude.ai / ChatGPT, placed above the transcript."""
    lo, hi = PROFILES[key]["clip_seconds"] if key in PROFILES else (20, 45)
    parts = ["You are a senior YouTube Shorts editor. Below is a timestamped transcript of a long video. "
             "Read ALL of it, then pick the moments most likely to go viral as standalone Shorts."]
    if key in PROFILES:
        parts.append(PROFILES[key]["context"])
    parts += [SHARED_RULES.format(lo=lo, hi=hi), OUTPUT_FORMAT]
    return "\n\n".join(parts)


def gemini_block(key):
    """Context put on top of OpenShorts' own Gemini prompts ('' when no profile)."""
    if key not in PROFILES:
        return ""
    return ("AUDIENCE PROFILE - these rules take priority over any conflicting rule below.\n\n"
            + PROFILES[key]["context"] + "\n\n---\n\n")
