"""Movie Shorts: the planning layer for standalone ~2-minute shorts cut from a
feature film. Two chat passes and a validator; the render lives in film_prep.

A short is ONE angle on the whole film ("He has a dual personality"),
assembled from 14-18 story beats that may come from anywhere in the runtime,
with the film's own dialogue as captions and a music bed. No narrator. The
model plans BEATS; the renderer expands each into 5-8 rapid shots, so the
word "shot" never appears in a prompt.

Nothing here calls an LLM. ``build_hooks_prompt`` / ``build_beats_prompt``
return text the user pastes into Claude or ChatGPT; ``parse_json`` reads the
answer back; ``validate_hooks`` / ``validate_beats`` return a structured error
list the dashboard renders and the user pastes back as a correction turn.
That loop is the whole UX.

The rule that matters most: **every caption must fuzzy-match a real cue**
(``CAPTION_MATCH_RATIO``). Models invent plausible film dialogue confidently
and often; without this check the pipeline burns subtitles nobody said.
"""

import json
import re
from difflib import SequenceMatcher
from typing import List, Optional

from pydantic import BaseModel, Field, ValidationError

import film_prep as fp

MOODS = ("tense", "ominous", "sad", "epic", "romantic", "eerie", "driving")

DEFAULT_TARGET_SECONDS = 120.0
TARGET_RANGE = (90.0, 150.0)
DEFAULT_SPEED = fp.DEFAULT_SPEED
DEFAULT_HOOK_COUNT = 8
HOOK_COUNT_RANGE = (3, 12)

BEATS_RANGE = (14, 18)
BEAT_SECONDS_RANGE = (8.0, 15.0)
FOOTAGE_TOLERANCE = 0.08
MIN_SPAN_MINUTES = 12.0
TITLE_MAX_WORDS = 8
BANNED_TITLE_WORDS = ("part", "explained", "recap", "summary", "ending", "full story")

CAPTION_MATCH_RATIO = 0.8
CAPTION_WINDOW_PAD = 1.5  # seconds either side of the beat a cue may sit


# --- prompts ----------------------------------------------------------------

HOOKS_PROMPT = """You are finding the angles for a set of standalone short videos made from one
film. Each short is about {TARGET_SECONDS} seconds of fast-cut clips with the film's own
dialogue shown as on-screen captions. There is no narrator.

Each short is COMPLETE ON ITS OWN. It is not part of a series. A viewer
watching it has not seen the film and will not watch another short. It opens
cold, and it pays off before it ends.

A hook is ONE angle on the whole film, not a scene and not a section. Its
clips may come from anywhere in the runtime. Good angles: a character's
defining trait, a relationship that turns, a deception and its exposure, a
thing that is not what it appears to be.

Below is a time-indexed digest of the film's subtitles. Runtime: {RUNTIME}.

Return ONLY JSON, no preamble, no code fences:

{{
  "film": {{"premise": "Two sentences, setup only."}},
  "hooks": [
    {{
      "index": 1,
      "title": "A STATEMENT about a character, present tense, under 8 words,
        no punctuation. The register: 'He has a dual personality',
        'No one dares to provoke him', 'A student disguised by a killer'.",
      "angle": "One sentence: what this short is actually about.",
      "payoff": "What the viewer understands by the end. Be specific.",
      "cold_open": "The single moment that should be the first shot, with a
        timestamp as MM:SS or HH:MM:SS. It must make the title make sense
        within two seconds.",
      "cold_open_at": "HH:MM:SS",
      "spans": [
        {{"start": "00:04:10", "end": "00:09:00"}},
        {{"start": "01:12:00", "end": "01:20:30"}}
      ],
      "mood": "one of: tense, ominous, sad, epic, romantic, eerie, driving",
      "strength": 1
    }}
  ]
}}

Return {HOOK_COUNT} hooks, ordered by "strength" (1 = strongest). Each hook's
"spans" are the regions of the film it draws from; they may overlap between
hooks but must total at least {MIN_SPAN_MINUTES} minutes so the beat pass has room to choose.

Titles must not contain: Part, Explained, Recap, Summary, Ending, Full Story.
Two hooks must not share the same payoff.

SUBTITLE DIGEST:
{DIGEST}
"""

