"""music_library: scan into a manifest with an injected decoder, mood pick with
the fallback ladder, use counting, and the search recipes."""

import json
import os

import numpy as np
import pytest

import music_library as ml
from test_beat_grid import click_track, drone


def _lib(tmp_path, tracks):
    """tracks: {relative path: samples}. Writes empty files + returns a decoder."""
    samples = {}
    for rel, y in tracks.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0")
        samples[str(p)] = y

    def decoder(path):
        return samples[str(path)]
    return decoder


@pytest.fixture
def library(tmp_path):
    decoder = _lib(tmp_path, {
        "tense/pulse_a.mp3": click_track(120.0, seconds=130),
        "tense/pulse_b.mp3": click_track(128.0, seconds=70),
        "ominous/drone_a.mp3": drone(seconds=130),
        "epic/hits.mp3": click_track(100.0, seconds=130, accent_every=2),
        "loose.mp3": click_track(110.0, seconds=130),
    })
    (tmp_path / "tense" / "pulse_a.json").write_text(json.dumps({
        "source": "pixabay", "licence": "Pixabay Content License",
        "attribution_required": False, "url": "https://pixabay.com/music/x"}))
    manifest = ml.scan(str(tmp_path), decoder=decoder, normalise=False, log=lambda *_: None)
    return tmp_path, manifest, decoder


def test_scan_writes_a_manifest_with_analysis_and_sidecar(library):
    root, manifest, _ = library
    assert os.path.exists(root / "manifest.json")
    by = {t["path"]: t for t in manifest["tracks"]}
    assert set(by) == {"tense/pulse_a.mp3", "tense/pulse_b.mp3", "ominous/drone_a.mp3",
                       "epic/hits.mp3", "loose.mp3"}
    a = by["tense/pulse_a.mp3"]
    assert a["mood"] == "tense" and a["source"] == "pixabay" and a["licence"]
    assert a["bpm"] == pytest.approx(120, abs=2) and a["beat_confidence"] >= 1.5
    assert a["duration"] == pytest.approx(130, abs=0.1) and a["uses"] == 0
    assert by["loose.mp3"]["mood"] == "any"
    assert by["ominous/drone_a.mp3"]["beat_confidence"] < 1.5
    # A 70 s track is flagged short-ish only under 60 s; under 2 min and not
    # loop-safe is the flag that applies, and nothing was deleted.
    assert os.path.exists(root / "tense" / "pulse_b.mp3")


def test_rescan_keeps_uses_and_skips_unchanged_files(library):
    root, manifest, decoder = library
    ml.record_use("tense/pulse_a.mp3", str(root))
    calls = []

    def counting(path):
        calls.append(path)
        return decoder(path)
    m2 = ml.scan(str(root), decoder=counting, normalise=False, log=lambda *_: None)
    assert calls == []  # every mtime unchanged
    assert {t["path"]: t["uses"] for t in m2["tracks"]}["tense/pulse_a.mp3"] == 1


def test_pick_prefers_the_mood_least_used_and_covering():
    root_manifest = {"tracks": [
        {"path": "tense/a.mp3", "mood": "tense", "duration": 130, "bpm": 120, "beat_confidence": 2.0, "uses": 3, "loop_safe": True},
        {"path": "tense/b.mp3", "mood": "tense", "duration": 130, "bpm": 124, "beat_confidence": 2.0, "uses": 0, "loop_safe": True},
        {"path": "tense/c.mp3", "mood": "tense", "duration": 70, "bpm": 124, "beat_confidence": 2.0, "uses": 0, "loop_safe": True},
    ]}
    got = ml.pick("tense", 120, manifest=root_manifest, music_dir="/lib")
    assert got["track"]["path"] == "tense/b.mp3"
    assert got["fallback"] is False and got["tier"] == "beat" and got["loop"] is False
    assert got["path"].endswith(os.path.join("tense", "b.mp3"))


def test_pick_falls_back_to_the_adjacent_mood_and_carries_a_hint(library):
    root, manifest, _ = library
    got = ml.pick("eerie", 120, manifest=manifest, music_dir=str(root))
    assert got["fallback"] is True
    # eerie -> ominous: the drone fails the beat filters, so under strict
    # filtering the ladder walks on to "any" (110 BPM click, passes).
    assert got["mood_used"] in ("ominous", "any")
    assert got["hint"]["mood"] == "eerie"
    assert "eerie ambient" in got["hint"]["primary"]
    assert got["hint"]["pixabay"].startswith("https://pixabay.com/music/search/")


