"""beat_grid: tempo, beats, confidence, downbeats and the beat-locked shot
schedule, on a synthetic click track (no audio files, no ffmpeg)."""

import numpy as np
import pytest

import beat_grid as bg

SR = bg.SR


def click_track(bpm=120.0, seconds=40.0, accent_every=4, noise=0.02, seed=1):
    """Decaying noise bursts on every beat, louder on the downbeat."""
    rng = np.random.default_rng(seed)
    y = rng.normal(0, noise, int(seconds * SR)).astype(np.float32)
    period = 60.0 / bpm
    burst = (rng.normal(0, 1, int(0.03 * SR)) * np.exp(-np.linspace(0, 8, int(0.03 * SR)))).astype(np.float32)
    i = 0
    t = 0.05
    while t < seconds - 0.1:
        s = int(t * SR)
        gain = 1.0 if i % accent_every == 0 else 0.45
        y[s:s + len(burst)] += gain * burst
        t += period
        i += 1
    return y


def drone(seconds=40.0, seed=2):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * 55 * t) * (1 + 0.1 * np.sin(2 * np.pi * 0.1 * t))
            + rng.normal(0, 0.005, len(t))).astype(np.float32)


def test_analyse_finds_tempo_beats_and_downbeats_on_a_click_track():
    grid = bg.analyse(click_track(120.0))
    assert grid["bpm"] == pytest.approx(120.0, abs=1.5)
    assert grid["beat_confidence"] >= bg.BEAT_CONFIDENCE_MIN
    beats = np.array(grid["beats"])
    gaps = np.diff(beats)
    assert np.median(gaps) == pytest.approx(0.5, abs=0.02)
    # Downbeats: the accented clicks were beats 0, 4, 8... starting at 0.05 s.
    down = beats[grid["downbeat_phase"]::4]
    assert np.allclose((down - 0.05) % 2.0, 0, atol=0.06) or np.allclose((down - 0.05) % 2.0, 2.0, atol=0.06)
    assert bg.tier_for(grid) == "beat"
    assert len(grid["energy"]) > 10


def test_analyse_does_not_invent_a_beat_in_a_drone():
    grid = bg.analyse(drone())
    assert bg.tier_for(grid) in ("energy", "free")


def test_tempo_prior_keeps_the_pick_off_the_half_tempo():
    grid = bg.analyse(click_track(100.0, accent_every=2))
    assert grid["bpm"] == pytest.approx(100.0, abs=2.0)


def test_phase_lock_statistic():
    beats = np.arange(0, 60, 0.5)
    locked = beats[3::3] + 0.01
    assert bg.phase_lock(locked, beats, 0.5) > 0.95
    rng = np.random.default_rng(0)
    assert bg.phase_lock(rng.uniform(0, 60, 80), beats, 0.5) < 0.35


@pytest.mark.parametrize("bpm,ks", [(90, [2]), (100, [2, 3]), (120, [3]), (128, [3]), (140, [3, 4]), (150, [3, 4])])
def test_usable_k(bpm, ks):
    assert bg.usable_k(bpm) == ks


def _beats(n=8, length=9.375):
    out, t = [], 100.0
    for i in range(n):
        out.append({"start": t, "end": t + length, "weight": "key" if i in (1, 5) else "normal",
                    "captions": [{"at": t + 3.0, "text": "x"}]})
        t += length + 30.0
    return out


GRID_120 = {"bpm": 120.0, "period": 0.5, "beat_confidence": 2.4, "downbeat_phase": 0,
            "beats": list(np.arange(0, 200, 0.5)), "energy": [0.5] * 400}
# 100 BPM has two usable k (2 and 3): the sweet spot where shot length can
# vary while staying locked. At 120 only k=3 fits and every shot is 1.5 s.
GRID_100 = {"bpm": 100.0, "period": 0.6, "beat_confidence": 2.4, "downbeat_phase": 0,
            "beats": list(np.arange(0, 240, 0.6)), "energy": [0.5] * 480}


