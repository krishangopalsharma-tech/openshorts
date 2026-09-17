"""movieshorts: the hook and beat prompts, reading a chat answer back, and the
validator whose one indispensable rule is that captions must be real cues."""

import json

import pytest

import film_prep as fp
import movieshorts as ms

DURATION = 7200.0
LINES = [
    "Where are we now", "Boston Harbor, that way", "Pull yourself together Teddy",
    "I never had any children", "Your daughter, her name was Rachel",
    "You will never leave this island alive", "Not you, not your partner",
    "Which would be worse, to live as a monster", "Or to die as a good man",
    "The rules are simple here", "Nobody escapes the lighthouse",
]


def _cues():
    """A synthetic film: one 3 s cue every 20 s, text cycling through LINES."""
    cues = []
    t = 5.0
    i = 0
    while t < DURATION - 10:
        cues.append({"start": t, "end": t + 3.0, "text": LINES[i % len(LINES)] + f" {i}"})
        t += 20.0
        i += 1
    return cues


CUES = _cues()

HOOK = {
    "index": 1,
    "title": "He has a dual personality",
    "angle": "The film shows two men and lets you believe they are one.",
    "payoff": "Teddy and the patient are the same person.",
    "cold_open": "Teddy on the ferry, 00:00:05",
    "cold_open_at": "00:00:05",
    "spans": [{"start": "00:00:00", "end": "00:10:00"}, {"start": "01:00:00", "end": "01:10:00"}],
    "mood": "tense",
    "strength": 1,
}


def _beats(n=16, length=9.375, key_every=6):
    """A valid plan: n beats spaced along the first span, each holding one
    cue and quoting it exactly."""
    beats = []
    for i in range(n):
        cue = CUES[i]  # cues at 5, 25, 45 ... each 3 s long
        start = cue["start"] - 1.0
        beats.append({
            "start": start, "end": round(start + length, 3),
            "why": "evidence",
            "captions": [{"at": cue["start"], "text": cue["text"], "emphasis": None}],
            "weight": "key" if key_every and i % key_every == 1 else "normal",
            "composite_ok": True,
        })
    return {"hook_index": 1, "beats": beats}


# --- prompts ----------------------------------------------------------------

def test_hooks_prompt_carries_budget_and_digest():
    text = ms.build_hooks_prompt(CUES, DURATION, hook_count=6)
    assert "Return 6 hooks" in text
    assert "Runtime: 02:00:00" in text
    assert "[00:00:00-00:02:00]" in text
    assert "Boston Harbor" in text
    assert "{DIGEST}" not in text and "{{" not in text


def test_hooks_prompt_clamps_count():
    assert "Return 12 hooks" in ms.build_hooks_prompt(CUES, DURATION, hook_count=40)
    assert "Return 3 hooks" in ms.build_hooks_prompt(CUES, DURATION, hook_count=0)


def test_beats_prompt_uses_only_the_hook_spans():
    text = ms.build_beats_prompt(CUES, HOOK)
    assert 'titled "He has a dual personality"' in text
    assert "total source footage 150.0 seconds" in text
    assert "00:00:00-00:10:00, 01:00:00-01:10:00" in text
    assert "(at 00:00:05)" in text
    # Cue 40 sits at 805 s: outside both spans, so not in the digest.
    assert " 40\n" not in text and "13:25" not in text
    # Cue 2 (45 s) is inside.
    assert "00:45 " in text


def test_footage_budget():
    assert ms.footage_budget(120, 1.25) == 150.0
    assert ms.footage_budget(90, 1.0) == 90.0


# --- parse_json -------------------------------------------------------------

def test_parse_json_tolerates_fences_and_prose():
    answer = 'Sure! Here you go:\n```json\n{"a": 1, "b": [2]}\n```\nLet me know.'
    assert ms.parse_json(answer) == {"a": 1, "b": [2]}


def test_parse_json_reports_bad_json():
    with pytest.raises(ms.PlanError):
        ms.parse_json("{not json")
    with pytest.raises(ms.PlanError):
        ms.parse_json("no braces here")


# --- hooks validation -------------------------------------------------------

def test_validate_hooks_accepts_a_good_plan():
    plan, errors = ms.validate_hooks({"film": {"premise": "x"}, "hooks": [HOOK]}, DURATION)
    assert errors == []
    assert plan.hooks[0].cold_open_seconds() == 5.0


def test_validate_hooks_catches_each_rule():
    bad = dict(HOOK)
    bad.update({
        "title": "The Ending Explained In Full Detail For Everyone Watching Today",
        "mood": "jazzy",
        "spans": [{"start": "00:00:00", "end": "00:05:00"}],
        "cold_open": "somewhere",
        "cold_open_at": None,
    })
    dup = dict(HOOK, index=2, title="No one dares to provoke him")
    other = dict(HOOK, index=3, title="She knew from the first night",
                 payoff="Different payoff entirely.")
    plan, errors = ms.validate_hooks({"hooks": [bad, dup, other]}, DURATION)
    codes = {e["code"] for e in errors}
    assert {"title_length", "title_banned", "mood", "spans_short", "cold_open", "payoff_dup"} <= codes
    # Every error names its hook so the correction can be pasted back.
    assert all("hook" in e for e in errors)
    text = ms.format_errors(errors)
    assert "hook 1:" in text and "hook 2:" in text