BEATS_PROMPT = """You are choosing the clips for a standalone short titled "{TITLE}".

The short is about: {ANGLE}
By the end the viewer must understand: {PAYOFF}
The first shot is: {COLD_OPEN} (at {COLD_OPEN_AT})
Draw only from these regions of the film: {SPANS}

BUDGET - hard limits:
- {BEATS_MIN} to {BEATS_MAX} beats
- each beat {BEAT_MIN} to {BEAT_MAX} seconds of source footage
- total source footage {FOOTAGE_BUDGET} seconds, tolerance {TOLERANCE_PCT}%
- beats in ascending time order, non-overlapping
- the FIRST beat must contain {COLD_OPEN_AT}
- the LAST beat must deliver the payoff

A BEAT is a moment of story, not a single shot. The renderer cuts each beat
into several rapid shots automatically, so choose beats that sustain 5 to 8
seconds of screen time: a confrontation, a realisation, an arrival. Do not
choose 2-second reaction shots.

For each beat, list the dialogue lines from the digest that must appear as
captions, COPIED EXACTLY from the digest (never paraphrased, never invented),
and mark at most one word per line for emphasis. Times are seconds from the
start of the film.

Return ONLY JSON, no preamble, no code fences:

{{
  "hook_index": {N},
  "beats": [
    {{
      "start": 132.5,
      "end": 143.8,
      "why": "What this beat contributes to the payoff.",
      "captions": [
        {{"at": 134.2, "text": "I never had any children", "emphasis": "never"}},
        {{"at": 139.0, "text": "Your daughter, her name was", "emphasis": null}}
      ],
      "weight": "normal | key",
      "composite_ok": true
    }}
  ]
}}

"weight": "key" marks the 2-3 beats carrying the short; the renderer gives them
slightly longer shots. "composite_ok": false for beats whose framing must not
be split into a two-panel layout.

SUBTITLE DIGEST (only the regions this short draws from):
{DIGEST}
"""


def footage_budget(target_seconds=DEFAULT_TARGET_SECONDS, speed=DEFAULT_SPEED):
    """Source seconds a short may use: finished runtime x playback speed."""
    return round(float(target_seconds) * float(speed), 1)


def build_hooks_prompt(cues, duration, hook_count=DEFAULT_HOOK_COUNT,
                       target_seconds=DEFAULT_TARGET_SECONDS):
    hook_count = int(min(HOOK_COUNT_RANGE[1], max(HOOK_COUNT_RANGE[0], hook_count)))
    return HOOKS_PROMPT.format(
        TARGET_SECONDS=int(target_seconds),
        RUNTIME=fp.format_timestamp(duration, hours=True),
        HOOK_COUNT=hook_count,
        MIN_SPAN_MINUTES=int(MIN_SPAN_MINUTES),
        DIGEST=fp.digest(cues),
    )


