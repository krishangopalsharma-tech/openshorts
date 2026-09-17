"""Beats -> shots expansion (film_prep) and the render assembly (film_render),
driven end to end with fakes: no ffmpeg, no GPU, no models."""

import os

import pytest

import film_prep as fp
import film_render as fr

SPEED = 1.25


def _beats(n=16, length=9.375):
    beats = []
    t = 100.0
    for i in range(n):
        beats.append({"start": t, "end": t + length, "weight": "key" if i in (1, 7, 13) else "normal",
                      "composite_ok": i != 7,
                      "captions": [{"at": t + 2.0, "text": f"line {i}"}]})
        t += length + 20.0
    return beats


# --- expansion --------------------------------------------------------------

def test_expand_plan_hits_the_reference_rhythm():
    shots = fp.expand_plan(_beats(), speed=SPEED)
    s = fp.shots_summary(shots, SPEED)
    assert 70 <= s["shots"] <= 110
    assert 1.2 <= s["mean_shot_finished"] <= 1.7
    assert s["min_shot_finished"] >= 1.2 - 1e-6
    assert s["max_shot_finished"] <= 1.8 + 1e-6
    # Source seconds are exactly the beats' footage: nothing dropped, nothing invented.
    assert s["source_seconds"] == pytest.approx(16 * 9.375, abs=0.01)
    # Shots tile each beat without gaps or overlaps.
    by_beat = {}
    for sh in shots:
        by_beat.setdefault(sh["beat"], []).append(sh)
    for i, group in by_beat.items():
        assert group[0]["start"] == pytest.approx(100.0 + i * 29.375, abs=1e-3)
        for a, b in zip(group, group[1:]):
            assert a["end"] == pytest.approx(b["start"], abs=1e-3)


def test_expand_plan_is_deterministic_and_cycles_the_ladder():
    a = fp.expand_plan(_beats(), speed=SPEED, seed=7)
    b = fp.expand_plan(_beats(), speed=SPEED, seed=7)
    assert a == b
    scales = [s["scale"] for s in a]
    assert set(scales) == set(fp.CROP_LADDER)
    # Never two identical scales adjacent, anywhere in the plan.
    assert all(x != y for x, y in zip(scales, scales[1:]))
    # The ladder does not restart on every beat: beat 1 does not open at 1.00.
    first_of_beat = {}
    for s in a:
        first_of_beat.setdefault(s["beat"], s["scale"])
    assert len(set(first_of_beat.values())) > 1
    assert max(scales) <= fp.SCALE_MAX


def test_expand_plan_places_composites_on_key_beats_that_allow_them():
    beats = _beats()
    shots = fp.expand_plan(beats, speed=SPEED)
    comps = [s for s in shots if s["composite"]]
    assert round(len(shots) * fp.COMPOSITE_SHARE) == len(comps)
    for s in comps:
        assert beats[s["beat"]]["composite_ok"]
        assert not s["key_line"]
    # Beat 7 is key but forbids composites: none landed there.
    assert not any(s["beat"] == 7 for s in comps)
    # Exactly one shot per beat carries the key line.
    assert sum(1 for s in shots if s["key_line"]) == len(beats)


def test_expand_beat_uses_real_scene_cuts_first():
    beat = {"start": 100.0, "end": 110.0, "weight": "normal", "captions": []}
    shots, _, _ = fp.expand_beat(beat, 0, speed=SPEED, scene_cuts=[103.7, 106.9])
    bounds = {round(s["end"], 3) for s in shots}
    assert 103.7 in bounds and 106.9 in bounds
    # A sliver next to a cut is absorbed rather than becoming a 0.3 s shot.
    shots2, _, _ = fp.expand_beat(beat, 0, speed=SPEED, scene_cuts=[100.2])
    assert all(s["end"] - s["start"] >= 1.2 * SPEED - 1e-6 for s in shots2)


def test_expand_beat_honours_an_explicit_schedule():
    beat = {"start": 0.0, "end": 9.0, "weight": "key", "captions": [{"at": 4.0}]}
    shots, _, _ = fp.expand_beat(beat, 0, speed=SPEED, durations=[1.875, 1.875, 1.5, 2.25])
    assert [round(s["end"] - s["start"], 3) for s in shots] == [1.875, 1.875, 1.5, 2.25, 1.5]
    assert [s["key_line"] for s in shots] == [False, False, True, False, False]


