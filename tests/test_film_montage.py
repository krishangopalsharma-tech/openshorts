"""film_montage: the line index the chat sees, cue -> EDL (merge, split, hold),
the validator's errors and warnings, mood -> track."""

import film_montage as fm

DURATION = 5400.0


def _cue(start, end, text):
    return {"start": float(start), "end": float(end), "text": text}


# Ten lines: a tight exchange (0-3), one long monologue line (4), a pair far
# apart (5, 6), and a run of short lines that together outlast MAX_RUN (7-9).
CUES = [
    _cue(100.0, 101.5, "Breakfast is served."),          # 0
    _cue(101.8, 103.0, "Wow"),                            # 1  gap 0.3 → same run as 0
    _cue(103.2, 105.0, "Nora, time for bed."),            # 2  gap 0.2
    _cue(107.0, 108.0, "Night-night."),                   # 3  gap 2.0 → new run
    _cue(200.0, 212.0, "A single very long line that no cut can shorten because it is one cue"),  # 4
    _cue(300.0, 302.0, "Where is he?"),                   # 5
    _cue(330.0, 332.0, "Hiding."),                        # 6
    _cue(400.0, 403.0, "You know the rules."),            # 7
    _cue(403.3, 406.5, "No soda upstairs."),              # 8  gap 0.3
    _cue(406.8, 410.0, "What? How did you..."),           # 9  gap 0.3 → run 7-9 spans 10 s
]


def _plan(shorts):
    return {"shorts": shorts}


def _short(scenes, title="How a top spy babysat 3 kids", **kw):
    s = {"title": title, "hook": "He fought dictators. Then breakfast.", "mood": "funny",
         "ending": "the ally pulls a gun", "scenes": scenes}
    s.update(kw)
    return s


# --- line index and prompt ---------------------------------------------------------

def test_line_index_keeps_file_positions_as_ids():
    lines = fm.line_index([_cue(1, 2, "a"), _cue(3, 4, "   "), _cue(5, 6, "b  c")])
    assert [ln["id"] for ln in lines] == [0, 2]          # the blank cue keeps its slot
    assert lines[1]["text"] == "b c"


def test_prompt_names_every_line_with_its_id_and_the_rules():
    text = fm.build_prompt(CUES, DURATION, "The Spy Next Door", count=4)
    assert "#0 01:40 Breakfast is served." in text
    assert "#9 06:47 What? How did you..." in text
    assert "4 shorts" in text and "The Spy Next Door" in text
    assert "hold_after" in text and "funny, tense" in text


def test_prompt_count_is_clamped_to_the_allowed_range():
    assert "5 shorts" in fm.build_prompt(CUES, DURATION, "x", count=99)
    assert "2 shorts" in fm.build_prompt(CUES, DURATION, "x", count=0)


# --- cue -> EDL ----------------------------------------------------------------------

def _by_id():
    return {ln["id"]: ln for ln in fm.line_index(CUES)}


def test_close_lines_join_into_one_run_and_a_pause_starts_another():
    segs, warn = fm.scene_segments(fm.Scene(lines=[0, 1, 2, 3]), _by_id(), DURATION)
    assert warn == []
    assert segs == [{"start": 99.75, "end": 105.35}, {"start": 106.75, "end": 108.35}]


def test_lines_are_taken_in_time_order_whatever_order_the_chat_wrote():
    a, _ = fm.scene_segments(fm.Scene(lines=[3, 0, 2, 1]), _by_id(), DURATION)
    b, _ = fm.scene_segments(fm.Scene(lines=[0, 1, 2, 3]), _by_id(), DURATION)
    assert a == b


def test_hold_after_extends_only_the_last_run():
    segs, _ = fm.scene_segments(fm.Scene(lines=[0, 1, 2, 3], hold_after=2.0), _by_id(), DURATION)
    assert segs[0]["end"] == 105.35 and segs[1]["end"] == 110.35


def test_hold_after_is_clamped():
    assert fm.Scene(lines=[0], hold_after=99).hold_after == fm.HOLD_AFTER_MAX
    assert fm.Scene(lines=[0], hold_after=-3).hold_after == 0.0