def build_beats_prompt(cues, hook, target_seconds=DEFAULT_TARGET_SECONDS,
                       speed=DEFAULT_SPEED):
    """``hook`` is a validated ``Hook`` (or its dict). Digest carries only
    the hook's spans so the model cannot wander outside them."""
    hook = hook if isinstance(hook, Hook) else Hook.model_validate(hook)
    parts = []
    for span in hook.spans:
        s, e = span.seconds()
        parts.append(fp.digest(cues, start=s, end=e))
    spans_text = ", ".join(
        f"{fp.format_timestamp(sp.seconds()[0], hours=True)}-"
        f"{fp.format_timestamp(sp.seconds()[1], hours=True)}" for sp in hook.spans)
    return BEATS_PROMPT.format(
        TITLE=hook.title, ANGLE=hook.angle, PAYOFF=hook.payoff,
        COLD_OPEN=hook.cold_open,
        COLD_OPEN_AT=fp.format_timestamp(hook.cold_open_seconds(), hours=True),
        SPANS=spans_text, N=hook.index,
        BEATS_MIN=BEATS_RANGE[0], BEATS_MAX=BEATS_RANGE[1],
        BEAT_MIN=int(BEAT_SECONDS_RANGE[0]), BEAT_MAX=int(BEAT_SECONDS_RANGE[1]),
        FOOTAGE_BUDGET=footage_budget(target_seconds, speed),
        TOLERANCE_PCT=int(FOOTAGE_TOLERANCE * 100),
        DIGEST="\n\n".join(p for p in parts if p),
    )


# --- reading the answer back ------------------------------------------------

class PlanError(ValueError):
    """The pasted text is not usable JSON. Message is safe for the UI."""


def parse_json(text):
    """Pull the JSON object out of a chat answer: code fences, preamble and a
    trailing sentence are all tolerated. Raises PlanError."""
    if isinstance(text, dict):
        return text
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise PlanError("no JSON object found in the pasted text")
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise PlanError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from exc


# --- schemas ----------------------------------------------------------------

class Span(BaseModel):
    start: str | float
    end: str | float

    def seconds(self):
        return fp.parse_timestamp(self.start), fp.parse_timestamp(self.end)


class Hook(BaseModel):
    index: int
    title: str
    angle: str
    payoff: str
    cold_open: str
    cold_open_at: Optional[str | float] = None
    spans: List[Span]
    mood: str
    strength: int = 1

    def cold_open_seconds(self):
        if self.cold_open_at is not None:
            return fp.parse_timestamp(self.cold_open_at)
        m = re.search(r"(\d{1,2}:)?\d{1,2}:\d{2}", self.cold_open)
        if not m:
            raise fp.SubtitleError("cold_open has no timestamp")
        return fp.parse_timestamp(m.group(0))


class HooksPlan(BaseModel):
    film: dict = Field(default_factory=dict)
    hooks: List[Hook]


class Caption(BaseModel):
    at: float
    text: str
    emphasis: Optional[str] = None


class Beat(BaseModel):
    start: float
    end: float
    why: str = ""
    captions: List[Caption] = Field(default_factory=list)
    weight: str = "normal"
    composite_ok: bool = True
    blur: Optional[dict] = None  # set by hand in the shots preview


class BeatPlan(BaseModel):
    hook_index: int
    beats: List[Beat]


def _err(code, message, **where):
    out = {"code": code, "message": message}
    out.update(where)
    return out


def _schema_errors(exc):
    errors = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()))
        errors.append(_err("schema", f"{loc}: {e.get('msg')}", path=loc))
    return errors


# --- hooks validation -------------------------------------------------------

