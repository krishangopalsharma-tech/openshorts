"""movierecap: the three prompts, the protected-range union with the wall,
the validators, the narration lint, the budget and the voiceover fit plan."""

import pytest

import film_prep as fp
import movierecap as mr

DURATION = 7200.0  # two hours; wall at 5400
CUES = [{"start": 5.0 + 20.0 * i, "end": 8.0 + 20.0 * i, "text": f"spoken line number {i} here"}
        for i in range(350)]

SPOILERS = {
    "protected": [
        {"start": "01:42:10", "end": "02:00:00", "tier": "ending", "why": "resolution"},
        {"start": "01:18:05", "end": "01:21:30", "tier": "twist", "why": "the reveal"},
        {"start": "00:52:00", "end": "00:54:15", "tier": "midpoint", "why": "the bargain"},
    ],
    "safe_premise": "Two marshals arrive on an island.",
    "central_question": "What is the island for?",
    "twist_exists": True,
    "twist_location": "01:18:05",
    "twist_effect": "revokes what you believed since minute three",
}

STRUCTURE = {
    "series_title": "How Shutter Island lies",
    "parts": [
        {"index": 1, "title": "Why the first twenty minutes lie to you", "claim": "The grammar is the tell.",
         "evidence_band": {"start": "00:00:00", "end": "00:38:00"}, "open_question": "Why does the grammar break once?",
         "withheld": "Every first-act consequence.", "cannot_deliver": "Sustained unease."},
        {"index": 2, "title": "The cut it repeats six times", "claim": "One pattern, six placements.",
         "evidence_band": {"start": "00:38:00", "end": "01:10:00"}, "open_question": "What is the sixth for?",
         "withheld": "The midpoint's outcome.", "cannot_deliver": "The rhythm."},
        {"index": 3, "title": "What to watch for the first time", "claim": "The ending is built to be missed once.",
         "evidence_band": {"start": "00:20:00", "end": "01:15:00"}, "open_question": "Which scene would you re-watch?",
         "withheld": "The ending.", "cannot_deliver": "Falling for it."},
    ],
}


def _protected():
    return mr.union_protected(SPOILERS, DURATION)


def _plan(n=18, length=10.4, start=60.0, gap=60.0, index=1, words=27):
    chunks = []
    t = start
    for i in range(n):
        chunks.append({"start": t, "end": round(t + length, 2),
                       "narration": " ".join(["watch"] * words), "shows": "a doorway", "withholds": "who enters"})
        t += length + gap
    return {"index": index, "target_duration": 150.0, "chunks": chunks,
            "closing_lines": ["So what is the doorway for?"],
            "spoiler_self_check": "Nothing about who is behind the door, or why it matters."}


# --- protected ranges -------------------------------------------------------

def test_union_adds_the_wall_and_merges_overlaps():
    ranges = _protected()
    tiers = [r["tier"] for r in ranges]
    assert tiers[-1] == "wall" or "wall" in ranges[-1]["why"]
    assert ranges[-1]["end"] == DURATION
    # The ending range (6130-7200) overlaps the wall (5400-7200): one merged range.
    assert sum(1 for r in ranges if r["start"] >= 5400) == 1
    assert ranges[-1]["start"] == 5400.0
    assert any(r["tier"] == "twist" for r in ranges)
    assert mr.wall_seconds(DURATION) == 5400.0


def test_union_takes_manual_exclusions():
    ranges = mr.union_protected(SPOILERS, DURATION, manual=[{"start": 100, "end": 130}])
    assert any(r["start"] == 100.0 and r["tier"] == "manual" for r in ranges)


# --- prompts ----------------------------------------------------------------