def test_validate_hooks_schema_errors_are_readable():
    plan, errors = ms.validate_hooks({"hooks": [{"index": "one"}]}, DURATION)
    assert plan is None
    assert errors and errors[0]["code"] == "schema"
    assert "hooks.0" in errors[0]["path"]


def test_cold_open_timestamp_is_pulled_from_prose():
    h = ms.Hook.model_validate(dict(HOOK, cold_open_at=None, cold_open="The ferry shot at 01:02:03"))
    assert h.cold_open_seconds() == 3723.0


# --- beats validation -------------------------------------------------------

def test_validate_beats_accepts_a_good_plan():
    plan, errors = ms.validate_beats(_beats(), HOOK, CUES, DURATION)
    assert errors == [], ms.format_errors(errors)
    s = ms.summarize(plan)
    assert s["beats"] == 16 and s["key_beats"] == 3
    assert s["footage_seconds"] == 150.0


def test_validate_beats_rejects_invented_dialogue():
    data = _beats()
    data["beats"][3]["captions"][0]["text"] = "You were never here, Teddy"
    plan, errors = ms.validate_beats(data, HOOK, CUES, DURATION)
    assert [e["code"] for e in errors] == ["caption_text"]
    assert errors[0]["beat"] == 3
    assert "never invent dialogue" in errors[0]["message"]
    assert "closest:" in errors[0]["message"]


def test_validate_beats_accepts_a_caption_that_is_part_of_a_longer_cue():
    data = _beats()
    # The model quotes half a merged line: still real dialogue.
    data["beats"][4]["captions"][0]["text"] = "her name was Rachel"
    plan, errors = ms.validate_beats(data, HOOK, CUES, DURATION)
    assert errors == []


def test_validate_beats_tolerates_punctuation_and_case():
    data = _beats()
    data["beats"][0]["captions"][0]["text"] = "where are we NOW, 0?"
    plan, errors = ms.validate_beats(data, HOOK, CUES, DURATION)
    assert errors == []


def test_validate_beats_catches_budget_count_order_and_cold_open():
    data = _beats(n=12, length=15.0)  # 180 s: over budget, too few beats
    data["beats"][2]["start"] = data["beats"][1]["start"] + 1  # overlap
    data["beats"][2]["end"] = data["beats"][2]["start"] + 9
    data["beats"][0]["start"] = 10.0  # cold open at 5 s now outside
    data["beats"][0]["end"] = 19.0
    data["beats"][0]["captions"][0]["at"] = 25.0  # outside the beat
    plan, errors = ms.validate_beats(data, HOOK, CUES, DURATION)
    codes = {e["code"] for e in errors}
    assert {"beat_count", "footage", "beat_order", "cold_open", "caption_time"} <= codes


def test_validate_beats_flags_length_span_weight_emphasis_and_missing_caption():
    data = _beats()
    data["beats"][5]["end"] = data["beats"][5]["start"] + 20.0  # too long
    data["beats"][6]["start"] = 2000.0  # outside spans
    data["beats"][6]["end"] = 2009.0
    data["beats"][7]["weight"] = "huge"
    data["beats"][8]["captions"][0]["emphasis"] = "banana"
    data["beats"][9]["captions"] = []
    plan, errors = ms.validate_beats(data, HOOK, CUES, DURATION)
    codes = {e["code"] for e in errors}
    assert {"beat_length", "beat_span", "weight", "emphasis", "no_caption"} <= codes
    # Beat 6 moved out of order too and lost its cue; both are reported, nothing crashes.
    assert any(e["code"] == "caption_text" and e["beat"] == 6 for e in errors)


def test_validate_beats_requires_a_key_beat():
    data = _beats(key_every=None)
    plan, errors = ms.validate_beats(data, HOOK, CUES, DURATION)
    assert [e["code"] for e in errors] == ["no_key"]


def test_validate_beats_hook_index_mismatch():
    data = _beats()
    data["hook_index"] = 4
    plan, errors = ms.validate_beats(data, HOOK, CUES, DURATION)
    assert any(e["code"] == "hook_index" for e in errors)


def test_round_trip_through_json_text():
    text = "```json\n" + json.dumps(_beats()) + "\n```"
    plan, errors = ms.validate_beats(ms.parse_json(text), HOOK, CUES, DURATION)
    assert errors == []


def test_match_caption_window_respects_pad():
    cues = [{"start": 100.0, "end": 103.0, "text": "hello there general"}]
    ratio, cue = ms.match_caption("hello there general", cues, 90.0, 98.0)
    assert ratio == 0.0 and cue is None  # 2 s away, pad is 1.5
    ratio, cue = ms.match_caption("hello there general", cues, 90.0, 99.0)
    assert ratio == 1.0


def test_fixture_srt_feeds_the_prompts():
    cues = fp.parse_subtitles(__file__.replace("test_movieshorts.py", "fixtures/film_sample.srt"))
    text = ms.build_hooks_prompt(cues, 6200)
    assert "monster" in text
