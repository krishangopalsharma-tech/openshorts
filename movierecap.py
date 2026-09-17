"""Movie Recap: a three-part video essay series that makes the viewer want to
watch the film MORE, planned in a chat window and rendered here.

The design goal overrides everything else: the climax is never shown, never
described, never resolved. A recap that summarises the plot substitutes for
the film (the market-effect factor is what decides these cases, and it is
also the weaker video); a recap that explains how the film is built
advertises it. So each part argues one claim about the film's construction,
the plot is evidence, and every payoff is withheld.

Three chat passes, each cheap and reviewable:

- **A, spoiler map**: the ranges that must never be shown or described,
  tiered ``ending > twist > reveal > midpoint``. Unioned server-side with the
  automatic wall at 75% of runtime and any manual exclusions.
- **B, series structure**: three claims, three titles, what each part
  withholds. One screen of text; the user reviews and edits it here.
- **C, chunk plan**, once per part: 14-22 chunks with narration, inside that
  part's evidence band only.

``validate_plan`` hard-fails timestamps (protected range, wall, band, order,
length, footage budget) and structure, and ``lint_narration`` WARNS on
resolution language (``then he``, ``turns out``, ``finally``...) so the user
reads every hit; some are legitimate in analysis. ``flip`` survives as a
manual editorial flag with a stated reason; more than three per part or a
regular alternation fails, deliberately: uniform mirroring does nothing for
any legal factor and is circumvention under the platform's terms.

Nothing here calls an LLM; the film never leaves the machine.
"""

import math
import re
from typing import List, Optional

from pydantic import BaseModel, Field, ValidationError

import film_prep as fp

PART_TARGET_SECONDS = 150.0
SPEED = fp.DEFAULT_SPEED
WALL_FRACTION = 0.75
CHUNKS_RANGE = (14, 22)
CHUNK_SECONDS_RANGE = (6.0, 15.0)
MEAN_CHUNK_RANGE = (6.0, 13.0)
FOOTAGE_TOLERANCE = 0.08
WORDS_PER_PART = 450
WORDS_TOLERANCE = 0.10
MAX_FLIPS_PER_PART = 3

TIERS = ("ending", "twist", "reveal", "midpoint")
BANNED_TITLE_WORDS = ("setup", "beginning", "middle", "end", "ending", "finale",
                      "explained", "recap", "summary", "full story")
# The narrator's register. The first real scripts (17-sep-2026) came out as a
# lecture: "watch the lens", "the camera holds this kindness like an exhibit",
# "here's the key design decision". Correct, and dead. "story" (default) tells
# the moment from inside it, in the film's mood, and never names the
# apparatus; "essay" is the original video-essay voice for anyone who wants it.
NARRATION_STYLES = ("story", "essay")
MOODS = ("tense", "ominous", "sad", "epic", "romantic", "eerie", "driving", "warm", "bitter")
MOOD_GUIDE = {
    "tense": "short sentences, present tense, breath held; say what could go wrong, not what does",
    "ominous": "slow, low, plain words; let the dread sit in what is not said",
    "sad": "quiet and simple; name the loss plainly, no adjectives doing the crying for you",
    "epic": "long sentences that build; scale, stakes, the size of what is asked of him",
    "romantic": "warm, close, small details noticed the way a lover notices them",
    "eerie": "calm surface, one wrong detail per line; never explain the wrongness",
    "driving": "momentum; verbs at the front, no pauses, one line pushes into the next",
    "warm": "gentle, generous to the people on screen; humour allowed, mockery not",
    "bitter": "dry, clipped, unforgiving; let the irony land without pointing at it",
}
# Lecture vocabulary. Warned per chunk in story mode, never auto-failed: a
# storyteller may say "the camera" once, and a script that says it eleven
# times is the problem the style exists to fix.
CRAFT_PHRASES = (
    "the film", "the camera", "the director", "the script", "the editor", "the editors", "the cut",
    "the edit", "the shot", "the frame", "the score", "the lens", "close-up", "wide frame",
    "wide shot", "establishing", "screen time", "design decision", "rhyme", "rhymes", "notice",
    "watch how", "watch the", "listen to how", "listen for", "the viewer", "the audience",
    "this stretch", "this opening", "part one", "part two", "part three",
)

NARRATION_RULES = {
    "story": """NARRATION RULES - you are a storyteller, not a critic:
- Tell the moment from inside it. What this person wants, what they fear, what
  it costs them to stand there. Present tense. Make the viewer feel the room
  before they understand it.
- The mood of this part is {MOOD}: {MOOD_GUIDE}. Every line carries it.
- Plain spoken words, the way you would tell a friend about a night that
  changed someone. Short sentences. Names, not roles.
- NEVER name the apparatus. Banned: "the film", "the camera", "the director",
  "the script", "the edit", "the cut", "the shot", "the frame", "the score",
  "the lens", "close-up", "wide shot", "screen time", "rhyme", "design",
  "the viewer", "the audience", "part one/two/three".
- Do not lecture. "Notice", "watch how", "listen for" at most ONCE in the whole
  part. You are not pointing at things; you are living them.
- Still no payoff, no resolution, no consequence landing. Say what he is about
  to face, what she does not know yet, what is on the table. Never how it
  goes. Banned: "then he/she", "turns out", "finally", "ends with", "we learn
  that", "it is revealed", "in the end", "the twist is".
- Every line must earn its place by feeling: cut any sentence that only
  informs.
- The last 2-3 lines are the ache the viewer takes away: the open question
  above, felt, not asked like a quiz. Do not answer it.""",
    "essay": """NARRATION RULES:
- Analysis, context, criticism. Never plot recitation.
- Apply this test to every sentence: could it substitute for watching the
  scene? If yes, rewrite it.
- Banned constructions: "then he/she", "turns out", "finally", "ends with",
  "we learn that", "it is revealed", "in the end", "the twist is".
- Address the viewer directly. Tell them what to watch for.
- The last 2-3 lines open the question above. Do not answer it.""",
}