def test_pick_relaxes_beat_filters_before_giving_up():
    manifest = {"tracks": [
        {"path": "ominous/d.mp3", "mood": "ominous", "duration": 130, "bpm": 70, "beat_confidence": 0.3,
         "uses": 0, "loop_safe": True, "energy": [0.4] * 20},
    ]}
    got = ml.pick("ominous", 120, manifest=manifest, music_dir="/lib")
    assert got is not None and got["tier"] == "energy" and got["fallback"] is False
    assert got["reasons"]["ominous/d.mp3"]


def test_pick_without_beat_sync_ignores_bpm():
    manifest = {"tracks": [
        {"path": "sad/p.mp3", "mood": "sad", "duration": 200, "bpm": 62, "beat_confidence": 0.2, "uses": 0, "loop_safe": False},
    ]}
    got = ml.pick("sad", 120, beat_sync=False, manifest=manifest, music_dir="/lib")
    assert got["tier"] == "free" and got["fallback"] is False


def test_pick_returns_none_on_an_empty_library(tmp_path):
    assert ml.pick("tense", 120, music_dir=str(tmp_path)) is None


def test_pick_uses_the_normalised_copy_when_present():
    manifest = {"tracks": [
        {"path": "tense/a.mp3", "normalised": ".normalised/a.m4a", "mood": "tense", "duration": 130,
         "bpm": 120, "beat_confidence": 2.0, "uses": 0, "loop_safe": True},
    ]}
    got = ml.pick("tense", 120, manifest=manifest, music_dir="/lib")
    assert got["path"].endswith(os.path.join(".normalised", "a.m4a"))


def test_upload_track_files_it_under_the_mood_with_a_sidecar(tmp_path, monkeypatch):
    import music
    samples = click_track(120.0, seconds=130)

    def fake_save(filename, fileobj, music_dir=None):
        os.makedirs(music_dir, exist_ok=True)
        dest = os.path.join(music_dir, "pulse.mp3")
        with open(dest, "wb") as fh:
            fh.write(fileobj.read())
        return {"name": "pulse", "file": "pulse.mp3", "duration": 130.0}
    monkeypatch.setattr(music, "save_track", fake_save)
    monkeypatch.setattr(ml, "decode_audio", lambda path: samples)

    import io
    entry = ml.upload_track("pulse.mp3", io.BytesIO(b"\0" * 10), "tense",
                            meta={"source": "pixabay", "licence": "Pixabay Content License",
                                  "attribution_required": False, "url": "", "attribution": ""},
                            music_dir=str(tmp_path), normalise=False)
    assert entry["path"] == "tense/pulse.mp3" and entry["mood"] == "tense"
    assert entry["bpm"] == pytest.approx(120, abs=2) and entry["source"] == "pixabay"
    assert "beats" not in entry
    side = json.loads((tmp_path / "tense" / "pulse.json").read_text())
    assert side == {"source": "pixabay", "licence": "Pixabay Content License", "attribution_required": False}
    assert ml.library_summary(str(tmp_path))["by_mood"]["tense"] == 1
    with pytest.raises(ValueError):
        ml.upload_track("x.mp3", io.BytesIO(b"\0"), "jazzy", music_dir=str(tmp_path), normalise=False)


def test_music_hint_lists_sources_with_licences():
    h = ml.music_hint("tense")
    names = [s["name"] for s in h["sources"]]
    assert "Pixabay Music" in names and "YouTube Audio Library" in names
    fma = next(s for s in h["sources"] if s["name"] == "Free Music Archive")
    assert "tension" in fma["search"] and fma["attribution_required"] is True


def test_music_hint_and_summary(library):
    root, manifest, _ = library
    hint = ml.music_hint("driving")
    assert hint["bpm"] == [120, 150] and "driving percussion" in hint["primary"]
    assert "trailer" in hint["avoid_terms"]
    assert ml.music_hint("nonsense")["pixabay"] is None
    s = ml.library_summary(str(root), manifest)
    assert s["tracks"] == 5 and s["by_mood"]["tense"] == 2 and s["by_mood"]["any"] == 1
    assert "sad" in s["thin"]


def test_analyse_samples_flags():
    info = ml.analyse_samples(click_track(120.0, seconds=30))
    assert any(f.startswith("short") for f in info["flags"])
    quiet_loud = np.concatenate([np.zeros(int(60 * ml.SR), dtype=np.float32) + 1e-4,
                                 click_track(120.0, seconds=60)])
    info2 = ml.analyse_samples(quiet_loud)
    assert any("dynamic" in f for f in info2["flags"])
