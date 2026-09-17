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
      "cannot_deliver": "What only the film itself can give the viewer here."
    }}
  ]
}}

Titles must not contain: Setup, Beginning, Middle, End, Ending, Finale,
Explained, Recap, Summary, Full Story.
Every evidence_band must end at or before {WALL}.

SUBTITLE DIGEST (up to the wall):
{DIGEST}
"""

PROMPT_C = """You are planning the clips and narration for Part {N} of three: "{TITLE}".

This part argues: {CLAIM}
It must end leaving the viewer with: {OPEN_QUESTION}
It must NOT reveal: {WITHHELD}
Only the film can give the viewer: {CANNOT_DELIVER}

BUDGET - these are hard limits, not targets:
- Finished runtime {TARGET} s at {SPEED}x playback -> {FOOTAGE_BUDGET} s of footage
- {CHUNKS_MIN}-{CHUNKS_MAX} chunks, each {CHUNK_MIN}-{CHUNK_MAX} s (never longer than {CHUNK_MAX})
- ~{WORDS} narration words total, ~{WORDS_PER_CHUNK} per chunk
- Every chunk must have narration. Silent footage is a failure.
- Draw only from {EVIDENCE_BAND}
- Never from: {PROTECTED_RANGES}
- Nothing after {WALL}
- Times are seconds from the start of the film.

CLIP SELECTION RULES:
- Prefer the moment BEFORE a beat: the reaction without the cause, the
  establishing wide, the breath before the line.
- Cut out of scenes early. Leaving the viewer hanging is the point.
- Never use the film's best shot or best line. Use the second-best.
- No chunk may show an outcome, a resolution, or a consequence landing.

NARRATION RULES:
- Analysis, context, criticism. Never plot recitation.
- Apply this test to every sentence: could it substitute for watching the
  scene? If yes, rewrite it.
- Banned constructions: "then he/she", "turns out", "finally", "ends with",
  "we learn that", "it is revealed", "in the end", "the twist is".
- Address the viewer directly. Tell them what to watch for.
- The last 2-3 lines open the question above. Do not answer it.

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


def build_prompt_a(cues, duration):
    return PROMPT_A.format(RUNTIME=fp.format_timestamp(duration, hours=True),
                           DIGEST=fp.digest(cues))


def build_prompt_b(cues, duration, spoilers, protected, target=PART_TARGET_SECONDS):
    """``spoilers``: validated pass-A dict; ``protected``: the unioned ranges."""
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


def build_prompt_c(cues, duration, part, protected, target=PART_TARGET_SECONDS, speed=SPEED):
    """``part``: one validated pass-B part (``Part`` or dict)."""
    part = part if isinstance(part, Part) else Part.model_validate(part)
    b_start, b_end = part.band_seconds()
    wall = wall_seconds(duration)
    b_end = min(b_end, wall)
    return PROMPT_C.format(
        N=part.index, TITLE=part.title, CLAIM=part.claim,
        OPEN_QUESTION=part.open_question, WITHHELD=part.withheld,
        CANNOT_DELIVER=part.cannot_deliver or "the experience of not knowing",
        TARGET=int(target), SPEED=speed, FOOTAGE_BUDGET=footage_budget(target, speed),
        CHUNKS_MIN=CHUNKS_RANGE[0], CHUNKS_MAX=CHUNKS_RANGE[1],
        CHUNK_MIN=int(CHUNK_SECONDS_RANGE[0]), CHUNK_MAX=int(CHUNK_SECONDS_RANGE[1]),
        WORDS=WORDS_PER_PART, WORDS_PER_CHUNK=round(WORDS_PER_PART / 18),
        EVIDENCE_BAND=f"{fp.format_timestamp(b_start, hours=True)}-{fp.format_timestamp(b_end, hours=True)}",
        PROTECTED_RANGES=_ranges_text(protected),
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


class Part(BaseModel):
    index: int
    title: str
    claim: str
    evidence_band: Range
    open_question: str
    withheld: str
    cannot_deliver: str = ""

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


def union_protected(spoilers, duration, manual=None):
    """Pass A ranges + the 75% wall + the user's own exclusions, merged."""
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

def validate_spoilers(data, duration):
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


def validate_structure(data, duration, protected):
    try:
        st = Structure.model_validate(data)
    except ValidationError as exc:
        return None, _schema_errors(exc)
    errors = []
    if len(st.parts) != 3:
        errors.append(_err("parts", f"{len(st.parts)} parts; the series has three"))
    wall = wall_seconds(duration)
    withhelds = {}
    for p in st.parts:
        low = p.title.lower()
        for banned in BANNED_TITLE_WORDS:
            if re.search(r"\b" + re.escape(banned) + r"\b", low):
                errors.append(_err("title_banned", f'title "{p.title}" contains "{banned}"', part=p.index))
        for field in ("claim", "open_question", "withheld"):
            if not getattr(p, field).strip():
                errors.append(_err("missing", f"{field} is empty", part=p.index))
        try:
            s, e = p.band_seconds()
            if e <= s:
                errors.append(_err("band", "evidence_band ends before it starts", part=p.index))
            if e > wall + 1:
                errors.append(_err("band_wall", f"evidence_band ends after the wall at "
                                   f"{fp.format_timestamp(wall, hours=True)}", part=p.index))
        except fp.SubtitleError as exc:
            errors.append(_err("band", str(exc), part=p.index))
        key = re.sub(r"\W+", " ", p.withheld.lower()).strip()
        if key in withhelds:
            errors.append(_err("withheld_dup", f"withheld repeats part {withhelds[key]}: the arc has not been thought about",
                               part=p.index))
        withhelds.setdefault(key, p.index)
        if re.search(r"\bwhat happens next\b", p.open_question.lower()):
            errors.append(_err("question_plot", "open_question is about the plot, not the method", part=p.index))
    return st, errors


def lint_narration(text):
    """Resolution-language hits in one narration string."""
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    return [p for p in RESOLUTION_PHRASES if re.search(r"\b" + re.escape(p) + r"\b", low)]


def validate_plan(data, part, duration, protected, target=PART_TARGET_SECONDS, speed=SPEED):
    """Returns ``(PartPlan | None, errors, warnings)``. Errors block the
    render; warnings (the lint) are for the user to read."""
    try:
        plan = PartPlan.model_validate(data)
    except ValidationError as exc:
        return None, _schema_errors(exc), []
    part = part if isinstance(part, Part) else Part.model_validate(part)
    errors, warnings = [], []
    if plan.index != part.index:
        errors.append(_err("index", f"plan is for part {plan.index}, expected {part.index}"))
    n = len(plan.chunks)
    if not CHUNKS_RANGE[0] <= n <= CHUNKS_RANGE[1]:
        errors.append(_err("chunk_count", f"{n} chunks; need {CHUNKS_RANGE[0]}-{CHUNKS_RANGE[1]}"))
    wall = wall_seconds(duration)
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
        for hit in lint_narration(c.narration):
            warnings.append(_err("resolution_language", f'"{hit}" in narration', chunk=i))
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
    check = plan.spoiler_self_check.strip().lower().rstrip(".!")
    # "Nothing." is the failure; "Nothing about who is behind the door" is
    # the answer the prompt asked for.
    if check in NOTHING_WORDS or (check.startswith("nothing") and len(check.split()) <= 2):
        errors.append(_err("self_check", "spoiler_self_check is empty or 'nothing': the part is wrong"))
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