# What the series is FOR. "teaser" is the original design: appetite-building,
# nothing after 75%, every payoff withheld. "story" (the channel's choice,
# 18-sep-2026) tells the whole film in three parts with a cliffhanger between
# them and the ending in part 3: a viewer who loved the telling goes to the
# film, a viewer who was teased scrolls on. Library default stays "teaser" so
# the original tests keep their meaning; film_api defaults sessions to "story".
RECAP_MODES = ("story", "teaser")

NARRATION_RULES_FULL = """NARRATION RULES - you are telling the whole story, as a storyteller:
- Line 1 is the hook: one sentence a stranger cannot scroll past. A person, a
  choice, a cost. No throat-clearing, no "in this film".
- Then tell what happens, in order, cause and effect. Present tense. Names,
  not roles. What this person wants, what stands in the way, what it costs.
- The mood of this part is {MOOD}: {MOOD_GUIDE}. Every line carries it.
- Plain spoken words, short sentences, the way you would tell a friend about
  a night that changed someone. Make the viewer feel the room before they
  understand it. Stakes in every third line at the latest.
- NEVER name the apparatus. Banned: "the film", "the camera", "the director",
  "the script", "the edit", "the cut", "the shot", "the frame", "the score",
  "the lens", "close-up", "wide shot", "screen time", "rhyme", "design",
  "the viewer", "the audience". Say "he", "she", "Sam", never "we see".
- Do not lecture. "Notice", "watch how", "listen for" at most ONCE in the
  whole part. You are not pointing at things; you are living them.
- Spoilers are allowed and wanted. Tell the twist when it comes. Tell the
  ending in part 3. What you withhold is only TIMING: never announce a beat
  before its clip; land it on the clip that shows it.
- Each chunk's line says what is happening in that footage and what it
  means to the person in it. No line that only informs.
- {ENDING_RULE}"""

ENDING_RULES = {
    "cliffhanger": ("The last 2-3 lines are the cliffhanger: the thing that has just gone "
                    "wrong or the choice now on the table, and one question the viewer cannot "
                    "leave without answering. Point straight at the next part."),
    "ending": ("This is the last part: tell the ending, all of it, and let the last 2 lines "
               "land what it cost and what it meant. One sentence of stillness at the end; "
               "no moral, no 'and that is why'."),
}

CLIP_RULES = {
    "teaser": """CLIP SELECTION RULES:
- Prefer the moment BEFORE a beat: the reaction without the cause, the
  establishing wide, the breath before the line.
- Cut out of scenes early. Leaving the viewer hanging is the point.
- Never use the film's best shot or best line. Use the second-best.
- No chunk may show an outcome, a resolution, or a consequence landing.""",
    "story": """CLIP SELECTION RULES:
- Pick the moments that CARRY the story: the choice, the blow, the look that
  answers it. Every chunk should move the story one step.
- Faces over places. A reaction beats an establishing shot every time.
- Chunks in story order. The part must read as one continuous telling.
- Land each beat on the footage that shows it; the narration for a chunk is
  about what is on screen in that chunk.
- The last chunk of a part is its cliffhanger (or, in the last part, the
  ending); spend a full 10-15 s chunk on it.""",
}

PROMPT_A_STORY = """You are helping build a three-part story retelling of a film for a short-video
channel. Your only job in this step is to map the story: who, what they want,
what stops them, where it turns, how it ends. Spoilers are wanted.

Below is a time-indexed digest of the film's subtitles. Runtime: {RUNTIME}.

Return ONLY JSON, no preamble, no markdown fences:

{{
  "premise": "Two sentences. A person, a want, an obstacle.",
  "protagonist": "Name and one line: who they are when we meet them.",
  "want": "What they want, plainly.",
  "obstacle": "What stands in the way, plainly.",
  "hook_line": "One sentence that would make a stranger stop scrolling. A
    person, a choice, a cost. No 'in this film'.",
  "turning_points": [
    {{"at": "HH:MM:SS", "what": "One sentence: the event and what it costs
      someone. 6 to 10 of these, in order, covering the WHOLE film."}}
  ],
  "climax_at": "HH:MM:SS",
  "ending": "What happens at the end and what it means, plainly, 2-3 sentences.",
  "mood_arc": ["one mood per act, three entries, from: tense, ominous, sad,
    epic, romantic, eerie, driving, warm, bitter"]
}}

SUBTITLE DIGEST:
{DIGEST}
"""

PROMPT_B_STORY = """You are structuring a three-part story retelling of a film for a short-video
channel. Each part is {PART_MINUTES} minutes of narration over clips, told by a
storyteller, not a critic. Spoilers are wanted: the series tells the whole
film, in order, and part 3 tells the ending.

THE RULE THAT OVERRIDES EVERYTHING: a viewer who watches part 1 must NEED
part 2. Each of parts 1 and 2 ends on a cliffhanger: the thing that just went
wrong or the choice now on the table. Part 3 pays everything off.

Story map: {PREMISE}
Protagonist: {PROTAGONIST}. Wants: {WANT}. Against: {OBSTACLE}.
Turning points: {TURNING_POINTS}
Climax at {CLIMAX_AT}. Ending: {ENDING}
Mood arc: {MOOD_ARC}
{EXCLUDED}

Return ONLY JSON:

{{
  "series_title": "...",
  "parts": [
    {{
      "index": 1,
      "title": "A spoken hook, present tense, under 10 words, a statement not a
        chapter heading. 'He walked out of prison and straight into a fight'
        - never 'Part 1', 'The Setup', 'Explained'.",
      "claim": "2-3 sentences: the stretch of story this part tells, with its
        turning points named.",
      "evidence_band": {{"start": "00:00:00", "end": "00:38:00"}},
      "open_question": "The cliffhanger this part ends on (parts 1 and 2), or
        for part 3 the last thing the viewer is left holding.",
      "mood": "one of: tense, ominous, sad, epic, romantic, eerie, driving,
        warm, bitter"
    }}
  ]
}}

The three evidence_bands cover the film in order, part 3's ends at the film's
end ({RUNTIME}). Titles must not contain: Explained, Recap, Summary, Part.

SUBTITLE DIGEST:
{DIGEST}
"""

