"""film_render: the recap assembly (silent preview and narrated part), driven
end to end with fakes: no ffmpeg, no GPU, no models."""

import os

import pytest

import film_render as fr

SPEED = 1.25


def _chunks(n=6, length=10.4, start=100.0, gap=60.0):
    out, t = [], start
    for _ in range(n):
        out.append({"start": t, "end": round(t + length, 2), "hflip": False})
        t += length + gap
    return out


# --- commands ---------------------------------------------------------------

def test_shot_cut_command_has_input_seek_and_uniform_encode():
    cmd = fr.shot_cut_command("film.mp4", {"start": 132.5, "end": 143.0}, "c.mp4",
                              out_size=(1280, 528))
    assert cmd.index("-ss") < cmd.index("-i")  # input seeking
    assert cmd[cmd.index("-ss") + 1] == "132.500"
    assert cmd[cmd.index("-vf") + 1] == "scale=1280:528:flags=lanczos"
    assert "-pix_fmt" in cmd and "aac" in cmd


def test_shot_filter_hflip_is_manual_only():
    assert fr.shot_filter({"start": 0, "end": 1}) is None
    assert fr.shot_filter({"start": 0, "end": 1, "hflip": True}) == "hflip"
    assert fr.shot_filter({"hflip": True}, out_size=(1921, 1080)) == "hflip,scale=1920:1080:flags=lanczos"


def test_finalize_command_normalises_once_with_lra_7_or_mutes():
    cmd = fr.finalize_command("in.mp4", "out.mp4", fps=60)
    assert cmd[cmd.index("-af") + 1] == "loudnorm=I=-14:TP=-2.0:LRA=7"
    assert cmd[cmd.index("-vf") + 1] == "fps=60"
    # The recap's silent preview drops the soundtrack instead of normalising it.
    muted = fr.finalize_command("in.mp4", "out.mp4", fps=60, mute=True)
    assert "-an" in muted and "-af" not in muted and "aac" not in muted


def test_cut_shots_uses_one_encoder_for_every_part(monkeypatch, tmp_path):
    # Mixed NVENC/libx264 parts concat "successfully" into a stream the next
    # pass cannot read; every part must carry the same encoder.
    monkeypatch.setenv("FFMPEG_ENCODER", "x264")
    seen = []
    def runner(cmd):
        seen.append("h264_nvenc" if "h264_nvenc" in cmd else "libx264")
    parts = fr.cut_shots("f.mp4", _chunks(12), str(tmp_path), runner=runner, nvenc_parallel=2, cpu_parallel=4)
    assert len(parts) == 12 and set(seen) == {"libx264"}


def test_narration_mix_command_mutes_the_bed_except_kept_ranges():
    chunks = [{"path": "l1.wav", "voice_start": 0.15}, {"path": "l2.wav", "voice_start": 8.5}]
    cmd = fr.narration_mix_command("v.mp4", chunks, "o.mp4", keep_ranges=[(8.32, 16.6)], duration=150.0)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "adelay=150:all=1" in graph and "adelay=8500:all=1" in graph
    assert "between(t,8.320,16.600)" in graph and "sidechaincompress" in graph
    assert "ratio=12" in graph and "release=300" in graph
    assert cmd[cmd.index("-t") + 1] == "150.000" and "copy" in cmd
    # No kept dialogue: the bed is dropped entirely.
    cmd2 = fr.narration_mix_command("v.mp4", chunks, "o.mp4")
    assert "[0:a]" not in cmd2[cmd2.index("-filter_complex") + 1]


# --- assembly with fakes ----------------------------------------------------

def _fakes(calls):
    def runner(cmd):
        calls.append(cmd)
        open(cmd[-1], "wb").close()

    def reframe(inp, out, fmt):
        calls.append(["reframe", inp, out, fmt])
        open(out, "wb").close()
        return True
    return runner, reframe