def validate_hooks(data, duration):
    """Returns ``(HooksPlan | None, errors)``. Errors are hard failures."""
    try:
        plan = HooksPlan.model_validate(data)
    except ValidationError as exc:
        return None, _schema_errors(exc)

    errors = []
    payoffs = {}
    for h in plan.hooks:
        title_words = h.title.strip().split()
        if len(title_words) > TITLE_MAX_WORDS:
            errors.append(_err("title_length",
                               f'title "{h.title}" has {len(title_words)} words (max {TITLE_MAX_WORDS})',
                               hook=h.index))
        low = h.title.lower()
        for banned in BANNED_TITLE_WORDS:
            if re.search(r"\b" + re.escape(banned) + r"\b", low):
                errors.append(_err("title_banned", f'title "{h.title}" contains "{banned}"',
                                   hook=h.index))
        if h.mood not in MOODS:
            errors.append(_err("mood", f'mood "{h.mood}" is not one of {", ".join(MOODS)}',
                               hook=h.index))
        total = 0.0
        for i, sp in enumerate(h.spans):
            try:
                s, e = sp.seconds()
            except fp.SubtitleError as exc:
                errors.append(_err("span", f"span {i}: {exc}", hook=h.index))
                continue
            if e <= s:
                errors.append(_err("span", f"span {i} ends before it starts", hook=h.index))
            if e > duration + 1:
                errors.append(_err("span", f"span {i} ends after the film ({fp.format_timestamp(duration, hours=True)})",
                                   hook=h.index))
            total += max(0.0, e - s)
        if total < MIN_SPAN_MINUTES * 60:
            errors.append(_err("spans_short",
                               f"spans total {total / 60:.1f} min; at least {MIN_SPAN_MINUTES:.0f} needed",
                               hook=h.index))
        try:
            co = h.cold_open_seconds()
            if not any(sp.seconds()[0] - 1 <= co <= sp.seconds()[1] + 1 for sp in h.spans):
                errors.append(_err("cold_open", "cold_open timestamp is outside every span",
                                   hook=h.index))
        except fp.SubtitleError as exc:
            errors.append(_err("cold_open", str(exc), hook=h.index))
        key = re.sub(r"\W+", " ", h.payoff.lower()).strip()
        if key in payoffs:
            errors.append(_err("payoff_dup", f"payoff repeats hook {payoffs[key]}", hook=h.index))
        payoffs.setdefault(key, h.index)
    return plan, errors


# --- beats validation -------------------------------------------------------

_NORM_RE = re.compile(r"[^\w\s']+", re.UNICODE)


def _norm_text(text):
    return re.sub(r"\s+", " ", _NORM_RE.sub("", str(text).lower())).strip()


def match_caption(text, cues, start, end, pad=CAPTION_WINDOW_PAD):
    """Best fuzzy match of ``text`` against cues overlapping ``[start-pad,
    end+pad]``. Returns ``(ratio, cue | None)``. A caption that is a clean
    substring of a longer (merged) cue counts as a full match."""
    want = _norm_text(text)
    if not want:
        return 0.0, None
    best, best_cue = 0.0, None
    for cue in cues:
        if cue["end"] < start - pad or cue["start"] > end + pad:
            continue
        have = _norm_text(cue["text"])
        if not have:
            continue
        if want in have or have in want:
            ratio = 1.0 if want in have else len(have) / float(len(want))
        else:
            ratio = SequenceMatcher(None, want, have).ratio()
        if ratio > best:
            best, best_cue = ratio, cue
    return best, best_cue