RESOLUTION_PHRASES = (
    "then he", "then she", "then they", "turns out", "finally", "ends with", "we learn",
    "it is revealed", "it's revealed", "in the end", "the twist is", "dies", "kills",
    "escapes", "wins", "survives", "is dead", "was dead", "all along",
)
NOTHING_WORDS = ("nothing", "none", "n/a", "na", "-", "")

# --- prompts ----------------------------------------------------------------

PROMPT_A = """You are helping build a spoiler-free video essay about a film. Your only job
in this step is to identify what must never be shown or described.

Below is a time-indexed digest of the film's subtitles. Runtime: {RUNTIME}.

Return ONLY JSON, no preamble, no markdown fences:

{{
  "protected": [
    {{"start": "01:42:10", "end": "02:03:40", "tier": "ending",
     "why": "final confrontation and resolution"}},
    {{"start": "01:18:05", "end": "01:21:30", "tier": "twist",
     "why": "the reveal the film is built around"}},
    {{"start": "00:52:00", "end": "00:54:15", "tier": "midpoint",
     "why": "outcome of the central bargain"}}
  ],
  "safe_premise": "One or two sentences covering only what the film's own
    marketing would have revealed.",
  "central_question": "The dramatic question the film poses early and answers
    late, phrased WITHOUT the answer.",
  "twist_exists": true,
  "twist_location": "01:18:05",
  "twist_effect": "What the twist does to the viewer's understanding, stated
    without revealing its content."
}}

Tiers, most to least protected: "ending", "twist", "reveal", "midpoint".
Be generous - over-protecting costs nothing here. Include any range where a
consequence lands, not just where it is announced.

SUBTITLE DIGEST:
{DIGEST}
"""

PROMPT_B = """You are writing a three-part video essay series about a film. Each part is
{PART_MINUTES} minutes of narration over clips.

THE RULE THAT OVERRIDES EVERYTHING: after all three parts, a viewer who has
not seen the film must want to watch it MORE than before. You are advertising
the film, not summarising it. If a viewer finishes your series feeling they
now know the story, you have failed.

Therefore:
- Each part argues ONE claim about how the film is built - its method, its
  visual grammar, a pattern it repeats, a lie it tells on purpose.
- The plot is evidence for the claim, never the subject.
- You give setups and stakes in full. You never give a payoff.
- You may say a twist exists, where it falls, and what it does to the viewer.
  You may NEVER say what it is.
- Nothing from these protected ranges may be shown or described:
  {PROTECTED_RANGES}
- Nothing from after {WALL} may be used at all.

Safe premise you may rely on: {SAFE_PREMISE}
Central question, unanswered: {CENTRAL_QUESTION}
Twist exists: {TWIST_EXISTS} at {TWIST_LOCATION}; its effect: {TWIST_EFFECT}

Part 3 is the hardest and must NOT become the film's third act. Its subject is
why the ending's design only works when experienced, and what a first-time
viewer should watch for. It presupposes watching the film.

Return ONLY JSON:

{{
  "series_title": "...",
  "parts": [
    {{
      "index": 1,
      "title": "A claim, not a chapter heading. 'Why the first twenty minutes
        lie to you' - not 'The Setup'.",
      "claim": "One sentence: what this part argues about the film's
        construction.",
      "evidence_band": {{"start": "00:00:00", "end": "00:38:00"}},
      "open_question": "The unanswered question the viewer leaves holding.
        About the film's method, not 'what happens next'.",
      "withheld": "What this part deliberately does not say, stated plainly.
        This is a promise you are making.",
      "cannot_deliver": "What only the film itself can give the viewer here.",
      "mood": "one of: tense, ominous, sad, epic, romantic, eerie, driving, warm,
        bitter. The feeling this part should leave, matched to the film's own
        register in this stretch, not to the essay's cleverness."
    }}
  ]
}}

Titles must not contain: Setup, Beginning, Middle, End, Ending, Finale,
Explained, Recap, Summary, Full Story.
Every evidence_band must end at or before {WALL}.
Titles are spoken to a viewer, not written for a seminar: no "grammar",
"design", "structure", "motif" in a title.

SUBTITLE DIGEST (up to the wall):
{DIGEST}
"""

