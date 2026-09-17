"""Movie Shorts, second design: a montage over the film through the clip maker.

The reference this is built to (measured 18-sep-2026 on a 2-minute short cut
from *The Spy Next Door*, docs/film-modules-plan.md): seven scenes from a
94-minute film in a NEW order, one gag each, every silence and reaction
between lines removed, a music bed that never stops, one caption line at
frame centre, and the last line a twist. 76 shots in 120 s, but the SOURCE
runs the editor kept were 5-8 s of continuous dialogue, trimmed between lines
rather than inside them.

Nothing here calls an LLM and nothing here renders. The chat is handed the
film's subtitle lines with ids, answers with 3-4 shorts as ordered lists of
line ids, and this module turns each short into an EDL (``segments`` for
``recut.perform_recut``): a segment per run of consecutive lines, with a
little lead and tail, split where a run would outlast ``MAX_RUN_SECONDS``.
"Skipping the silences" is therefore not a feature, it is what an EDL built
from subtitle cues does by construction; the one thing the SRT cannot see is
a physical gag after the last line (the cat leaps, the toast catches fire),
so a scene may ask for ``hold_after`` seconds of picture past its last line.

The removed crop-ladder Movie Shorts (commit 0c58b66) fought the film's own
cuts with beat-locked per-shot crops. This keeps the film's cuts and adds
only the layers every clip already gets: reframe, look, music duck, overlays,
captions.
"""

from __future__ import annotations

import re
from typing import List, Union

from pydantic import BaseModel, Field, ValidationError, field_validator

import film_prep as fp

# --- knobs --------------------------------------------------------------------

SHORTS_RANGE = (2, 5)                # shorts per film the chat may propose
SCENES_RANGE = (3, 12)               # scenes per short
LINES_PER_SCENE_MAX = 30
TOTAL_SECONDS_RANGE = (45.0, 180.0)  # rendered length after trimming
TARGET_SECONDS = 110.0
LEAD_SECONDS = 0.25                  # picture before the first word of a run
TAIL_SECONDS = 0.35                  # picture after the last word of a run
MERGE_GAP_SECONDS = 0.6              # lines closer than this stay in one run
MAX_RUN_SECONDS = 8.0                # a continuous source run is split past this
HOLD_AFTER_MAX = 6.0
EXTRA_RANGE_MAX = 12.0               # a music-only picture range the chat names by time
MIN_SEGMENT_SECONDS = 0.5
MIN_CUT_GAP_SECONDS = 0.5            # two cuts closer than this on the source are one cut
CAPTION_WORDS_MAX = 9                # a line longer than this will wrap; warn

MOODS = ("funny", "tense", "epic", "heartfelt", "eerie", "driving", "romantic")

# Track picking is by filename keywords: the library is what the user dropped
# into assets/music, so names are all there is to go on. First hit wins, the
# first track alphabetically is the fallback.
MOOD_TRACK_WORDS = {
    "funny": ("upbeat", "fun", "quirky", "comedy", "happy", "bounce", "ukulele"),
    "driving": ("percussion", "fast", "dynamic", "drive", "beat", "energy"),
    "epic": ("epic", "force", "trailer", "power", "orchestra"),
    "tense": ("shadow", "dark", "tension", "cinematic", "suspense", "thriller"),
    "eerie": ("eerie", "horror", "creep", "ambient", "shadow", "dark"),
    "heartfelt": ("emotional", "piano", "soft", "warm", "hope", "inspir"),
    "romantic": ("romantic", "love", "piano", "soft", "acoustic"),
}

# --- the line index the chat sees ------------------------------------------------

def line_index(cues):
    """Every cue with a stable id: ``[{id, start, end, text}]``. Ids are the
    cue's position in the (offset-shifted) subtitle file, so a corrected
    offset keeps every id the chat already used."""
    out = []
    for i, c in enumerate(cues):
        text = " ".join(str(c.get("text") or "").split())
        if not text:
            continue
        out.append({"id": i, "start": float(c["start"]), "end": float(c["end"]), "text": text})
    return out