def test_a_run_past_max_run_stays_one_cut_and_is_reported():
    # 7-9 are 0.3 s apart: one continuous 10 s stretch. Cutting inside it
    # would remove no frames, so it is kept whole and named for the chat.
    segs, warn = fm.scene_segments(fm.Scene(lines=[7, 8, 9]), _by_id(), DURATION)
    assert segs == [{"start": 399.75, "end": 410.35}]
    assert warn == [10.6]
    # dropping the middle line breaks it into two short runs, no warning
    segs, warn = fm.scene_segments(fm.Scene(lines=[7, 9]), _by_id(), DURATION)
    assert len(segs) == 2 and warn == []


def test_a_single_long_line_cannot_be_split_and_is_reported():
    segs, warn = fm.scene_segments(fm.Scene(lines=[4]), _by_id(), DURATION)
    assert len(segs) == 1 and segs[0] == {"start": 199.75, "end": 212.35}
    assert warn == [12.6]


def test_two_cuts_closer_than_half_a_second_become_one():
    # lines 0.9 s apart: separate runs by merge_gap, but after lead and tail
    # the cuts sit 0.3 s apart on the source, which is a stutter, not a trim
    by_id = {0: {"id": 0, "start": 10.0, "end": 12.0, "text": "a"},
             1: {"id": 1, "start": 12.9, "end": 14.0, "text": "b"}}
    segs, _ = fm.scene_segments(fm.Scene(lines=[0, 1]), by_id, DURATION)
    assert segs == [{"start": 9.75, "end": 14.35}]


def test_extra_picture_ranges_slot_in_by_time_and_are_capped():
    scene = fm.Scene(lines=[0, 3], extra=[{"start": "01:45.5", "end": "01:46.5"}, {"start": 150, "end": 200}])
    segs, _ = fm.scene_segments(scene, _by_id(), DURATION)
    # 105.5-106.5 sits 0.25 s before line 3's lead (106.75), so the picture
    # range and that run become one cut; line 0 stays its own; 150-200 is
    # cut to 12 s
    assert segs == [{"start": 99.75, "end": 101.85}, {"start": 105.5, "end": 108.35}, {"start": 150.0, "end": 162.0}]


def test_unknown_ids_are_skipped_not_fatal_in_the_cutter():
    segs, _ = fm.scene_segments(fm.Scene(lines=[5, 999]), _by_id(), DURATION)
    assert segs == [{"start": 299.75, "end": 302.35}]


def test_segments_never_leave_the_film():
    by_id = {0: {"id": 0, "start": 0.1, "end": 5399.9, "text": "x"}}
    segs, _ = fm.scene_segments(fm.Scene(lines=[0], hold_after=5), by_id, DURATION)
    assert segs == [{"start": 0.0, "end": 5400.0}]


def test_short_segments_keep_scene_order_not_film_order():
    short = fm.Short.model_validate(_short([{"lines": [7, 8]}, {"lines": [0, 1]}, {"lines": [5]}]))
    segs, per_scene = fm.short_segments(short, _by_id(), DURATION)
    assert [s["start"] for s in segs] == [399.75, 99.75, 299.75]
    assert [sc["index"] for sc in per_scene] == [0, 1, 2]
    assert per_scene[1]["text"] == "Breakfast is served. / Wow"
    assert per_scene[0]["seconds"] == round(406.85 - 399.75, 2)


# --- validation -----------------------------------------------------------------------

def _long_cues(n=60, gap=20.0, length=4.0):
    return [_cue(10 + gap * i, 10 + gap * i + length, f"line number {i}") for i in range(n)]


def _ok_plan():
    # 3 scenes x 5 lines x (4 s + lead/tail 0.6) = 69 s: inside the range
    return _plan([_short([{"lines": list(range(0, 5))}, {"lines": list(range(10, 15))},
                          {"lines": list(range(20, 25)), "hold_after": 1.5}])])


def test_valid_plan_returns_previews_with_the_edl():
    plan, errors, warnings, previews = fm.validate_shorts(_ok_plan(), _long_cues(), DURATION)
    assert plan is not None and errors == [] and warnings == []
    p = previews[0]
    assert p["cuts"] == 15 and p["seconds"] == 70.5 and len(p["scenes"]) == 3
    assert p["scenes"][2]["hold_after"] == 1.5 and p["title"].startswith("How a top spy")


def test_schema_errors_return_no_plan():
    plan, errors, _w, previews = fm.validate_shorts({"shorts": [{"title": "x"}]}, _long_cues(), DURATION)
    assert plan is None and previews == []
    assert errors and all(e["code"] == "schema" for e in errors)