PROMPT_C = """You are planning the clips and narration for Part {N} of three: "{TITLE}".

This part argues: {CLAIM}
It must end leaving the viewer with: {OPEN_QUESTION}
It must NOT reveal: {WITHHELD}
Only the film can give the viewer: {CANNOT_DELIVER}
The mood of this part: {MOOD}

BUDGET - these are hard limits, not targets:
- Finished runtime {TARGET} s at {SPEED}x playback -> {FOOTAGE_BUDGET} s of footage
- {CHUNKS_MIN}-{CHUNKS_MAX} chunks, each {CHUNK_MIN}-{CHUNK_MAX} s (never longer than {CHUNK_MAX})
- ~{WORDS} narration words total, ~{WORDS_PER_CHUNK} per chunk
- Every chunk must have narration. Silent footage is a failure.
- Draw only from {EVIDENCE_BAND}
- Never from: {PROTECTED_RANGES}
- Nothing after {WALL}
- Times are seconds from the start of the film.

{CLIP_RULES}

{NARRATION_RULES}

Return ONLY JSON:

{{
  "index": {N},
  "target_duration": {TARGET},
  "chunks": [
    {{
      "start": 132.5,
      "end": 143.2,
      "narration": "Watch the lens. Fifty mil, eye level, dead still - the
        same setup the film uses for every lie it tells later.",
      "shows": "what is on screen",
      "withholds": "what this chunk deliberately stops short of",
      "keep_audio": false
    }}
  ],
  "closing_lines": ["...", "..."],
  "spoiler_self_check": "State plainly what a viewer could NOT figure out from
    this part. If the answer is 'nothing', the part is wrong."
}}

"keep_audio": true only where the film's own dialogue in that chunk IS the
evidence; the original soundtrack is otherwise muted under the narration.

SUBTITLE DIGEST FOR THIS BAND:
{BAND_DIGEST}
"""


class PlanError(ValueError):
    """The pasted text is not usable JSON. Message is safe for the UI."""


def parse_json(text):
    """Pull the JSON object out of a chat answer: code fences, preamble and a
    trailing sentence are all tolerated. Raises PlanError."""
    import json
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


def wall_seconds(duration, fraction=WALL_FRACTION):
    return round(float(duration) * fraction, 1)


def footage_budget(target=PART_TARGET_SECONDS, speed=SPEED):
    return round(float(target) * float(speed), 1)


def _ranges_text(ranges):
    if not ranges:
        return "(none)"
    return "; ".join(
        f"{fp.format_timestamp(r['start'], hours=True)}-{fp.format_timestamp(r['end'], hours=True)}"
        + (f" ({r['tier']})" if r.get("tier") else "")
        for r in ranges)


def build_prompt_a(cues, duration, mode="teaser"):
    template = PROMPT_A_STORY if mode == "story" else PROMPT_A
    return template.format(RUNTIME=fp.format_timestamp(duration, hours=True),
                           DIGEST=fp.digest(cues))


def build_prompt_b(cues, duration, spoilers, protected, target=PART_TARGET_SECONDS, mode="teaser"):
    """``spoilers``: validated pass-A dict (a spoiler map in teaser mode, a
    story map in story mode); ``protected``: the unioned ranges."""
    if mode == "story":
        tps = spoilers.get("turning_points") or []
        excluded = ""
        manual = [r for r in protected if r.get("tier") == "manual"]
        if manual:
            excluded = "Never use these ranges (the user excluded them): " + _ranges_text(manual)
        return PROMPT_B_STORY.format(
            PART_MINUTES=f"{target / 60:.1f}".rstrip("0").rstrip("."),
            PREMISE=spoilers.get("premise", ""), PROTAGONIST=spoilers.get("protagonist", ""),
            WANT=spoilers.get("want", ""), OBSTACLE=spoilers.get("obstacle", ""),
            TURNING_POINTS="; ".join(f"{t.get('at')}: {t.get('what')}" for t in tps) or "(none given)",
            CLIMAX_AT=spoilers.get("climax_at") or "?", ENDING=spoilers.get("ending", ""),
            MOOD_ARC=", ".join(spoilers.get("mood_arc") or []) or "?",
            EXCLUDED=excluded, RUNTIME=fp.format_timestamp(duration, hours=True),
            DIGEST=fp.digest(cues),
        )
    wall = wall_seconds(duration)
    return PROMPT_B.format(
        PART_MINUTES=f"{target / 60:.1f}".rstrip("0").rstrip("."),
        PROTECTED_RANGES=_ranges_text(protected),
        WALL=fp.format_timestamp(wall, hours=True),
        SAFE_PREMISE=spoilers.get("safe_premise", ""),
        CENTRAL_QUESTION=spoilers.get("central_question", ""),
        TWIST_EXISTS="yes" if spoilers.get("twist_exists") else "no",
        TWIST_LOCATION=spoilers.get("twist_location") or "n/a",
        TWIST_EFFECT=spoilers.get("twist_effect") or "n/a",
        DIGEST=fp.digest(cues, start=0, end=wall),
    )


def build_prompt_c(cues, duration, part, protected, target=PART_TARGET_SECONDS, speed=SPEED,
                   style="story", mode="teaser"):
    """``part``: one validated pass-B part (``Part`` or dict). ``style`` picks
    the narrator's register (NARRATION_STYLES); the part's ``mood`` from
    Pass B shapes the story voice; ``mode`` (RECAP_MODES) decides whether the
    story is told in full or withheld."""
    part = part if isinstance(part, Part) else Part.model_validate(part)
    b_start, b_end = part.band_seconds()
    full = mode == "story"
    wall = float(duration) if full else wall_seconds(duration)
    b_end = min(b_end, wall)
    style = style if style in NARRATION_STYLES else "story"
    mood = (part.mood or "").lower() if part.mood in MOODS else "tense"
    if full:
        ending = ENDING_RULES["ending" if part.index >= 3 else "cliffhanger"]
        rules = NARRATION_RULES_FULL.format(MOOD=mood, MOOD_GUIDE=MOOD_GUIDE[mood], ENDING_RULE=ending)
        withheld = "nothing. Tell it all; only the timing is yours to hold."
        cannot = part.cannot_deliver or "the two hours of being there"
        ranges = [r for r in protected if r.get("tier") == "manual"]
    else:
        rules = NARRATION_RULES[style].format(MOOD=mood, MOOD_GUIDE=MOOD_GUIDE[mood])
        withheld = part.withheld
        cannot = part.cannot_deliver or "the experience of not knowing"
        ranges = protected
    return PROMPT_C.format(
        N=part.index, TITLE=part.title, CLAIM=part.claim,
        OPEN_QUESTION=part.open_question, WITHHELD=withheld,
        CANNOT_DELIVER=cannot,
        MOOD=f"{mood} ({MOOD_GUIDE[mood]})", NARRATION_RULES=rules,
        CLIP_RULES=CLIP_RULES["story" if full else "teaser"],
        TARGET=int(target), SPEED=speed, FOOTAGE_BUDGET=footage_budget(target, speed),
        CHUNKS_MIN=CHUNKS_RANGE[0], CHUNKS_MAX=CHUNKS_RANGE[1],
        CHUNK_MIN=int(CHUNK_SECONDS_RANGE[0]), CHUNK_MAX=int(CHUNK_SECONDS_RANGE[1]),
        WORDS=WORDS_PER_PART, WORDS_PER_CHUNK=round(WORDS_PER_PART / 18),
        EVIDENCE_BAND=f"{fp.format_timestamp(b_start, hours=True)}-{fp.format_timestamp(b_end, hours=True)}",
        PROTECTED_RANGES=_ranges_text(ranges),
        WALL=fp.format_timestamp(wall, hours=True),
        BAND_DIGEST=fp.digest(cues, start=b_start, end=b_end),
    )