def test_prompts_carry_their_constraints():
    a = mr.build_prompt_a(CUES, DURATION)
    assert "Runtime: 02:00:00" in a and "spoken line number 3 here" in a
    sm, errors = mr.validate_spoilers(SPOILERS, DURATION)
    assert errors == []
    b = mr.build_prompt_b(CUES, DURATION, SPOILERS, _protected())
    assert "Nothing from after 01:30:00" in b
    assert "01:18:05-01:21:30 (twist)" in b
    assert "Two marshals" in b
    # Digest for pass B stops at the wall: cue 300 sits at 6005 s.
    assert "number 300 here" not in b and "number 100 here" in b
    st, errors = mr.validate_structure(STRUCTURE, DURATION, _protected())
    assert errors == []
    c = mr.build_prompt_c(CUES, DURATION, st.parts[1], _protected())
    assert 'Part 2 of three: "The cut it repeats six times"' in c
    assert "187.5 s of footage" in c
    assert "Draw only from 00:38:00-01:10:00" in c
    # Band digest: cue 20 (405 s) is outside, cue 150 (3005 s) inside.
    assert "number 20 here" not in c and "number 150 here" in c
    assert "{" not in c.replace("{{", "").replace("}}", "") or "{\n" in c


# --- validators -------------------------------------------------------------

def test_validate_spoilers_flags_bad_tier_and_answered_question():
    bad = dict(SPOILERS, protected=[{"start": "00:10:00", "end": "00:09:00", "tier": "spicy"}],
               central_question="Who did it, because it was the warden", twist_location=None)
    sm, errors = mr.validate_spoilers(bad, DURATION)
    codes = {e["code"] for e in errors}
    assert {"range", "tier", "twist", "question_answered"} <= codes


def test_validate_structure_catches_titles_bands_and_repeated_promises():
    bad = {"parts": [
        dict(STRUCTURE["parts"][0], title="The Setup Explained"),
        dict(STRUCTURE["parts"][1], evidence_band={"start": "01:00:00", "end": "01:50:00"}, withheld="Every first-act consequence."),
        dict(STRUCTURE["parts"][2], open_question="What happens next?", claim=""),
    ]}
    st, errors = mr.validate_structure(bad, DURATION, _protected())
    codes = {e["code"] for e in errors}
    assert {"title_banned", "band_wall", "withheld_dup", "question_plot", "missing"} <= codes
    assert all("part" in e for e in errors)
    st2, errors2 = mr.validate_structure({"parts": STRUCTURE["parts"][:2]}, DURATION, _protected())
    assert any(e["code"] == "parts" for e in errors2)


def test_validate_plan_accepts_a_good_part():
    st, _ = mr.validate_structure(STRUCTURE, DURATION, _protected())
    plan, errors, warnings = mr.validate_plan(_plan(), st.parts[0], DURATION, _protected())
    assert errors == [], mr.format_errors(errors)
    assert warnings == []
    assert plan.index == 1


def test_validate_plan_hard_fails_protected_wall_band_and_silence():
    st, _ = mr.validate_structure(STRUCTURE, DURATION, _protected())
    protected = _protected()
    data = _plan(index=2, start=2300.0, gap=60.0)          # part 2 band 2280-4200
    data["chunks"][3]["start"], data["chunks"][3]["end"] = 3125.0, 3135.0   # inside the midpoint range
    data["chunks"][5]["start"], data["chunks"][5]["end"] = 5395.0, 5405.0   # across the wall (and outside band)
    data["chunks"][6]["narration"] = ""
    data["chunks"][7]["end"] = data["chunks"][7]["start"] + 22.0
    plan, errors, warnings = mr.validate_plan(data, st.parts[1], DURATION, protected)
    codes = {e["code"] for e in errors}
    assert {"protected", "wall", "band", "silent", "length", "order"} <= codes
    prot = next(e for e in errors if e["code"] == "protected")
    assert prot["chunk"] == 3 and "midpoint" in prot["message"] and "00:52:00" in prot["message"]