def validate_beats(data, hook, cues, duration, target_seconds=DEFAULT_TARGET_SECONDS,
                   speed=DEFAULT_SPEED):
    """Returns ``(BeatPlan | None, errors)``. Every rule the model tends to
    break is here; the caption match is the one that protects the viewer."""
    try:
        plan = BeatPlan.model_validate(data)
    except ValidationError as exc:
        return None, _schema_errors(exc)
    hook = hook if isinstance(hook, Hook) else Hook.model_validate(hook)

    errors = []
    n = len(plan.beats)
    if not BEATS_RANGE[0] <= n <= BEATS_RANGE[1]:
        errors.append(_err("beat_count", f"{n} beats; need {BEATS_RANGE[0]}-{BEATS_RANGE[1]}"))
    if plan.hook_index != hook.index:
        errors.append(_err("hook_index", f"plan is for hook {plan.hook_index}, expected {hook.index}"))

    spans = [sp.seconds() for sp in hook.spans]
    total = 0.0
    prev_end = -1.0
    for i, b in enumerate(plan.beats):
        length = b.end - b.start
        total += max(0.0, length)
        if b.end <= b.start:
            errors.append(_err("beat_range", "ends before it starts", beat=i))
            continue
        if b.start < 0 or b.end > duration + 0.5:
            errors.append(_err("beat_range", "outside the film", beat=i))
        if not BEAT_SECONDS_RANGE[0] - 0.05 <= length <= BEAT_SECONDS_RANGE[1] + 0.05:
            errors.append(_err("beat_length", f"{length:.1f} s; each beat must be "
                               f"{BEAT_SECONDS_RANGE[0]:.0f}-{BEAT_SECONDS_RANGE[1]:.0f} s", beat=i))
        if b.start < prev_end:
            errors.append(_err("beat_order", "overlaps or precedes the previous beat", beat=i))
        prev_end = b.end
        if spans and not any(s - 0.5 <= b.start and b.end <= e + 0.5 for s, e in spans):
            errors.append(_err("beat_span", "outside every span of the hook", beat=i))
        if b.weight not in ("normal", "key"):
            errors.append(_err("weight", f'weight "{b.weight}" must be normal or key', beat=i))
        if not b.captions:
            errors.append(_err("no_caption", "beat has no caption", beat=i))
        for j, c in enumerate(b.captions):
            if not b.start - 0.5 <= c.at <= b.end + 0.5:
                errors.append(_err("caption_time", f"caption {j} at {c.at:.1f} is outside the beat",
                                   beat=i, caption=j))
            ratio, cue = match_caption(c.text, cues, b.start, b.end)
            if ratio < CAPTION_MATCH_RATIO:
                near = f' (closest: "{cue["text"]}", {ratio:.2f})' if cue else ""
                errors.append(_err("caption_text",
                                   f'caption {j} "{c.text}" matches no subtitle in this beat{near}. '
                                   "Copy lines from the digest; never invent dialogue.",
                                   beat=i, caption=j))
            if c.emphasis:
                words = {_norm_text(w) for w in c.text.split()}
                if _norm_text(c.emphasis) not in words:
                    errors.append(_err("emphasis", f'emphasis "{c.emphasis}" is not a word of caption {j}',
                                       beat=i, caption=j))

    budget = footage_budget(target_seconds, speed)
    if plan.beats and abs(total - budget) > budget * FOOTAGE_TOLERANCE:
        errors.append(_err("footage", f"{total:.1f} s of footage against a budget of {budget:.1f} s "
                           f"(±{int(FOOTAGE_TOLERANCE * 100)}%)"))
    if plan.beats:
        try:
            co = hook.cold_open_seconds()
            first = plan.beats[0]
            if not first.start - 0.5 <= co <= first.end + 0.5:
                errors.append(_err("cold_open", f"first beat does not contain the cold open at "
                                   f"{fp.format_timestamp(co, hours=True)}", beat=0))
        except fp.SubtitleError:
            pass
    keys = sum(1 for b in plan.beats if b.weight == "key")
    if plan.beats and keys == 0:
        errors.append(_err("no_key", "no beat is marked weight: key (2-3 expected)"))
    return plan, errors


def format_errors(errors):
    """One line per error, ready to paste back to the model."""
    lines = []
    for e in errors:
        where = []
        if "hook" in e:
            where.append(f"hook {e['hook']}")
        if "beat" in e:
            where.append(f"beat {e['beat'] + 1}")
        prefix = " ".join(where)
        lines.append(f"- {prefix + ': ' if prefix else ''}{e['message']}")
    return "\n".join(lines)


def summarize(plan):
    """Numbers the dashboard shows next to a validated beat plan."""
    beats = plan.beats if isinstance(plan, BeatPlan) else BeatPlan.model_validate(plan).beats
    lengths = [b.end - b.start for b in beats]
    return {
        "beats": len(beats),
        "footage_seconds": round(sum(lengths), 1),
        "mean_beat_seconds": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
        "key_beats": sum(1 for b in beats if b.weight == "key"),
        "captions": sum(len(b.captions) for b in beats),
        "composite_ok": sum(1 for b in beats if b.composite_ok),
    }