# --- schemas ----------------------------------------------------------------

class Range(BaseModel):
    start: str | float
    end: str | float
    tier: Optional[str] = None
    why: Optional[str] = None

    def seconds(self):
        return fp.parse_timestamp(self.start), fp.parse_timestamp(self.end)


class SpoilerMap(BaseModel):
    protected: List[Range] = Field(default_factory=list)
    safe_premise: str = ""
    central_question: str = ""
    twist_exists: bool = False
    twist_location: Optional[str | float] = None
    twist_effect: Optional[str] = None


class TurningPoint(BaseModel):
    at: str | float
    what: str = ""


class StoryMap(BaseModel):
    """Pass A in story mode: the spine of the whole film, spoilers included."""
    premise: str = ""
    protagonist: str = ""
    want: str = ""
    obstacle: str = ""
    hook_line: str = ""
    turning_points: List[TurningPoint] = Field(default_factory=list)
    climax_at: Optional[str | float] = None
    ending: str = ""
    mood_arc: List[str] = Field(default_factory=list)


class Part(BaseModel):
    index: int
    title: str
    claim: str
    evidence_band: Range
    open_question: str = ""
    withheld: str = ""
    cannot_deliver: str = ""
    mood: str = ""

    def band_seconds(self):
        return self.evidence_band.seconds()


class Structure(BaseModel):
    series_title: str = ""
    parts: List[Part]


class Chunk(BaseModel):
    start: float
    end: float
    narration: str = ""
    shows: str = ""
    withholds: str = ""
    keep_audio: bool = False
    flip: bool = False
    flip_reason: Optional[str] = None


class PartPlan(BaseModel):
    index: int
    target_duration: float = PART_TARGET_SECONDS
    chunks: List[Chunk]
    closing_lines: List[str] = Field(default_factory=list)
    spoiler_self_check: str = ""


def _err(code, message, **where):
    out = {"code": code, "message": message}
    out.update(where)
    return out


def _schema_errors(exc):
    return [_err("schema", f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}")
            for e in exc.errors()]


# --- protected ranges -------------------------------------------------------

def normalize_ranges(ranges, duration):
    out = []
    for r in ranges or []:
        try:
            s, e = (r.seconds() if isinstance(r, Range) else
                    (fp.parse_timestamp(r["start"]), fp.parse_timestamp(r["end"])))
        except (KeyError, TypeError, fp.SubtitleError):
            continue
        s, e = max(0.0, s), min(float(duration), e)
        if e > s:
            tier = (r.tier if isinstance(r, Range) else r.get("tier")) or "manual"
            why = (r.why if isinstance(r, Range) else r.get("why")) or ""
            out.append({"start": s, "end": e, "tier": tier, "why": why})
    return sorted(out, key=lambda r: r["start"])


def union_protected(spoilers, duration, manual=None, mode="teaser"):
    """Pass A ranges + the 75% wall + the user's own exclusions, merged.
    In story mode there is no wall and no spoiler map: only what the user
    excluded by hand is off limits."""
    if mode == "story":
        return normalize_ranges(manual or [], duration)
    ranges = normalize_ranges(spoilers.get("protected") if isinstance(spoilers, dict) else
                              getattr(spoilers, "protected", []), duration)
    ranges += normalize_ranges(manual or [], duration)
    wall = wall_seconds(duration)
    ranges.append({"start": wall, "end": float(duration), "tier": "wall",
                   "why": f"final {int((1 - WALL_FRACTION) * 100)}% of runtime"})
    ranges.sort(key=lambda r: r["start"])
    merged = []
    for r in ranges:
        if merged and r["start"] <= merged[-1]["end"] + 0.01:
            prev = merged[-1]
            prev["end"] = max(prev["end"], r["end"])
            if TIERS.index(r["tier"]) < TIERS.index(prev["tier"]) if r["tier"] in TIERS and prev["tier"] in TIERS else False:
                prev["tier"] = r["tier"]
            if r["tier"] == "wall" and prev["tier"] != "wall":
                prev["why"] = (prev["why"] + "; " + r["why"]).strip("; ")
        else:
            merged.append(dict(r))
    return merged


def _hits(start, end, ranges):
    return [r for r in ranges if start < r["end"] and end > r["start"]]


# --- validation -------------------------------------------------------------

