"""Climax tail layers: muted recut segments, the beat punch filters, the
music bed's climax swell, drop alignment and the numpy beat/drop analysis."""

import numpy as np

import cinematic
import music
import recut


# --- recut: picture-only segments ------------------------------------------------

def test_mute_flag_survives_normalize_and_drops_words_from_captions():
    segs = recut.normalize_segments([{"start": 0, "end": 2}, {"start": 10, "end": 11, "mute": True}])
    assert "mute" not in segs[0] and segs[1]["mute"] is True
    transcript = {"segments": [{"words": [{"word": " hi", "start": 0.5, "end": 0.9},
                                          {"word": " boom", "start": 10.2, "end": 10.6}]}]}
    v = recut.virtual_transcript(transcript, segs)
    assert [w["word"] for w in v["segments"][0]["words"]] == [" hi"]
    assert v["segments"][1]["words"] == [] and v["segments"][1]["start"] == 2.0


# --- cinematic punches -------------------------------------------------------------

def test_punches_are_normalized_and_make_the_look_non_noop():
    e = cinematic.normalize({"punches": [5, "6.5", "x", -1, 5.0]})
    assert e["punches"] == [5.0, 6.5]
    assert not cinematic.is_noop({"punches": [1.0]})
    assert cinematic.is_noop({"punches": []})
    assert "punches" not in cinematic.normalize({})


def test_punch_filters_keep_the_frame_size_and_flash_on_each_time():
    graph = cinematic.build_filter_complex({"punches": [2.0, 3.0]}, 1080, 1920)
    assert "eval=frame" in graph and "crop=1080:1920" in graph
    assert graph.count("color=white@0.85") == 2 and "between(t,2.000,2.034)" in graph
    assert graph.endswith("copy[fx]")


def test_gritty_grade_exists():
    assert "unsharp" in cinematic.COLOR_GRADES["gritty"]
    assert cinematic.normalize({"color_grade": "gritty"})["color_grade"] == "gritty"


# --- music climax ------------------------------------------------------------------

def test_climax_spec_is_normalized():
    spec = music.normalize({"track": "a.mp3", "volume_db": -16, "climax_at": 50.5, "climax_db": 6, "drop_at": 18.4})
    assert spec["climax_at"] == 50.5 and spec["climax_db"] == 0.0 and spec["drop_at"] == 18.4
    spec = music.normalize({"track": "a.mp3", "volume_db": -16, "climax_at": 50.5, "climax_db": -30})
    assert spec["climax_db"] == -16.0 and "drop_at" not in spec          # never below the resting bed
    assert "climax_at" not in music.normalize({"track": "a.mp3", "climax_at": None})


def test_climax_graph_mutes_the_clip_and_swells_the_bed():
    spec = music.normalize({"track": "a.mp3", "volume_db": -16, "duck": 70, "climax_at": 50.0, "climax_db": -4})
    g = music.build_audio_graph(spec, 56.0, voice=True, profile="dialogue")
    assert "[0:a]volume=0:enable='gte(t,50.000)',asplit=2" in g
    assert "clip((t-49.650)/0.350,0,1)" in g and "eval=frame" in g
    gain = 10 ** (12 / 20.0) - 1
    assert f"1+{gain:.4f}*" in g
    plain = music.build_audio_graph(music.normalize({"track": "a.mp3"}), 56.0)
    assert "enable=" not in plain and "eval=frame" not in plain


def test_aligned_start_wraps_around_the_loop():
    assert music.aligned_start(18.0, 50.0, 120.0) == 88.0       # (18 - 50) mod 120
    assert music.aligned_start(60.0, 50.0, 120.0) == 10.0
    assert music.aligned_start(60.0, 50.0, None) == 10.0


# --- beat / drop analysis ------------------------------------------------------------

def _click_track(bpm=120.0, drop=12.0, seconds=30.0):
    sr = music.ANALYSIS_SR
    t = np.arange(int(seconds * sr)) / sr
    y = 0.01 * np.sin(2 * np.pi * 80 * t) * np.where(t < drop, 1, 6)
    for b in np.arange(0, seconds, 60.0 / bpm):
        i = int(b * sr)
        y[i:i + 300] += np.hanning(600)[300:] * (0.15 if b < drop else 0.9)
    return y.astype(np.float32)


def test_analysis_finds_tempo_and_drop():
    a = music.analyze_samples(_click_track(bpm=120, drop=12))
    assert abs(a["beat_period"] - 0.5) < 0.01 and abs(a["drop_at"] - 12.0) < 0.1
    a = music.analyze_samples(_click_track(bpm=90, drop=16))
    assert abs(a["beat_period"] - 60 / 90) < 0.015 and abs(a["drop_at"] - 16.0) < 0.1


def test_analysis_of_short_or_flat_audio_returns_nones():
    assert music.analyze_samples(np.zeros(1000, np.float32))["beat_period"] is None
    flat = music.analyze_samples(np.full(music.ANALYSIS_SR * 30, 0.1, np.float32))
    assert flat["drop_at"] is None


def test_analyze_track_caches_by_file(tmp_path, monkeypatch):
    track = tmp_path / "t.wav"
    track.write_bytes(b"x" * 10)
    calls = []
    monkeypatch.setattr(music, "_decode_mono", lambda p: calls.append(p) or _click_track())
    first = music.analyze_track(str(track))
    second = music.analyze_track(str(track))
    assert first == second and len(calls) == 1 and (tmp_path / ".analysis.json").exists()
    assert music.analyze_track(str(tmp_path / "missing.wav")) == {}