def test_unknown_and_reused_lines_are_errors_with_their_place():
    data = _plan([_short([{"lines": [0, 1, 2, 3, 4]}, {"lines": [4, 5, 6, 7, 8]}, {"lines": [900, 10, 11, 12, 13]}])])
    plan, errors, _w, _p = fm.validate_shorts(data, _long_cues(), DURATION)
    assert plan is not None
    codes = {(e["code"], e.get("short"), e.get("scene")) for e in errors}
    assert ("reused_line", 0, 1) in codes and ("unknown_line", 0, 2) in codes
    assert "short 1 scene 2: line #4 is already in short 1 scene 1" in fm.format_errors(errors)


def test_footage_outside_the_range_and_bad_titles_are_errors():
    tiny = _plan([_short([{"lines": [0]}, {"lines": [1]}, {"lines": [2]}], title="Part 2 explained")])
    _plan_, errors, _w, previews = fm.validate_shorts(tiny, _long_cues(), DURATION)
    codes = [e["code"] for e in errors]
    assert "total" in codes and "title" in codes
    assert previews[0]["seconds"] == 13.8


def test_extra_ranges_are_validated():
    data = _plan([_short([{"lines": list(range(0, 5)), "extra": [{"start": "x", "end": "y"}]},
                          {"lines": list(range(10, 15)), "extra": [{"start": 5000, "end": 5401}]},
                          {"lines": list(range(20, 25)), "extra": [{"start": "10:00", "end": "10:20"}]}])])
    plan, errors, warnings, previews = fm.validate_shorts(data, _long_cues(), DURATION)
    assert plan is not None
    assert [(e["code"], e["scene"]) for e in errors] == [("extra_range", 0), ("extra_range", 1)]
    assert [(w["code"], w["scene"]) for w in warnings] == [("extra_long", 2)]
    # the 20 s range is cut to 12 s in the EDL
    assert any(s == {"start": 600.0, "end": 612.0} for s in previews[0]["scenes"][2]["segments"])


def test_scene_count_is_enforced():
    one = _plan([_short([{"lines": list(range(0, 15))}])])
    _p, errors, _w, _pv = fm.validate_shorts(one, _long_cues(), DURATION)
    assert any(e["code"] == "scene_count" for e in errors)


def test_long_lines_and_long_runs_are_warnings_not_errors():
    cues = _long_cues()
    cues.append(_cue(2000.0, 2011.0, " ".join(["word"] * 14)))       # id 60: 14 words, 11 s
    data = _plan([_short([{"lines": list(range(0, 5))}, {"lines": list(range(10, 15))}, {"lines": [60, 20, 21, 22, 23]}])])
    plan, errors, warnings, _p = fm.validate_shorts(data, cues, DURATION)
    assert plan is not None and errors == []
    codes = {w["code"] for w in warnings}
    assert codes == {"long_line", "long_run"}


def test_mood_falls_back_to_funny():
    assert fm.Short.model_validate(_short([{"lines": [0]}], mood="Weird")).mood == "funny"
    assert fm.Short.model_validate(_short([{"lines": [0]}], mood=" Tense ")).mood == "tense"


# --- music, captions, watermark -----------------------------------------------------

TRACKS = [{"file": "empire-of-shadows-cinematic.mp3", "name": "Empire Of Shadows"},
          {"file": "fast-dynamic-percussion.mp3", "name": "Fast Dynamic Percussion"},
          {"file": "verclub-upbeat.mp3", "name": "Upbeat"}]


def test_pick_track_by_mood_keywords():
    assert fm.pick_track("funny", TRACKS)["file"] == "verclub-upbeat.mp3"
    assert fm.pick_track("tense", TRACKS)["file"] == "empire-of-shadows-cinematic.mp3"
    assert fm.pick_track("driving", TRACKS)["file"] == "fast-dynamic-percussion.mp3"
    assert fm.pick_track("romantic", TRACKS) == TRACKS[0]       # no keyword hit: first track
    assert fm.pick_track("funny", []) is None


def test_caption_style_is_one_centred_line():
    st = fm.caption_style()
    assert st["preset"] == "film_pop" and st["overrides"]["position"] == "center"
    assert st["overrides"]["max_lines"] == 1


def test_watermark_is_a_text_overlay_or_nothing():
    assert fm.watermark("  ") == []
    item = fm.watermark("made by sona darus")[0]
    assert item["type"] == "text" and item["y"] > 0.9 and item["text"] == "made by sona darus"