def validate_spoilers(data, duration, mode="teaser"):
    """Pass A back from the chat: a spoiler map (teaser) or a story map
    (story). Returns ``(model | None, errors)``."""
    if mode == "story":
        try:
            sm = StoryMap.model_validate(data)
        except ValidationError as exc:
            return None, _schema_errors(exc)
        errors = []
        if len(sm.turning_points) < 3:
            errors.append(_err("turning_points", f"{len(sm.turning_points)} turning points; the whole film needs at least 3"))
        for i, tp in enumerate(sm.turning_points):
            try:
                at = fp.parse_timestamp(tp.at)
                if at > duration + 1:
                    errors.append(_err("turning_points", f"turning point {i} is after the film ends"))
            except fp.SubtitleError as exc:
                errors.append(_err("turning_points", f"turning point {i}: {exc}"))
        if not sm.ending.strip():
            errors.append(_err("ending", "ending is empty; in story mode the ending is told"))
        if not sm.hook_line.strip():
            errors.append(_err("hook", "hook_line is empty; it opens part 1"))
        for m in sm.mood_arc:
            if m not in MOODS:
                errors.append(_err("mood", f'mood "{m}" is not one of {", ".join(MOODS)}'))
        return sm, errors
    try:
        sm = SpoilerMap.model_validate(data)
    except ValidationError as exc:
        return None, _schema_errors(exc)
    errors = []
    for i, r in enumerate(sm.protected):
        try:
            s, e = r.seconds()
        except fp.SubtitleError as exc:
            errors.append(_err("range", f"protected {i}: {exc}"))
            continue
        if e <= s:
            errors.append(_err("range", f"protected {i} ends before it starts"))
        if r.tier and r.tier not in TIERS:
            errors.append(_err("tier", f'protected {i}: tier "{r.tier}" is not one of {", ".join(TIERS)}'))
    if sm.twist_exists and sm.twist_location is None:
        errors.append(_err("twist", "twist_exists is true but twist_location is missing"))
    if not sm.central_question.strip():
        errors.append(_err("question", "central_question is empty"))
    low = sm.central_question.lower()
    if any(p in low for p in ("because", "the answer is", "which is")):
        errors.append(_err("question_answered", "central_question looks like it carries its answer"))
    return sm, errors


STORY_BANNED_TITLE_WORDS = ("explained", "recap", "summary", "part")


def validate_structure(data, duration, protected, mode="teaser"):
    try:
        st = Structure.model_validate(data)
    except ValidationError as exc:
        return None, _schema_errors(exc)
    full = mode == "story"
    errors = []
    if len(st.parts) != 3:
        errors.append(_err("parts", f"{len(st.parts)} parts; the series has three"))
    wall = float(duration) if full else wall_seconds(duration)
    withhelds = {}
    banned_words = STORY_BANNED_TITLE_WORDS if full else BANNED_TITLE_WORDS
    for p in st.parts:
        low = p.title.lower()
        for banned in banned_words:
            if re.search(r"\b" + re.escape(banned) + r"\b", low):
                errors.append(_err("title_banned", f'title "{p.title}" contains "{banned}"', part=p.index))
        required = ("claim", "open_question") if full else ("claim", "open_question", "withheld")
        for field in required:
            if not getattr(p, field).strip():
                what = "cliffhanger (open_question)" if full and field == "open_question" else field
                errors.append(_err("missing", f"{what} is empty", part=p.index))
        try:
            s, e = p.band_seconds()
            if e <= s:
                errors.append(_err("band", "evidence_band ends before it starts", part=p.index))
            if e > wall + 1:
                errors.append(_err("band_wall", f"evidence_band ends after the wall at "
                                   f"{fp.format_timestamp(wall, hours=True)}", part=p.index))
        except fp.SubtitleError as exc:
            errors.append(_err("band", str(exc), part=p.index))
        if full:
            continue  # a story series withholds nothing and its cliffhangers ARE about the plot
        key = re.sub(r"\W+", " ", p.withheld.lower()).strip()
        if key in withhelds:
            errors.append(_err("withheld_dup", f"withheld repeats part {withhelds[key]}: the arc has not been thought about",
                               part=p.index))
        withhelds.setdefault(key, p.index)
        if re.search(r"\bwhat happens next\b", p.open_question.lower()):
            errors.append(_err("question_plot", "open_question is about the plot, not the method", part=p.index))
    if full and len(st.parts) == 3:
        try:
            last_end = st.parts[-1].band_seconds()[1]
            if last_end < float(duration) * 0.9:
                errors.append(_err("band_end", f"part 3's band ends at {fp.format_timestamp(last_end, hours=True)}; "
                                   "in story mode the series tells the ending, so it must reach the film's end", part=3))
        except fp.SubtitleError:
            pass
    return st, errors


def lint_narration(text):
    """Resolution-language hits in one narration string."""
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    return [p for p in RESOLUTION_PHRASES if re.search(r"\b" + re.escape(p) + r"\b", low)]


def lint_craft(text):
    """Lecture-vocabulary hits ("the camera", "notice", "rhyme"...) in one
    narration string: the register the story style exists to avoid."""
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    return [p for p in CRAFT_PHRASES if re.search(r"\b" + re.escape(p) + r"\b", low)]