def test_key_beats_run_longer_shots():
    normal = {"start": 0.0, "end": 12.0, "weight": "normal", "captions": []}
    key = {"start": 0.0, "end": 12.0, "weight": "key", "captions": []}
    n, _, _ = fp.expand_beat(normal, 0, speed=SPEED)
    k, _, _ = fp.expand_beat(key, 0, speed=SPEED)
    assert len(k) <= len(n)


def test_crop_filter():
    assert fp.crop_filter(1.0, (0.5, 0.5)) is None
    f = fp.crop_filter(1.45, (0.42, 0.30))
    assert f.startswith("crop=trunc(iw/1.450/2)*2:trunc(ih/1.450/2)*2:")
    assert "*0.420/2)*2" in f and "*0.300/2)*2" in f
    # Out-of-range scale is clamped, not passed through.
    assert "2.500" not in fp.crop_filter(2.5, (0.5, 0.5))


# --- commands ---------------------------------------------------------------

def test_shot_cut_command_has_crop_grade_and_input_seek():
    shot = {"start": 132.5, "end": 134.25, "scale": 1.45, "anchor": [0.42, 0.3]}
    cmd = fr.shot_cut_command("film.mp4", shot, "shot.mp4", grade="washed", encoder="cpu")
    assert cmd.index("-ss") < cmd.index("-i")  # input seeking
    assert cmd[cmd.index("-ss") + 1] == "132.500"
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("crop=") and "eq=saturation=0.78" in vf
    assert "libx264" in cmd and "veryfast" in cmd


def test_shot_cut_command_neutral_has_no_filter_and_blur_uses_filter_complex():
    cmd = fr.shot_cut_command("f.mp4", {"start": 0, "end": 1.5, "scale": 1.0}, "o.mp4", encoder="nvenc")
    assert "-vf" not in cmd and "-filter_complex" not in cmd
    assert "h264_nvenc" in cmd
    shot = {"start": 0, "end": 1.5, "scale": 1.0, "blur": {"x": 0.2, "y": 0.3, "w": 0.4, "h": 0.2}}
    cmd = fr.shot_cut_command("f.mp4", shot, "o.mp4", encoder="cpu")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "gblur" in graph and "overlay=W*0.200:H*0.300" in graph


def test_grade_chain_knows_the_film_grades():
    assert "saturation=0.78" in fr.grade_chain("washed")
    assert "gamma_b=1.08" in fr.grade_chain("cold")
    assert fr.grade_chain("neutral") is None and fr.grade_chain(None) is None


def test_finalize_command_normalises_once_with_lra_7():
    cmd = fr.finalize_command("in.mp4", "out.mp4", fps=60)
    assert cmd[cmd.index("-af") + 1] == "loudnorm=I=-14:TP=-2.0:LRA=7"
    assert cmd[cmd.index("-vf") + 1] == "fps=60"


def test_captions_for_cut_remaps_and_divides_by_speed():
    cues = [{"start": 101.0, "end": 103.0, "text": "hello world"},
            {"start": 500.0, "end": 502.0, "text": "far away"}]
    shots = [{"start": 100.0, "end": 104.0}, {"start": 200.0, "end": 204.0}]
    t = fr.captions_for_cut(cues, shots, 1.25)
    words = [w for s in t["segments"] for w in s["words"]]
    assert [w["word"].strip() for w in words] == ["hello", "world"]
    # 101.0 on the source is 1.0 into the cut, 0.8 after the speed change.
    assert words[0]["start"] == pytest.approx(0.8, abs=0.01)


# --- assembly with fakes ----------------------------------------------------