def test_validate_plan_lints_resolution_language_without_failing():
    st, _ = mr.validate_structure(STRUCTURE, DURATION, _protected())
    data = _plan()
    data["chunks"][2]["narration"] = "Then he finally opens it and it turns out the room is empty, twenty words " + "x " * 12
    plan, errors, warnings = mr.validate_plan(data, st.parts[0], DURATION, _protected())
    assert errors == []
    hits = {w["message"] for w in warnings if w.get("chunk") == 2}
    assert any("then he" in h for h in hits) and any("finally" in h for h in hits) and any("turns out" in h for h in hits)
    assert "chunk 3:" in mr.format_errors(warnings)


def test_validate_plan_budget_words_self_check_and_flip_guard():
    st, _ = mr.validate_structure(STRUCTURE, DURATION, _protected())
    data = _plan(n=14, length=15.0, words=45)  # 210 s footage (budget 187.5), 630 words
    data["spoiler_self_check"] = "Nothing."
    for i in (0, 2, 4, 6):
        data["chunks"][i]["flip"] = True
        data["chunks"][i]["flip_reason"] = "eyeline"
    data["chunks"][8]["flip"] = True  # no reason
    plan, errors, warnings = mr.validate_plan(data, st.parts[0], DURATION, _protected())
    codes = {e["code"] for e in errors}
    assert {"footage", "words", "self_check", "flips", "flip"} <= codes
    assert any("alternate" in e["message"] for e in errors if e["code"] == "flips") or \
        any("at most" in e["message"] for e in errors if e["code"] == "flips")


def test_prompt_c_story_voice_carries_the_mood_and_bans_the_apparatus():
    protected = _protected()
    parts = [dict(p, mood="sad") for p in STRUCTURE["parts"]]
    st, errors = mr.validate_structure({"parts": parts}, DURATION, protected)
    assert errors == []
    text = mr.build_prompt_c(CUES, DURATION, st.parts[0], protected)
    assert "The mood of this part: sad (" in text
    assert "you are a storyteller, not a critic" in text
    assert 'Banned: "the film", "the camera"' in text
    assert "Tell them what to watch for" not in text
    # Essay keeps the original voice; an unknown mood falls back to tense.
    essay = mr.build_prompt_c(CUES, DURATION, STRUCTURE["parts"][0], protected, style="essay")
    assert "Tell them what to watch for" in essay and "storyteller" not in essay
    assert "The mood of this part: tense (" in essay
    # Pass B asks for the mood.
    b = mr.build_prompt_b(CUES, DURATION, SPOILERS, protected)
    assert '"mood": "one of: tense, ominous, sad' in b


def test_story_style_warns_on_lecture_vocabulary_but_never_fails():
    st, _ = mr.validate_structure(STRUCTURE, DURATION, _protected())
    data = _plan()
    lecture = ("Watch the lens. The camera holds this kindness like an exhibit, and notice how "
               "the film stages the homecoming for the viewer " + "x " * 5)  # 27 words, on budget
    for i in range(7):
        data["chunks"][i]["narration"] = lecture
    plan, errors, warnings = mr.validate_plan(data, st.parts[0], DURATION, _protected())
    assert errors == []
    craft = [w for w in warnings if w["code"] == "craft_language"]
    assert len(craft) == 8  # seven chunks + the part-level summary
    assert any('"the camera"' in w["message"] and w.get("chunk") == 0 for w in craft)
    summary = next(w for w in craft if "chunk" not in w)
    assert "7 of 18 chunks" in summary["message"] and "tense" in summary["message"]
    # The essay style is allowed to talk about the camera.
    _, _, essay_warnings = mr.validate_plan(data, st.parts[0], DURATION, _protected(), style="essay")
    assert not [w for w in essay_warnings if w["code"] == "craft_language"]
    # A part with lecture words in a third or fewer of its chunks gets no summary.
    data2 = _plan()
    data2["chunks"][0]["narration"] = lecture
    _, _, w2 = mr.validate_plan(data2, st.parts[0], DURATION, _protected())
    assert [w for w in w2 if w["code"] == "craft_language" and "chunk" not in w] == []