def validate_plan(data, part, duration, protected, target=PART_TARGET_SECONDS, speed=SPEED,
                  style="story", mode="teaser"):
    """Returns ``(PartPlan | None, errors, warnings)``. Errors block the
    render; warnings (the lint) are for the user to read. In the ``story``
    style, lecture vocabulary is warned per chunk (``craft_language``) and a
    part with it in more than a third of its chunks gets one summary warning."""
    try:
        plan = PartPlan.model_validate(data)
    except ValidationError as exc:
        return None, _schema_errors(exc), []
    part = part if isinstance(part, Part) else Part.model_validate(part)
    errors, warnings = [], []
    craft_chunks = 0
    full = mode == "story"
    if full:
        # Story mode: no wall, spoilers wanted; only the user's own exclusions bind.
        protected = [r for r in protected if r.get("tier") == "manual"]
    if plan.index != part.index:
        errors.append(_err("index", f"plan is for part {plan.index}, expected {part.index}"))
    n = len(plan.chunks)
    if not CHUNKS_RANGE[0] <= n <= CHUNKS_RANGE[1]:
        errors.append(_err("chunk_count", f"{n} chunks; need {CHUNKS_RANGE[0]}-{CHUNKS_RANGE[1]}"))
    wall = float(duration) if full else wall_seconds(duration)
    b_start, b_end = part.band_seconds()
    total = 0.0
    words = 0
    prev_end = -1.0
    lengths = []
    flips = []
    for i, c in enumerate(plan.chunks):
        length = c.end - c.start
        if c.end <= c.start:
            errors.append(_err("range", "ends before it starts", chunk=i))
            continue
        lengths.append(length)
        total += length
        if c.start < 0 or c.end > duration + 0.5:
            errors.append(_err("range", "outside the film", chunk=i))
        if c.end > wall + 1e-6:
            errors.append(_err("wall", f"reaches past the wall at {fp.format_timestamp(wall, hours=True)}", chunk=i))
        for r in _hits(c.start, c.end, protected):
            if r["tier"] == "wall":
                continue
            errors.append(_err("protected", f"intersects protected {r['tier']} range "
                               f"{fp.format_timestamp(r['start'], hours=True)}-"
                               f"{fp.format_timestamp(r['end'], hours=True)}" +
                               (f" ({r['why']})" if r.get("why") else ""), chunk=i))
        if not (b_start - 0.5 <= c.start and c.end <= b_end + 0.5):
            errors.append(_err("band", "outside this part's evidence band", chunk=i))
        if length > CHUNK_SECONDS_RANGE[1] + 0.05:
            errors.append(_err("length", f"{length:.1f} s; a chunk is at most {CHUNK_SECONDS_RANGE[1]:.0f} s", chunk=i))
        if c.start < prev_end:
            errors.append(_err("order", "overlaps or precedes the previous chunk", chunk=i))
        prev_end = c.end
        if not c.narration.strip():
            errors.append(_err("silent", "chunk has no narration; silent footage is a failure", chunk=i))
        words += len(c.narration.split())
        if not full:
            for hit in lint_narration(c.narration):
                warnings.append(_err("resolution_language", f'"{hit}" in narration', chunk=i))
        if style == "story":
            craft = lint_craft(c.narration)
            if craft:
                craft_chunks += 1
                warnings.append(_err("craft_language", "lecture words: " + ", ".join(f'"{h}"' for h in craft[:4])
                                     + ". Tell the moment, do not point at the filmmaking.", chunk=i))
        if c.flip:
            flips.append(i)
            if not (c.flip_reason or "").strip():
                errors.append(_err("flip", "flip: true needs a flip_reason", chunk=i))
    if len(flips) > MAX_FLIPS_PER_PART:
        errors.append(_err("flips", f"{len(flips)} flipped chunks; at most {MAX_FLIPS_PER_PART}. "
                           "Uniform mirroring is not editing."))
    if len(flips) >= 3 and all(b - a == flips[1] - flips[0] for a, b in zip(flips, flips[1:])):
        errors.append(_err("flips", "flips alternate at a fixed interval; that is a pattern, not a decision"))
    if lengths:
        mean = total / len(lengths)
        if not MEAN_CHUNK_RANGE[0] - 0.5 <= mean <= MEAN_CHUNK_RANGE[1] + 0.5:
            errors.append(_err("mean_length", f"mean chunk {mean:.1f} s; want {MEAN_CHUNK_RANGE[0]:.0f}-{MEAN_CHUNK_RANGE[1]:.0f}"))
        budget = footage_budget(plan.target_duration or target, speed)
        if abs(total - budget) > budget * FOOTAGE_TOLERANCE:
            errors.append(_err("footage", f"{total:.1f} s of footage against {budget:.1f} s "
                               f"(±{int(FOOTAGE_TOLERANCE * 100)}%)"))
    if words > WORDS_PER_PART * (1 + WORDS_TOLERANCE):
        errors.append(_err("words", f"{words} narration words; budget {WORDS_PER_PART} (+{int(WORDS_TOLERANCE * 100)}%)"))
    if words and words < WORDS_PER_PART * 0.6:
        warnings.append(_err("words_low", f"{words} narration words; the part will run short of {plan.target_duration:.0f} s"))
    if style == "story" and plan.chunks and craft_chunks * 3 > len(plan.chunks):
        warnings.append(_err("craft_language", f"{craft_chunks} of {len(plan.chunks)} chunks talk about the "
                             "filmmaking instead of the moment. Ask the model to rewrite the narration as a "
                             f"story in the part's mood ({part.mood or 'tense'}), no camera, no 'notice'."))
    check = plan.spoiler_self_check.strip().lower().rstrip(".!")
    # "Nothing." is the failure; "Nothing about who is behind the door" is
    # the answer the prompt asked for. Story mode withholds nothing.
    if not full and (check in NOTHING_WORDS or (check.startswith("nothing") and len(check.split()) <= 2)):
        errors.append(_err("self_check", "spoiler_self_check is empty or 'nothing': the part is wrong"))
    if not full:
        for line in plan.closing_lines:
            for hit in lint_narration(line):
                warnings.append(_err("resolution_language", f'"{hit}" in a closing line'))
    return plan, errors, warnings