def test_render_short_runs_the_steps_in_order(tmp_path):
    calls = []

    def runner(cmd):
        calls.append(cmd)
        open(cmd[-1], "wb").close()

    def reframe(inp, out, fmt):
        calls.append(["reframe", inp, out, fmt])
        open(out, "wb").close()
        return True

    def captioner(video, transcript, out, preset, w, h):
        calls.append(["captions", preset, w, h, len(transcript["segments"])])
        open(out, "wb").close()
        return True

    def mixer(video, track, out, spec):
        calls.append(["music", os.path.basename(track), spec["duck"]])
        open(out, "wb").close()
        return True

    shots = fp.expand_plan(_beats(4), speed=SPEED)
    cues = [{"start": 101.0, "end": 103.0, "text": "a real line here"}]
    out = tmp_path / "short.mp4"
    report = fr.render_short("film.mp4", shots, str(out), str(tmp_path), speed=SPEED,
                             output_format="square", grade="warm", cues=cues,
                             music_track="/lib/tense/pulse.mp3", runner=runner,
                             reframe=reframe, captioner=captioner, mixer=mixer,
                             probe_size=lambda p: (1080, 1080), log=lambda *_: None)
    kinds = []
    for c in calls:
        if c[0] == "ffmpeg":
            if "-ss" in c:
                kinds.append("cut")
            elif "concat" in c:
                kinds.append("concat")
            elif any("setpts" in a for a in c):
                kinds.append("speed")
            elif any("loudnorm" in a for a in c):
                kinds.append("finalize")
        else:
            kinds.append(c[0])
    assert kinds.count("cut") == len(shots)
    order = [k for k in kinds if k != "cut"]
    assert order == ["concat", "speed", "reframe", "captions", "music", "finalize"]
    assert report["captions"] and report["music"] == "pulse.mp3"
    assert report["output"] == str(out)
    # Grade reached every shot; every temp part was cleaned up.
    cuts = [c for c in calls if c[0] == "ffmpeg" and "-ss" in c]
    assert all("colorbalance" in c[c.index("-vf") + 1] for c in cuts if "-vf" in c)
    assert not [p for p in os.listdir(tmp_path) if p.startswith("temp_film_")]


def test_render_short_rejects_bad_format_and_empty_plan(tmp_path):
    with pytest.raises(fr.RenderError):
        fr.render_short("f.mp4", [{"start": 0, "end": 1}], "o.mp4", str(tmp_path),
                        output_format="horizontal")
    with pytest.raises(fr.RenderError):
        fr.render_short("f.mp4", [], "o.mp4", str(tmp_path))


def test_render_short_cleans_up_when_a_step_fails(tmp_path):
    def runner(cmd):
        if any("setpts" in a for a in cmd):
            raise fr.RenderError("boom")
        open(cmd[-1], "wb").close()
    shots = fp.expand_plan(_beats(2), speed=SPEED)
    with pytest.raises(fr.RenderError):
        fr.render_short("f.mp4", shots, str(tmp_path / "o.mp4"), str(tmp_path),
                        runner=runner, reframe=lambda *a: True, log=lambda *_: None)
    assert not [p for p in os.listdir(tmp_path) if p.startswith("temp_film_")]


def test_cut_shots_uses_one_encoder_for_every_part(monkeypatch, tmp_path):
    # Mixed NVENC/libx264 parts concat "successfully" into a stream the next
    # pass cannot read; every part must carry the same encoder.
    monkeypatch.setenv("FFMPEG_ENCODER", "x264")
    seen = []
    def runner(cmd):
        seen.append("h264_nvenc" if "h264_nvenc" in cmd else "libx264")
    shots = [{"start": i, "end": i + 1.5, "scale": 1.0} for i in range(12)]
    parts = fr.cut_shots("f.mp4", shots, str(tmp_path), runner=runner, nvenc_parallel=2, cpu_parallel=4)
    assert len(parts) == 12 and set(seen) == {"libx264"}


def test_music_dialogue_profile_dips_instead_of_vanishing():
    import music
    spec = {"volume_db": -18.0, "duck": 100.0, "fade_out": 1.0}
    g = music.build_audio_graph(spec, 120.0, profile="dialogue")
    assert "ratio=6.00" in g and "release=260" in g and "threshold=0.05" in g
    g2 = music.build_audio_graph(spec, 120.0)
    assert "ratio=20.00" in g2 and "release=300" in g2


def test_film_pop_preset_exists():
    import caption_styles
    p = caption_styles.STYLE_PRESETS["film_pop"]
    assert p["animation"] == "pop" and p["uppercase"] is False