@pytest.mark.parametrize("grid,T", [(GRID_120, 0.5), (GRID_100, 0.6)])
def test_schedule_locks_every_boundary_to_the_grid_in_finished_time(grid, T):
    speed = 1.25
    res = bg.schedule(_beats(), grid, speed=speed)
    assert res["tier"] == "beat"
    cursor = 0.0
    boundaries = []
    for i, durations in sorted(res["schedule"].items()):
        beat = _beats()[i]
        assert sum(durations) <= beat["end"] - beat["start"] + 1e-6
        for d in durations[:-1]:
            assert 1.2 * speed - 1e-6 <= d <= 1.8 * speed + 1e-6
            cursor += d / speed
            boundaries.append(cursor)
        # The last shot of a beat may give or take one beat to land the next
        # story beat on a downbeat; never more.
        assert (1.2 - T) * speed - 1e-6 <= durations[-1] <= (1.8 + T) * speed + 1e-6
        cursor += durations[-1] / speed
    # Boundaries in finished seconds sit on multiples of the beat period.
    frac = np.array(boundaries) % T
    assert np.all(np.minimum(frac, T - frac) < 1e-6)
    r = bg.phase_lock(np.array(boundaries), np.array(grid["beats"]), T)
    assert r > 0.99
    assert res["report"]["boundaries"] == len(boundaries)
    assert res["report"]["downbeat_starts"] >= 2  # both key beats at least
    if len(bg.usable_k(grid["bpm"])) > 1:
        # Shot lengths vary (k cycles) instead of every shot being identical.
        assert len({round(d, 3) for ds in res["schedule"].values() for d in ds}) > 1


def test_schedule_key_line_shot_runs_a_beat_longer():
    grid = dict(GRID_120, bpm=100.0, period=0.6)  # k in {2, 3}: room to add one
    beat = {"start": 0.0, "end": 12.0, "weight": "normal", "captions": [{"at": 6.1}]}
    res = bg.schedule([beat], grid, speed=1.25)
    durations = res["schedule"][0]
    t = 0.0
    for d in durations:
        if t <= 6.1 < t + d:
            assert d >= max(durations) - 1e-6
        t += d


def test_schedule_dialogue_guard_moves_a_boundary_off_a_word():
    speed = 1.25
    res0 = bg.schedule(_beats(1), GRID_120, speed=speed)
    d0 = res0["schedule"][0]
    first_boundary = 100.0 + d0[0]
    words = [{"start": first_boundary - 0.05, "end": first_boundary + 0.08}]
    res = bg.schedule(_beats(1), GRID_120, speed=speed, words=words)
    d = res["schedule"][0]
    b = 100.0 + d[0]
    assert res["report"]["nudged"] == 1
    assert b <= words[0]["start"] + 1e-6 or b >= words[0]["end"] - 1e-6
    assert sum(d) == pytest.approx(sum(d0), abs=1e-6)  # the beat still ends on the grid
    # strict mode leaves it alone
    res_strict = bg.schedule(_beats(1), GRID_120, speed=speed, words=words, strictness="strict")
    assert res_strict["report"]["nudged"] == 0


def test_schedule_moves_a_whole_beat_when_the_word_is_long():
    speed = 1.25
    d0 = bg.schedule(_beats(1), GRID_120, speed=speed)["schedule"][0]
    b = 100.0 + d0[0]
    words = [{"start": b - 0.4, "end": b + 0.4}]  # 0.8 s word: nudging cannot clear it
    res = bg.schedule(_beats(1), GRID_120, speed=speed, words=words)
    assert res["report"]["moved_a_beat"] + res["report"]["nudged"] >= 1
    d = res["schedule"][0]
    assert sum(d) == pytest.approx(sum(d0), abs=1e-6)


def test_schedule_energy_tier_scales_shot_length_with_loudness():
    quiet = {"bpm": 70.0, "beat_confidence": 0.4, "energy": [0.0] * 400}
    loud = {"bpm": 70.0, "beat_confidence": 0.4, "energy": [1.0] * 400}
    speed = 1.25
    q = bg.schedule(_beats(2), quiet, speed=speed)
    l = bg.schedule(_beats(2), loud, speed=speed)
    assert q["tier"] == "energy" and l["tier"] == "energy"
    mean_q = np.mean([d for ds in q["schedule"].values() for d in ds]) / speed
    mean_l = np.mean([d for ds in l["schedule"].values() for d in ds]) / speed
    assert mean_q > mean_l
    assert 1.2 - 1e-6 <= mean_l <= 1.8 + 1e-6


def test_schedule_free_tier_returns_no_durations():
    res = bg.schedule(_beats(2), {"bpm": 0.0}, speed=1.25)
    assert res["tier"] == "free" and res["schedule"] == {}