def test_lint_narration_matches_whole_words_only():
    assert mr.lint_narration("The friendliness of the frame") == []
    assert "in the end" in mr.lint_narration("In the end, nothing.")
    assert "kills" in mr.lint_narration("He kills the lights.")


# --- budget -----------------------------------------------------------------

def test_budget_numbers():
    st, _ = mr.validate_structure(STRUCTURE, DURATION, _protected())
    plans = {}
    # Part 2 keeps its chunks before the midpoint range at 3120 s.
    for idx, start, gap in ((1, 60.0, 60.0), (2, 2280.0, 30.0), (3, 1250.0, 60.0)):
        plan, errors, _ = mr.validate_plan(_plan(index=idx, start=start, gap=gap), st.parts[idx - 1], DURATION, _protected())
        assert errors == [], (idx, mr.format_errors(errors))
        plans[idx] = plan
    b = mr.budget(plans, st, DURATION, _protected())
    assert b["footage_seconds"] == pytest.approx(3 * 18 * 10.4, abs=0.1)
    assert b["footage_share"] < 0.10 and b["chunks_in_protected"] == 0 and b["chunks_past_wall"] == 0
    assert b["words_per_footage_second"] > 2.5 and b["silent_share"] == 0.0
    assert b["withheld_distinct"] is True and b["ok"] is True
    assert b["parts_planned"] == [1, 2, 3]


# --- voiceover fit ----------------------------------------------------------

def test_compilation_plan_shape_and_room_stops_at_protected():
    st, _ = mr.validate_structure(STRUCTURE, DURATION, _protected())
    # A manual exclusion 1.2 s after chunk 1 (130.4-140.8 s) caps its tail room.
    protected = mr.union_protected(SPOILERS, DURATION, manual=[{"start": 142.0, "end": 150.0}])
    plan, errors, _ = mr.validate_plan(_plan(), st.parts[0], DURATION, protected)
    assert errors == []
    cp = mr.compilation_plan(plan, protected, DURATION)
    assert set(cp) == {"sequence", "padding", "shots", "vo"}
    assert len(cp["shots"]) == 18 and len(cp["vo"]["lines"]) == 18
    s0 = cp["shots"][0]
    # Chunk 0 is 60-70.4 s source: finished-equivalent 48-56.32.
    assert s0["src_in"] == pytest.approx(60 / 1.25) and s0["src_out"] == pytest.approx(70.4 / 1.25)
    assert s0["head_room"] == pytest.approx(3.0 / 1.25)
    # Room is bounded by the 3 s cap, not by the neighbour 60 s away...
    assert s0["tail_room"] == pytest.approx(3.0 / 1.25)
    # ...and by a protected range when one is closer.
    assert cp["shots"][1]["tail_room"] == pytest.approx(1.2 / 1.25)
    line = cp["vo"]["lines"][0]
    assert line["actual_words"] == 27 and line["first_words"] == "watch watch watch" and line["silent"] is False


def test_fitted_to_source_multiplies_back():
    fitted = [{"src_in": 48.0, "src_out": 60.0, "shot_start": 0.0, "shot_length": 12.0,
               "voice_start": 0.15, "silent": False, "keep_audio": True, "flip": False, "chunk": {"path": "x.wav"}}]
    segs = mr.fitted_to_source(fitted)
    assert segs[0]["start"] == 60.0 and segs[0]["end"] == 75.0 and segs[0]["keep_audio"] is True


def test_vo_duration_check():
    assert mr.vo_duration_check(160.0)["ok"] is True
    bad = mr.vo_duration_check(175.0)
    assert bad["ok"] is False and "+17%" in bad["message"]


def test_fixture_srt_drives_pass_a():
    cues = fp.parse_subtitles(__file__.replace("test_movierecap.py", "fixtures/film_sample.srt"))
    text = mr.build_prompt_a(cues, 6200)
    assert "Runtime: 01:43:20" in text