def lines_text(lines, block_seconds=fp.DIGEST_BLOCK_SECONDS):
    """``#id MM:SS text`` lines under ``[HH:MM:SS-HH:MM:SS]`` block headers,
    like film_prep.digest but with ids and without merging: the chat has to
    name exact lines, so nothing is joined or dropped."""
    rows = []
    block = None
    for ln in lines:
        b = int(ln["start"] // block_seconds)
        if b != block:
            block = b
            b0 = b * block_seconds
            rows.append(f"[{fp.format_timestamp(b0, hours=True)}-{fp.format_timestamp(b0 + block_seconds, hours=True)}]")
        rows.append(f"#{ln['id']} {fp.format_timestamp(ln['start'])} {ln['text']}")
    return "\n".join(rows)


PROMPT_SHORTS = """You are picking scenes from a feature film for {COUNT} short vertical videos
(60-150 seconds each) for a movie channel. Each short is a MONTAGE: 4-9 scenes
from anywhere in the film, in the order that tells the best mini-story, one
gag or one beat per scene, ending on a line that lands (a twist, a punchline,
a cliffhanger). Spoilers are fine. The viewer who loves it goes to the film.

The reference short this channel copies: "How a top spy babysat 3 kids". Seven
scenes from a 94-minute film in a NEW order (the breakfast at 44:40 came
first, the cat on the roof at 12:02 third), each scene one gag, every pause
between lines removed, and the last line a betrayal that the viewer did not
see coming. Two minutes, no narration, the film's own dialogue as captions.

Film: {TITLE} ({RUNTIME}).

Below are the film's subtitle lines, each with an id (#123) and its time.
You pick LINES, never times: the editor cuts a little before the first word
and a little after the last word of each run of lines you keep, and drops
everything between runs. So a scene is the list of line ids to KEEP, in
time order. Skip reactions, filler and any line the caption cannot carry
(over {CAP_WORDS} words wraps; prefer lines under that).

RULES
- {COUNT} shorts, {SC_MIN}-{SC_MAX} scenes each, {T_MIN:.0f}-{T_MAX:.0f} s of kept footage per short
  (each kept line costs roughly its own duration plus a second). Aim near {TARGET:.0f} s.
- Every short has a PREMISE its title states in plain words ("How a top spy
  babysat 3 kids", "The worst first date in cinema"), not the film's title,
  not "part 1", not "explained". The scenes prove the premise.
- Scenes may come from anywhere and in any order; a scene must not reuse a
  line another scene or another short used.
- The LAST scene of each short ends on the line that lands hardest. Say why
  in ``ending``.
- When the joke or the beat is in the PICTURE after the last line (a fall,
  a leap, a look), set ``hold_after`` (seconds of picture to keep, max {HOLD_MAX:.0f})
  on that scene, else 0.
- A gap of 8-30 s between two lines in a comedy or an action film usually
  hides the physical gag or the stunt (nobody talks while the toast burns).
  You may keep part of such a gap as picture with music and no caption:
  ``extra: [{{"start": "44:41", "end": "44:47"}}]`` on the scene, each range
  at most {EXTRA_MAX:.0f} s, placed between the lines it belongs to. The reference
  short is 40% such footage. Use it only where the surrounding lines make
  the action certain; the editor sees the picture, you do not.
- ``mood`` is one of: {MOODS}. It picks the music bed.
- ``hook`` is the first caption the viewer reads before any dialogue: 3-8
  words, a question or a dare, no film title.

Return ONLY this JSON:
{{
  "shorts": [
    {{
      "title": "How a top spy babysat 3 kids",
      "hook": "He fought dictators. Then came breakfast.",
      "mood": "funny",
      "ending": "the ally he trusted pulls a gun; the short ends on 'Good work'",
      "scenes": [
        {{"lines": [412, 413], "hold_after": 2.0, "extra": [{{"start": "44:41", "end": "44:47"}}], "note": "breakfast, toast catches fire"}},
        {{"lines": [388, 389, 391], "hold_after": 0, "extra": [], "note": "bathroom door"}}
      ]
    }}
  ]
}}

SUBTITLE LINES
{LINES}
"""


def build_prompt(cues, duration, title, count=3):
    lines = line_index(cues)
    count = max(SHORTS_RANGE[0], min(SHORTS_RANGE[1], int(count) if count is not None else 3))
    return PROMPT_SHORTS.format(
        COUNT=count, TITLE=title or "the film", RUNTIME=fp.format_timestamp(duration, hours=True),
        CAP_WORDS=CAPTION_WORDS_MAX, SC_MIN=SCENES_RANGE[0], SC_MAX=SCENES_RANGE[1],
        T_MIN=TOTAL_SECONDS_RANGE[0], T_MAX=TOTAL_SECONDS_RANGE[1], TARGET=TARGET_SECONDS,
        HOLD_MAX=HOLD_AFTER_MAX, EXTRA_MAX=EXTRA_RANGE_MAX, MOODS=", ".join(MOODS),
        LINES=lines_text(lines))


# --- models -----------------------------------------------------------------------

class ExtraRange(BaseModel):
    """A picture-only range named by time (``MM:SS`` or seconds): the gag
    between two lines that the subtitle file cannot see."""
    start: Union[str, float]
    end: Union[str, float]

    def seconds(self):
        return fp.parse_timestamp(str(self.start)), fp.parse_timestamp(str(self.end))


class Scene(BaseModel):
    lines: List[int] = Field(min_length=1, max_length=LINES_PER_SCENE_MAX)
    hold_after: float = 0.0
    extra: List[ExtraRange] = Field(default_factory=list, max_length=4)
    note: str = ""

    @field_validator("hold_after")
    @classmethod
    def _clamp_hold(cls, v):
        return round(min(HOLD_AFTER_MAX, max(0.0, float(v or 0.0))), 2)


class Short(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    hook: str = ""
    mood: str = "funny"
    ending: str = ""
    scenes: List[Scene] = Field(min_length=1, max_length=SCENES_RANGE[1] + 6)

    @field_validator("mood")
    @classmethod
    def _mood(cls, v):
        v = str(v or "").strip().lower()
        return v if v in MOODS else "funny"


class ShortsPlan(BaseModel):
    shorts: List[Short] = Field(min_length=1, max_length=SHORTS_RANGE[1] + 2)


# --- cue -> EDL -------------------------------------------------------------------

def _err(code, message, **where):
    out = {"code": code, "message": message}
    out.update(where)
    return out


def scene_segments(scene, by_id, duration, *, lead=LEAD_SECONDS, tail=TAIL_SECONDS,
                   merge_gap=MERGE_GAP_SECONDS, max_run=MAX_RUN_SECONDS):
    """One scene's lines -> ordered ``[{start, end}]`` on the film.

    Lines are sorted by time and joined into runs while the gap between them
    is under ``merge_gap``; each run gets ``lead`` before and ``tail`` after
    and ``hold_after`` extends the last one; ``extra`` picture ranges are
    slotted in by time. Two cuts closer than ``MIN_CUT_GAP_SECONDS`` on the
    source become one (a 0.3 s jump cut is a stutter, not a trim). A run
    longer than ``max_run`` is NOT split here: a cut that removes no frames
    leaves the source stretch continuous, which is what fingerprinting
    measures, so the run is reported instead and the chat drops a line to
    break it. Returns ``(segments, long_runs)``, the latter the lengths of
    the runs over ``max_run``."""
    lines = sorted((by_id[i] for i in scene.lines if i in by_id), key=lambda ln: ln["start"])
    warnings = []
    if not lines:
        return [], warnings
    runs = [[lines[0]]]
    for ln in lines[1:]:
        if ln["start"] - runs[-1][-1]["end"] <= merge_gap:
            runs[-1].append(ln)
        else:
            runs.append([ln])

    segments = []
    for n, run in enumerate(runs):
        start = max(0.0, run[0]["start"] - lead)
        end = min(float(duration), run[-1]["end"] + tail)
        if n == len(runs) - 1 and scene.hold_after:
            end = min(float(duration), end + float(scene.hold_after))
        if end - start >= MIN_SEGMENT_SECONDS:
            segments.append({"start": round(start, 3), "end": round(end, 3)})
        if run[-1]["end"] - run[0]["start"] > max_run:
            warnings.append(round(end - start, 1))
    for rng in scene.extra:
        try:
            s, e = rng.seconds()
        except (fp.SubtitleError, ValueError):
            continue
        s, e = max(0.0, s), min(float(duration), e, s + EXTRA_RANGE_MAX)
        if e - s >= MIN_SEGMENT_SECONDS:
            segments.append({"start": round(s, 3), "end": round(e, 3)})
    segments.sort(key=lambda x: x["start"])
    merged = []
    for s in segments:
        if merged and s["start"] <= merged[-1]["end"] + MIN_CUT_GAP_SECONDS:
            merged[-1]["end"] = max(merged[-1]["end"], s["end"])
        else:
            merged.append(dict(s))
    return merged, warnings


def short_segments(short, by_id, duration, **kw):
    """Every scene of a short, in the short's order. Returns
    ``(segments, per_scene)`` where per_scene is ``[{index, segments,
    seconds, lines, long_runs}]`` for the UI's scene table."""
    all_segments, per_scene = [], []
    for k, scene in enumerate(short.scenes):
        segs, long_runs = scene_segments(scene, by_id, duration, **kw)
        secs = round(sum(s["end"] - s["start"] for s in segs), 2)
        per_scene.append({"index": k, "segments": segs, "seconds": secs, "lines": list(scene.lines),
                          "long_runs": long_runs, "note": scene.note, "hold_after": scene.hold_after,
                          "text": " / ".join(by_id[i]["text"] for i in scene.lines if i in by_id)})
        all_segments.extend(segs)
    return all_segments, per_scene


def total_seconds(segments):
    return round(sum(s["end"] - s["start"] for s in segments), 2)


# --- validation -------------------------------------------------------------------

def validate_shorts(data, cues, duration, *, total_range=TOTAL_SECONDS_RANGE, scenes_range=SCENES_RANGE):
    """``(plan, errors, warnings, previews)``. ``plan`` is None on schema
    errors (nothing to render). Rule errors can be forced by the caller;
    warnings are review items. ``previews`` has each short's segments,
    per-scene table and total, computed here so the API and the UI agree."""
    try:
        plan = ShortsPlan.model_validate(data)
    except ValidationError as exc:
        return None, [_err("schema", f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}")
                      for e in exc.errors()], [], []
    by_id = {ln["id"]: ln for ln in line_index(cues)}
    errors, warnings, previews = [], [], []
    used = {}
    for si, short in enumerate(plan.shorts):
        if not (scenes_range[0] <= len(short.scenes) <= scenes_range[1]):
            errors.append(_err("scene_count", f"{len(short.scenes)} scenes; wanted {scenes_range[0]}-{scenes_range[1]}", short=si))
        title_l = short.title.lower()
        if re.search(r"\b(part\s*\d|explained|recap|summary|ending explained)\b", title_l):
            errors.append(_err("title", "the title states a premise in plain words, not 'part N' / 'explained' / 'recap'", short=si))
        for k, scene in enumerate(short.scenes):
            for i in scene.lines:
                if i not in by_id:
                    errors.append(_err("unknown_line", f"line #{i} is not in the subtitle file", short=si, scene=k))
                elif i in used and used[i] != (si, k):
                    o = used[i]
                    errors.append(_err("reused_line", f"line #{i} is already in short {o[0] + 1} scene {o[1] + 1}", short=si, scene=k))
                else:
                    used[i] = (si, k)
            for i in scene.lines:
                if i in by_id and len(by_id[i]["text"].split()) > CAPTION_WORDS_MAX:
                    warnings.append(_err("long_line", f"line #{i} has {len(by_id[i]['text'].split())} words; the caption will wrap", short=si, scene=k))
            for rng in scene.extra:
                try:
                    s, e = rng.seconds()
                except (fp.SubtitleError, ValueError):
                    errors.append(_err("extra_range", f"extra range {rng.start!r}-{rng.end!r} is not a time", short=si, scene=k))
                    continue
                if e <= s or s < 0 or e > float(duration):
                    errors.append(_err("extra_range", f"extra range {rng.start}-{rng.end} is empty or outside the film", short=si, scene=k))
                elif e - s > EXTRA_RANGE_MAX:
                    warnings.append(_err("extra_long", f"extra range {rng.start}-{rng.end} is {e - s:.0f} s; cut to {EXTRA_RANGE_MAX:.0f}", short=si, scene=k))
        segments, per_scene = short_segments(short, by_id, duration)
        total = total_seconds(segments)
        if segments and not (total_range[0] <= total <= total_range[1]):
            errors.append(_err("total", f"{total:.0f} s of footage; wanted {total_range[0]:.0f}-{total_range[1]:.0f}", short=si))
        elif not segments:
            errors.append(_err("empty", "no usable lines", short=si))
        for sc in per_scene:
            for secs in sc["long_runs"]:
                warnings.append(_err("long_run", f"a {secs} s continuous run; over {MAX_RUN_SECONDS:.0f} s of one stretch is what fingerprinting catches", short=si, scene=sc["index"]))
        previews.append({"index": si, "title": short.title, "hook": short.hook, "mood": short.mood,
                         "ending": short.ending, "segments": segments, "scenes": per_scene,
                         "seconds": total, "cuts": len(segments)})
    return plan, errors, warnings, previews


def format_errors(items):
    """The correction turn: one line per item, with where it is."""
    rows = []
    for e in items:
        where = []
        if "short" in e:
            where.append(f"short {e['short'] + 1}")
        if "scene" in e:
            where.append(f"scene {e['scene'] + 1}")
        rows.append((" ".join(where) + ": " if where else "") + e["message"])
    return "\n".join(rows)


# --- music -------------------------------------------------------------------------

def pick_track(mood, tracks):
    """A library track (``{file, name}``) for a mood by filename keywords, or
    the first track, or None when the library is empty."""
    if not tracks:
        return None
    words = MOOD_TRACK_WORDS.get(mood, ())
    for w in words:
        for t in tracks:
            if w in str(t.get("file", "")).lower() or w in str(t.get("name", "")).lower():
                return t
    return tracks[0]


def caption_style(preset="film_pop"):
    """The one-line centred caption the reference uses; the user restyles
    from the clip card afterwards."""
    return {"preset": preset, "overrides": {"position": "center", "max_lines": 1, "max_chars": 24,
                                            "uppercase": True, "font_size": 64}}


def watermark(text):
    """A small text overlay at the bottom centre, ``overlays.normalize`` shape."""
    text = " ".join(str(text or "").split())[:60]
    if not text:
        return []
    return [{"type": "text", "text": text, "x": 0.5, "y": 0.93, "w": 0.6, "size": 0.022,
             "font_family": "Roboto", "color": "#FFFFFF", "bold": True, "uppercase": True,
             "outline": 2.0, "outline_color": "#000000", "opacity": 0.85}]