def format_errors(items):
    lines = []
    for e in items:
        where = f"part {e['part']}: " if "part" in e else (f"chunk {e['chunk'] + 1}: " if "chunk" in e else "")
        lines.append(f"- {where}{e['message']}")
    return "\n".join(lines)


# --- budget -----------------------------------------------------------------

def budget(plans, structure, duration, protected, speed=SPEED):
    """The pre-publish checklist numbers. ``plans``: {part_index: PartPlan}."""
    chunks = [c for p in plans.values() for c in (p.chunks if isinstance(p, PartPlan) else PartPlan.model_validate(p).chunks)]
    lengths = [c.end - c.start for c in chunks if c.end > c.start]
    footage = sum(lengths)
    words = sum(len(c.narration.split()) for c in chunks)
    silent = sum(l for c, l in zip(chunks, lengths) if not c.narration.strip())
    wall = wall_seconds(duration)
    in_protected = sum(1 for c in chunks for r in _hits(c.start, c.end, protected) if r["tier"] != "wall")
    past_wall = sum(1 for c in chunks if c.end > wall + 1e-6)
    hits = sum(len(lint_narration(c.narration)) for c in chunks)
    withhelds = [p.withheld.strip().lower() for p in (structure.parts if isinstance(structure, Structure) else [])]
    return {
        "footage_seconds": round(footage, 1),
        "footage_share": round(footage / duration, 4) if duration else 0.0,
        "longest_chunk": round(max(lengths), 1) if lengths else 0.0,
        "mean_chunk": round(footage / len(lengths), 2) if lengths else 0.0,
        "words": words,
        "words_per_footage_second": round(words / footage, 2) if footage else 0.0,
        "silent_share": round(silent / footage, 4) if footage else 0.0,
        "chunks_in_protected": in_protected,
        "chunks_past_wall": past_wall,
        "resolution_hits": hits,
        "withheld_distinct": len(set(withhelds)) == len(withhelds) and all(withhelds),
        "parts_planned": sorted(plans.keys()),
        "targets": {"footage_seconds": "< 600", "footage_share": "< 0.10", "longest_chunk": "< 15",
                    "mean_chunk": "6-13", "words_per_footage_second": "> 2.5", "silent_share": "< 0.05",
                    "chunks_in_protected": 0, "chunks_past_wall": 0},
        "ok": (footage < 600 and (not duration or footage / duration < 0.10) and in_protected == 0
               and past_wall == 0 and (not footage or silent / footage < 0.05)
               and (not footage or words / footage > 2.5)),
    }


# --- voiceover fit ----------------------------------------------------------

def compilation_plan(plan, protected, duration, speed=SPEED, room_seconds=3.0):
    """The chunk plan in the shape ``compilation.py`` fits a recorded
    voiceover to. Times are FINISHED-equivalent (source / speed): compilation
    compares a chunk's length with its narration's measured duration, and the
    picture runs at ``speed``. ``film_api`` multiplies back to source seconds
    after ``fit_shots``. Head/tail room stops at the neighbouring chunk and at
    any protected range, so a fit can never grow a chunk into the ending.
    """
    plan = plan if isinstance(plan, PartPlan) else PartPlan.model_validate(plan)
    shots, lines = [], []
    chunks = plan.chunks
    for i, c in enumerate(chunks):
        prev_end = chunks[i - 1].end if i > 0 else 0.0
        next_start = chunks[i + 1].start if i + 1 < len(chunks) else float(duration)
        head = min(room_seconds, c.start - prev_end)
        tail = min(room_seconds, next_start - c.end)
        for r in protected:
            if r["start"] >= c.end:
                tail = min(tail, r["start"] - c.end)
            if r["end"] <= c.start:
                head = min(head, c.start - r["end"])
        n = i + 1
        shots.append({"n": n, "src_in": c.start / speed, "src_out": c.end / speed,
                      "planned_duration": (c.end - c.start) / speed,
                      "head_room": max(0.0, head) / speed, "tail_room": max(0.0, tail) / speed,
                      "description": c.shows, "keep_audio": c.keep_audio, "flip": c.flip})
        wordsn = c.narration.split()
        lines.append({"n": n, "shot_ref": n, "silent": not wordsn,
                      "actual_words": len(wordsn), "first_words": " ".join(wordsn[:3]),
                      "text": c.narration})
    return {"sequence": {"width": 1080, "height": 1920},
            "padding": {"default_lead_in": 0.15, "default_tail": 0.25},
            "shots": shots, "vo": {"lines": lines, "edge_guard": 0.1}}


def fitted_to_source(fitted, speed=SPEED):
    """``compilation.fit_shots`` output -> source segments + VO placement."""
    segments = []
    for s in fitted:
        segments.append({
            "start": round(s["src_in"] * speed, 3), "end": round(s["src_out"] * speed, 3),
            "shot_start": s["shot_start"], "shot_length": s["shot_length"],
            "voice_start": s["voice_start"], "silent": s["silent"],
            "keep_audio": bool(s.get("keep_audio")), "flip": bool(s.get("flip")),
            "chunk": s.get("chunk"),
        })
    return segments


def vo_duration_check(measured, target=PART_TARGET_SECONDS, tolerance=0.10):
    """Within ±10% the visuals are re-cut to the recording; outside, refuse."""
    ratio = measured / float(target) if target else 0.0
    ok = abs(ratio - 1.0) <= tolerance
    return {"ok": ok, "measured": round(measured, 2), "target": float(target),
            "ratio": round(ratio, 3),
            "message": None if ok else (
                f"voiceover runs {measured:.0f} s against a {target:.0f} s part "
                f"({(ratio - 1) * 100:+.0f}%); re-record or re-prompt rather than time-stretch")}