def _kinds(calls):
    kinds = []
    for c in calls:
        if c[0] == "ffmpeg":
            if "-ss" in c:
                kinds.append("cut")
            elif "concat" in c:
                kinds.append("concat")
            elif any("setpts" in a for a in c):
                kinds.append("speed")
            elif "-filter_complex" in c:
                kinds.append("mix")
            elif "-an" in c or any("loudnorm" in a or a == "anull" for a in c):
                kinds.append("finalize")
        else:
            kinds.append(c[0])
    return kinds


def test_render_silent_runs_the_steps_in_order_and_mutes(tmp_path):
    calls = []
    runner, reframe = _fakes(calls)
    out = tmp_path / "part.mp4"
    report = fr.render_silent("film.mp4", _chunks(4), str(out), str(tmp_path), speed=SPEED,
                              output_format="vertical", runner=runner, reframe=reframe,
                              probe_size=lambda p: (1280, 528), log=lambda *_: None)
    kinds = _kinds(calls)
    assert kinds.count("cut") == 4
    assert [k for k in kinds if k != "cut"] == ["concat", "speed", "reframe", "finalize"]
    final = [c for c in calls if c[0] == "ffmpeg" and "-an" in c]
    assert len(final) == 1 and final[0][-1] == str(out)
    assert report["muted"] is True and report["output"] == str(out)
    assert not [p for p in os.listdir(tmp_path) if p.startswith("temp_film_")]


def test_render_silent_rejects_bad_format_and_empty_plan(tmp_path):
    with pytest.raises(fr.RenderError):
        fr.render_silent("f.mp4", _chunks(1), "o.mp4", str(tmp_path), output_format="horizontal")
    with pytest.raises(fr.RenderError):
        fr.render_silent("f.mp4", [], "o.mp4", str(tmp_path))


def test_render_silent_cleans_up_when_a_step_fails(tmp_path):
    def runner(cmd):
        if any("setpts" in a for a in cmd):
            raise fr.RenderError("boom")
        open(cmd[-1], "wb").close()
    with pytest.raises(fr.RenderError):
        fr.render_silent("f.mp4", _chunks(2), str(tmp_path / "o.mp4"), str(tmp_path),
                         runner=runner, reframe=lambda *a: True, probe_size=lambda p: (1280, 528),
                         log=lambda *_: None)
    assert not [p for p in os.listdir(tmp_path) if p.startswith("temp_film_")]


def test_render_narrated_places_lines_and_keeps_flagged_dialogue(tmp_path):
    calls = []
    runner, reframe = _fakes(calls)
    segments = []
    t = 0.0
    for i, c in enumerate(_chunks(3)):
        segments.append({"start": c["start"], "end": c["end"], "shot_start": t, "shot_length": 8.32,
                         "voice_start": t + 0.15, "silent": False, "keep_audio": i == 1, "flip": i == 2,
                         "chunk": {"path": f"l{i}.wav", "duration": 7.0}})
        t += 8.32
    out = tmp_path / "part1.mp4"
    report = fr.render_narrated("film.mp4", segments, str(out), str(tmp_path), speed=SPEED,
                                runner=runner, reframe=reframe, probe_size=lambda p: (1280, 528),
                                log=lambda *_: None)
    kinds = _kinds(calls)
    assert [k for k in kinds if k != "cut"] == ["concat", "speed", "reframe", "mix", "finalize"]
    cuts = [c for c in calls if c[0] == "ffmpeg" and "-ss" in c]
    assert "hflip" in cuts[2][cuts[2].index("-vf") + 1] and "hflip" not in cuts[0][cuts[0].index("-vf") + 1]
    mix = next(c for c in calls if c[0] == "ffmpeg" and "-filter_complex" in c)
    graph = mix[mix.index("-filter_complex") + 1]
    assert "between(t,8.320,16.640)" in graph and graph.count("adelay=") == 3
    assert report == {"output": str(out), "chunks": 3, "narrated": 3, "bed_kept": 1, "duration": 24.96}
    assert not [p for p in os.listdir(tmp_path) if p.startswith("temp_film_")]


def test_music_narration_profile_exists():
    import music
    spec = {"volume_db": -18.0, "duck": 100.0, "fade_out": 1.0}
    g = music.build_audio_graph(spec, 120.0, profile="narration")
    assert "ratio=12.00" in g and "release=300" in g
