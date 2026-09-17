"""The recap routes end to end through the FastAPI app: a session on local
paths, the prompts, pasting each pass back, forcing past rule errors, the
(faked) silent render, the uploaded and the generated voiceover. Self-host
only: cloud mode answers 404."""

import json
import os

import pytest
from fastapi.testclient import TestClient

import app as app_module
import film_api
import film_prep as fp
from test_movierecap import SPOILERS, STRUCTURE, _plan as _recap_plan

DURATION = 7200.0
LINES = ["Where are we now", "Boston Harbor, that way", "Pull yourself together Teddy",
         "I never had any children", "Your daughter, her name was Rachel"]
CUES = [{"start": 5.0 + 20.0 * i, "end": 8.0 + 20.0 * i, "text": LINES[i % len(LINES)] + f" {i}"}
        for i in range(359)]


def _write_srt(path, cues):
    lines = []
    for i, c in enumerate(cues, 1):
        def ts(t):
            h, rem = divmod(int(t), 3600)
            m, s = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{s:02d},{int((t - int(t)) * 1000):03d}"
        lines += [str(i), f"{ts(c['start'])} --> {ts(c['end'])}", c["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def local(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(app_module, "BILLING_ENABLED", False)
    monkeypatch.setattr(film_api, "_probe_duration", lambda p: DURATION)
    # Threads run inline so the test sees the finished state deterministically.
    monkeypatch.setattr(film_api, "_start_thread", lambda target, *a: target(*a))
    video = tmp_path / "film.mp4"
    video.write_bytes(b"\x00" * 4096)
    srt = tmp_path / "film.srt"
    _write_srt(srt, CUES)
    return {"client": TestClient(app_module.app), "out": out, "video": str(video), "srt": str(srt)}


def _session(local, probe=False, **settings):
    # The fixtures below (SPOILERS, STRUCTURE, _recap_plan) are teaser-shaped;
    # the API default is "story", so tests name the mode they mean.
    settings.setdefault("recap_mode", "teaser")
    r = local["client"].post("/api/film/session", json={
        "kind": "recap", "video_path": local["video"], "subtitle_path": local["srt"],
        "settings": settings, "probe_offset": probe})
    assert r.status_code == 200, r.text
    return r.json()


def _recap_with_plan(local):
    c = local["client"]
    sid = _session(local)["id"]
    c.post(f"/api/movierecap/spoilers/{sid}", json=SPOILERS)
    c.post(f"/api/movierecap/structure/{sid}", json=STRUCTURE)
    assert c.post(f"/api/movierecap/plan/{sid}/1", json=_recap_plan()).json()["ok"]
    return sid


# --- sessions ---------------------------------------------------------------

def test_cloud_mode_hides_every_film_route(local, monkeypatch):
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
    c = local["client"]
    assert c.post("/api/film/session", json={"video_path": "x", "subtitle_path": "y"}).status_code == 404
    assert c.get("/api/film/sessions").status_code == 404
    assert c.get("/api/movierecap/prompt/abc").status_code == 404
    assert c.get("/api/film/voices").status_code == 404
    assert c.get("/film/abc/x.mp4").status_code == 404


def test_session_reads_the_srt_and_persists(local):
    s = _session(local, ratio="9:16")
    assert s["kind"] == "recap" and s["duration"] == DURATION and s["cue_count"] == len(CUES)
    assert s["settings"]["voice"] == "am_michael" and s["settings"]["tts_speed"] == 1.1
    assert os.path.exists(local["out"] / "film" / s["id"] / "session.json")
    assert local["client"].get(f"/api/film/session/{s['id']}").json()["id"] == s["id"]
    lst = local["client"].get("/api/film/sessions").json()["sessions"]
    assert [x["id"] for x in lst] == [s["id"]] and lst[0]["parts_planned"] == 0
    assert local["client"].get("/api/config").json()["filmModules"] is True


def test_session_rejects_missing_or_wrong_files_and_shorts(local):
    c = local["client"]
    r = c.post("/api/film/session", json={"video_path": "/nope.mp4", "subtitle_path": local["srt"]})
    assert r.status_code == 400 and "video not found" in r.json()["detail"]
    r = c.post("/api/film/session", json={"video_path": local["video"], "subtitle_path": local["video"]})
    assert r.status_code == 400 and ".srt" in r.json()["detail"]
    r = c.post("/api/film/session", json={"kind": "shorts", "video_path": local["video"], "subtitle_path": local["srt"]})
    assert r.status_code == 400 and "clip maker" in r.json()["detail"]


def test_offset_probe_runs_and_user_override_wins(local, monkeypatch):
    monkeypatch.setattr(fp, "probe_offset", lambda *a, **k: {"offset": 3.4, "confidence": 0.9, "matches": 20, "votes": 30})
    s = _session(local, probe=True)
    got = local["client"].get(f"/api/film/session/{s['id']}").json()
    assert got["offset"] == 3.4 and got["offset_probe"]["confidence"] == 0.9
    r = local["client"].post(f"/api/film/session/{s['id']}/offset", json={"offset": -1.25})
    assert r.json()["offset"] == -1.25
    d = local["client"].get(f"/api/film/digest/{s['id']}?start=0&end=120").json()
    assert "00:04 Where are we now 0" in d["digest"]  # 5.0 - 1.25 = 3.75 -> 00:04
    assert local["client"].post(f"/api/film/session/{s['id']}/offset", json={"offset": 9999}).status_code == 400


def test_settings_validation(local):
    s = _session(local)
    c = local["client"]
    assert c.post(f"/api/film/session/{s['id']}/settings", json={"ratio": "4:3"}).status_code == 400
    got = c.post(f"/api/film/session/{s['id']}/settings", json={"ratio": "1:1", "voice": "hf_alpha", "bogus": 1}).json()
    assert got["settings"]["ratio"] == "1:1" and got["settings"]["voice"] == "hf_alpha" and "bogus" not in got["settings"]


# --- recap flow -------------------------------------------------------------

def test_recap_flow(local, monkeypatch):
    c = local["client"]
    sid = _session(local)["id"]

    a = c.get(f"/api/movierecap/prompt/{sid}?pass=a").json()
    assert "Runtime: 02:00:00" in a["prompt"] and a["wall"] == 5400.0
    assert c.get(f"/api/movierecap/prompt/{sid}?pass=a&format=text").headers["content-type"].startswith("text/plain")
    assert c.get(f"/api/movierecap/prompt/{sid}?pass=b").status_code == 400  # spoilers first

    # Pasted as chat text with fences.
    r = c.post(f"/api/movierecap/spoilers/{sid}", json={"text": "Here:\n```json\n" + json.dumps(SPOILERS) + "\n```"}).json()
    assert r["ok"] is True and r["protected"][-1]["end"] == DURATION
    r = c.post(f"/api/movierecap/exclusions/{sid}", json={"ranges": [{"start": 100, "end": 130}]}).json()
    assert any(x["tier"] == "manual" for x in r["protected"])

    b = c.get(f"/api/movierecap/prompt/{sid}?pass=b").json()
    assert "Nothing from after 01:30:00" in b["prompt"]
    r = c.post(f"/api/movierecap/structure/{sid}", json={"text": json.dumps(STRUCTURE)}).json()
    assert r["ok"] is True and len(r["structure"]["parts"]) == 3

    cprompt = c.get(f"/api/movierecap/prompt/{sid}?pass=c&part=2").json()
    assert 'Part 2 of three' in cprompt["prompt"]

    bad = _recap_plan()
    bad["chunks"][0]["start"], bad["chunks"][0]["end"] = 105.0, 115.0   # inside the manual exclusion
    r = c.post(f"/api/movierecap/plan/{sid}/1", json=bad).json()
    assert r["ok"] is False and any(e["code"] in ("protected", "order") for e in r["errors"])
    good = _recap_plan()
    good["chunks"][4]["narration"] = "and then he finally sees it " + "watch " * 22
    r = c.post(f"/api/movierecap/plan/{sid}/1", json=good).json()
    assert r["ok"] is True
    assert any(w["code"] == "resolution_language" for w in r["warnings"])
    assert "chunk 5:" in r["warnings_text"]

    budget = c.get(f"/api/movierecap/budget/{sid}").json()
    assert budget["parts_planned"] == [1] and budget["chunks_in_protected"] == 0

    seen = {}

    def fake_render(video, shots, out_path, work, **kw):
        seen.update(kw, n=len(shots))
        open(out_path, "wb").write(b"\x01" * 8)
        return {"shots": len(shots), "muted": kw.get("mute")}
    monkeypatch.setattr(film_api.film_render, "render_silent", fake_render)
    r = c.post(f"/api/movierecap/render/{sid}", json={}).json()
    assert r["renders"] == ["recap-1-silent"]
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["recap-1-silent"]
    assert st["status"] == "completed" and st["report"]["shots"] == 18
    assert seen["mute"] is True and seen["output_format"] == "vertical"
    assert (local["out"] / "film" / sid / st["file"]).exists()
    assert c.post(f"/api/movierecap/render/{sid}", json={"parts": [3]}).status_code == 400
    # A session file is never a deliverable.
    assert c.get(f"/film/{sid}/session.json").status_code == 404


def test_story_mode_is_the_default_and_drops_the_wall(local):
    from test_movierecap import STORY_MAP, STORY_STRUCTURE
    c = local["client"]
    r = c.post("/api/film/session", json={"kind": "recap", "video_path": local["video"],
                                          "subtitle_path": local["srt"], "probe_offset": False})
    sid = r.json()["id"]
    assert r.json()["settings"]["recap_mode"] == "story"
    a = c.get(f"/api/movierecap/prompt/{sid}?pass=a").json()
    assert a["mode"] == "story" and a["wall"] is None and "Spoilers are wanted" in a["prompt"]
    r = c.post(f"/api/movierecap/spoilers/{sid}", json=STORY_MAP).json()
    assert r["ok"] is True and r["protected"] == [] and r["wall"] is None
    r = c.post(f"/api/movierecap/structure/{sid}", json=STORY_STRUCTURE).json()
    assert r["ok"] is True
    cprompt = c.get(f"/api/movierecap/prompt/{sid}?pass=c&part=3").json()
    assert "tell the ending, all of it" in cprompt["prompt"]
    # A part 3 plan deep past 75% is accepted; the wall does not exist here.
    plan = _recap_plan(index=3, start=5500.0, gap=30.0)
    plan["spoiler_self_check"] = ""
    r = c.post(f"/api/movierecap/plan/{sid}/3", json=plan).json()
    assert r["ok"] is True, r["errors_text"]
    # Switching to teaser afterwards makes the same session refuse it.
    c.post(f"/api/film/session/{sid}/settings", json={"recap_mode": "teaser"})
    r = c.post(f"/api/movierecap/plan/{sid}/3", json=plan).json()
    assert r["ok"] is False and any(e["code"] == "wall" for e in r["errors"])


def test_recap_plan_force_never_crosses_the_wall(local):
    c = local["client"]
    sid = _session(local)["id"]
    c.post(f"/api/movierecap/spoilers/{sid}", json=SPOILERS)
    c.post(f"/api/movierecap/structure/{sid}", json=STRUCTURE)
    data = _recap_plan(n=12)                            # too few chunks
    data["chunks"][3]["start"], data["chunks"][3]["end"] = 3125.0, 3135.0   # midpoint range
    data["chunks"][5]["start"], data["chunks"][5]["end"] = 5500.0, 5510.0   # past the wall
    r = c.post(f"/api/movierecap/plan/{sid}/1", json={"json": data}).json()
    assert r["ok"] is False and r["forceable"] is True
    r = c.post(f"/api/movierecap/plan/{sid}/1", json={"json": data, "force": True}).json()
    assert r["ok"] is True and r["forced"] is True and r["kept_chunks"] == 10
    stored = c.get(f"/api/film/session/{sid}").json()["recap"]["plans"]["1"]
    assert stored["forced"] is True and len(stored["chunks"]) == 10
    assert all(ch["end"] <= 5400 for ch in stored["chunks"])
    r = c.post(f"/api/movierecap/plan/{sid}/1", json={"json": {"index": "x"}, "force": True}).json()
    assert r["ok"] is False and r["forceable"] is False


# --- uploaded voiceover -----------------------------------------------------

def test_recap_voiceover_uses_compilation_alignment(local, monkeypatch):
    c = local["client"]
    sid = _recap_with_plan(local)
    import compilation
    plan_words = [w for c_ in _recap_plan()["chunks"] for w in c_["narration"].split()]
    words = [{"w": w, "s": i * 150.0 / len(plan_words), "e": (i + 0.8) * 150.0 / len(plan_words)}
             for i, w in enumerate(plan_words)]
    monkeypatch.setattr(compilation, "transcribe_vo", lambda p: words)
    monkeypatch.setattr(compilation, "pad_and_split", lambda vo, aligned, pad, wd: [
        {"path": f"{wd}/vo_{i}.wav", "start": a["start"], "end": a["end"],
         "duration": a["end"] - a["start"], "shot_ref": a["shot_ref"]} for i, a in enumerate(aligned)])
    captured = {}

    def fake_narrated(video, segments, out_path, work, **kw):
        captured["segments"] = segments
        open(out_path, "wb").write(b"\x02" * 8)
        return {"output": out_path, "chunks": len(segments)}
    monkeypatch.setattr(film_api.film_render, "render_narrated", fake_narrated)

    r = c.post(f"/api/movierecap/voiceover/{sid}/1", files={"file": ("vo.wav", b"RIFF" + b"\x00" * 64, "audio/wav")})
    assert r.status_code == 200, r.text
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["recap-1"]
    assert st["status"] == "completed", st
    assert st["vo_check"]["ok"] is True
    segs = captured["segments"]
    assert len(segs) == 18 and segs[0]["start"] == pytest.approx(60.0, abs=0.01)
    assert segs[0]["voice_start"] is not None and segs[0]["chunk"]["path"].endswith("vo_0.wav")
    assert c.post(f"/api/movierecap/voiceover/{sid}/1", files={"file": ("vo.txt", b"x", "text/plain")}).status_code == 400


def test_recap_voiceover_refuses_a_recording_far_off_target(local, monkeypatch):
    c = local["client"]
    sid = _recap_with_plan(local)
    import compilation
    monkeypatch.setattr(compilation, "transcribe_vo", lambda p: [{"w": "x", "s": 0.0, "e": 210.0}])
    c.post(f"/api/movierecap/voiceover/{sid}/1", files={"file": ("vo.mp3", b"\x00" * 64, "audio/mpeg")})
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["recap-1"]
    assert st["status"] == "failed" and "+40%" in st["error"]


# --- generated voiceover ----------------------------------------------------

def test_film_voices_endpoint_offline_and_online(local, monkeypatch):
    import film_voice
    c = local["client"]
    monkeypatch.setattr(film_voice, "health", lambda client=None: {
        "online": False, "url": "http://127.0.0.1:8880", "voices": 0, "error": "refused",
        "start_command": film_voice.START_COMMAND})
    r = c.get("/api/film/voices").json()
    assert r["online"] is False and r["groups"] == {} and "start-cpu" in r["start_command"]
    monkeypatch.setattr(film_voice, "health", lambda client=None: {
        "online": True, "url": "x", "voices": 2, "device": "cpu", "start_command": ""})
    monkeypatch.setattr(film_voice, "voices", lambda client=None, force=False: [
        film_voice.describe_voice("am_michael"), film_voice.describe_voice("hf_alpha")])
    r = c.get("/api/film/voices").json()
    assert r["online"] is True and set(r["groups"]) == {"English (US)", "Hindi"}
    assert r["default"] == "am_michael"


def test_recap_narrate_preview(local, monkeypatch):
    import film_voice
    c = local["client"]
    sid = _recap_with_plan(local)
    seen = {}

    def fake_synth(text, voice, speed, out, client=None):
        seen.update(text=text, voice=voice, speed=speed, out=out)
        open(out, "wb").write(b"RIFF")
        return {"path": out, "duration": 8.1, "speed": speed}
    monkeypatch.setattr(film_voice, "synthesize", fake_synth)
    r = c.post(f"/api/movierecap/narrate/{sid}/1/preview", json={"voice": "hf_alpha", "speed": 1.1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == f"/film/{sid}/preview_part1_hf_alpha_110.wav" and body["duration"] == 8.1
    assert seen["voice"] == "hf_alpha" and seen["text"].startswith("watch")
    assert (local["out"] / "film" / sid / "preview_part1_hf_alpha_110.wav").exists()
    assert c.post(f"/api/movierecap/narrate/{sid}/1/preview", json={"voice": "../x"}).status_code == 400

    def offline(*a, **k):
        raise film_voice.VoiceOffline("refused")
    monkeypatch.setattr(film_voice, "synthesize", offline)
    r = c.post(f"/api/movierecap/narrate/{sid}/1/preview", json={})
    assert r.status_code == 503 and "start-cpu" in r.json()["detail"]


def test_film_voices_start_endpoint(local, monkeypatch):
    import film_voice
    monkeypatch.setattr(film_voice, "ensure_server", lambda: {
        "online": True, "url": "x", "voices": 1, "device": "cpu", "start_command": "", "started": True})
    monkeypatch.setattr(film_voice, "voices", lambda client=None, force=False: [film_voice.describe_voice("am_michael")])
    r = local["client"].post("/api/film/voices/start").json()
    assert r["online"] is True and r["started"] is True and "English (US)" in r["groups"]
    monkeypatch.setattr(film_voice, "ensure_server", lambda: {
        "online": False, "started": False, "reason": "no checkout", "start_command": "cmd"})
    r = local["client"].post("/api/film/voices/start").json()
    assert r["online"] is False and r["reason"] == "no checkout" and r["groups"] == {}


def _narrated_part(local, monkeypatch):
    """A completed generated-voiceover render for part 1, with a real base file."""
    import film_voice
    c = local["client"]
    sid = _recap_with_plan(local)
    monkeypatch.setattr(film_voice, "health", lambda client=None: {"online": True, "start_command": ""})

    def fake_narrate(plan, part, protected, duration, voice, speed, workdir, **kw):
        segs, t = [], 0.0
        for ch in plan.chunks:
            segs.append({"start": ch.start, "end": ch.end, "shot_start": t, "shot_length": 8.32, "voice_start": t + 0.15,
                         "silent": False, "keep_audio": False, "flip": False, "chunk": {"path": "l.wav", "duration": 7.0}})
            t += 8.32
        return {"segments": segs, "report": [], "total_finished": t, "voice": voice, "speed": speed,
                "overruns": 0, "words": 480}
    monkeypatch.setattr(film_voice, "narrate_plan", fake_narrate)

    def fake_render(video, segments, out_path, work, **kw):
        open(out_path, "wb").write(b"\x03" * 8)
        return {"output": out_path, "chunks": len(segments), "duration": 149.76}
    monkeypatch.setattr(film_api.film_render, "render_narrated", fake_render)
    assert c.post(f"/api/movierecap/narrate/{sid}/1", json={}).status_code == 200
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["recap-1"]
    assert st["status"] == "completed" and st["base_file"] == st["file"]
    return sid, st


def test_recap_part_transcript_comes_from_the_narration(local, monkeypatch):
    c = local["client"]
    sid, st = _narrated_part(local, monkeypatch)
    assert len(st["narration_cues"]) == 18
    assert st["narration_cues"][0] == {"start": 0.15, "end": 7.15, "text": " ".join(["watch"] * 27)}
    t = c.get(f"/api/movierecap/part/{sid}/1/transcript").json()
    assert t["durationSec"] == 149.76 and t["language"] == "en"
    assert t["captions"][0]["text"] == "watch" and t["captions"][0]["startMs"] == 150
    # Words are spread across each line: the 27th word ends where the line ends.
    assert t["captions"][26]["endMs"] == 7150
    assert c.get(f"/api/movierecap/part/{sid}/2/transcript").status_code == 400


def test_recap_part_captions_and_overlays_relayer(local, monkeypatch):
    import overlays as _ov
    c = local["client"]
    sid, st = _narrated_part(local, monkeypatch)
    base = local["out"] / "film" / sid / st["base_file"]
    calls = []

    def fake_apply_overlays(video, items, out_path, overlays_dir=None):
        calls.append(("overlays", os.path.basename(video), len(items), os.path.basename(out_path)))
        open(out_path, "wb").write(b"ov")
        return True
    monkeypatch.setattr(_ov, "apply_overlays", fake_apply_overlays)

    def fake_burn(video, transcript, start, end, style, split_ranges=None):
        calls.append(("captions", os.path.basename(video), style["preset"],
                      sum(len(s["words"]) for s in transcript["segments"]), end))
        out = os.path.join(os.path.dirname(video), f"subtitled_9_{os.path.basename(video)}")
        open(out, "wb").write(b"cap")
        return out
    monkeypatch.setattr(app_module, "_burn_styled_captions", fake_burn)

    # Captions first: burned straight onto the base.
    r = c.post(f"/api/movierecap/part/{sid}/1/captions", json={"preset": "bold_white", "overrides": {"offset_y": 10, "bogus": 1}})
    assert r.status_code == 200, r.text
    assert r.json()["file"].startswith("subtitled_9_") and r.json()["caption_style"]["overrides"] == {"offset_y": 10.0}
    assert calls[-1][0] == "captions" and calls[-1][1] == st["base_file"] and calls[-1][3] == 27 * 18
    # Unknown preset refused.
    assert c.post(f"/api/movierecap/part/{sid}/1/captions", json={"preset": "nope"}).status_code == 400

    # Then overlays: re-derived from the base, captions go back ON TOP of the
    # overlay pass, and the earlier subtitled file is gone.
    items = [{"type": "text", "text": "TG FILMS", "x": 0.5, "y": 0.9, "w": 0.4}]
    r = c.post(f"/api/movierecap/part/{sid}/1/overlays", json={"overlays": items})
    assert r.status_code == 200, r.text
    kinds = [k[0] for k in calls[-2:]]
    assert kinds == ["overlays", "captions"]
    assert calls[-2][1] == st["base_file"] and calls[-2][3].startswith("ov_")
    assert calls[-1][1].startswith("ov_")
    served = r.json()["file"]
    assert served.startswith("subtitled_9_ov_")
    files = os.listdir(local["out"] / "film" / sid)
    assert served in files and base.exists()
    assert sum(1 for f in files if f.startswith("subtitled_")) == 1
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["recap-1"]
    assert st["file"] == served and len(st["overlays"]) == 1 and st["caption_style"]["preset"] == "bold_white"

    # Removing captions leaves the overlay layer as the served file.
    r = c.post(f"/api/movierecap/part/{sid}/1/captions", json={"preset": None}).json()
    assert r["file"].startswith("ov_") and r["caption_style"] is None
    # Removing overlays too returns to the bare narrated render.
    r = c.post(f"/api/movierecap/part/{sid}/1/overlays", json={"overlays": []}).json()
    assert r["file"] == st["base_file"]
    # Edited caption words are burned verbatim.
    r = c.post(f"/api/movierecap/part/{sid}/1/captions", json={
        "preset": "bold_white", "words": [{"text": "Hello", "startMs": 100, "endMs": 600}, {"text": "there", "startMs": 700, "endMs": 1200}]})
    assert r.status_code == 200 and calls[-1][3] == 2 and calls[-1][4] == pytest.approx(2.2)


def test_recap_narrate_generates_fits_and_renders(local, monkeypatch):
    import film_voice
    c = local["client"]
    sid = _recap_with_plan(local)
    monkeypatch.setattr(film_voice, "health", lambda client=None: {"online": True, "start_command": ""})
    captured = {}

    def fake_narrate(plan, part, protected, duration, voice, speed, workdir, **kw):
        captured.update(voice=voice, speed=speed, workdir=workdir, n=len(plan.chunks))
        kw["log"]("line 1/18")
        segs = [{"start": 60.0, "end": 70.4, "shot_start": 0.0, "shot_length": 8.32, "voice_start": 0.15,
                 "silent": False, "keep_audio": False, "flip": False, "chunk": {"path": "l.wav", "duration": 7.0}}]
        return {"segments": segs, "report": [{"chunk": 1, "action": "fit", "voice_seconds": 7.0}],
                "total_finished": 150.0, "voice": voice, "speed": speed, "overruns": 0, "words": 480}
    monkeypatch.setattr(film_voice, "narrate_plan", fake_narrate)

    def fake_render(video, segments, out_path, work, **kw):
        open(out_path, "wb").write(b"\x03" * 8)
        return {"output": out_path, "chunks": len(segments)}
    monkeypatch.setattr(film_api.film_render, "render_narrated", fake_render)

    r = c.post(f"/api/movierecap/narrate/{sid}/1", json={"voice": "am_michael", "speed": 1.05})
    assert r.status_code == 200, r.text
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["recap-1"]
    assert st["status"] == "completed", st
    assert st["generated"] is True and st["voice"] == "am_michael" and st["tts_speed"] == 1.05
    assert st["fit"][0]["action"] == "fit" and st["overruns"] == 0 and st["words"] == 480
    assert captured["n"] == 18 and captured["workdir"].endswith("vo_part1_am_michael")
    assert any("line 1/18" in line for line in st["logs"])
    sess = c.get(f"/api/film/session/{sid}").json()
    assert sess["settings"]["voice"] == "am_michael" and sess["settings"]["tts_speed"] == 1.05
    monkeypatch.setattr(film_voice, "health", lambda client=None: {"online": False, "start_command": "cmd"})
    assert c.post(f"/api/movierecap/narrate/{sid}/1", json={}).status_code == 503


# --- movie shorts (montage through the clip maker) ---------------------------

def _shorts_plan():
    # CUES are 3 s long, 20 s apart: every line is its own 3.6 s cut, so five
    # lines per scene and three scenes is 54 s, inside the 45-180 s range.
    def scene(lo, hold=0.0):
        return {"lines": list(range(lo, lo + 5)), "hold_after": hold, "note": f"lines {lo}+"}
    return {"shorts": [
        {"title": "How a top spy babysat 3 kids", "hook": "He fought dictators. Then breakfast.",
         "mood": "funny", "ending": "the ally pulls a gun",
         "scenes": [scene(20), scene(0, 1.5), scene(40)]},
        {"title": "The worst first date in cinema", "hook": "Would you stay?", "mood": "tense",
         "ending": "she walks out", "scenes": [scene(60), scene(80), scene(100)]},
    ]}


def test_shorts_prompt_lists_lines_with_ids(local):
    c = local["client"]
    sid = _session(local, shorts_count=4)["id"]
    p = c.get(f"/api/movieshorts/prompt/{sid}").json()
    assert p["count"] == 4 and "4 short vertical videos" in p["prompt"]
    assert "#0 00:05 Where are we now 0" in p["prompt"]
    assert "#358 " in p["prompt"]
    assert c.get(f"/api/movieshorts/prompt/{sid}?format=text").headers["content-type"].startswith("text/plain")


def test_shorts_plan_validates_stores_previews_and_can_be_forced(local):
    c = local["client"]
    sid = _session(local)["id"]
    assert c.post(f"/api/movieshorts/render/{sid}", json={}).status_code == 400   # nothing planned

    bad = _shorts_plan()
    bad["shorts"][1]["scenes"][0]["lines"] = [20, 21, 22, 23, 24]            # reused from short 1
    r = c.post(f"/api/movieshorts/plan/{sid}", json={"text": "```json\n" + json.dumps(bad) + "\n```"}).json()
    assert r["ok"] is False and r["forceable"] is True
    assert any(e["code"] == "reused_line" and e["short"] == 1 for e in r["errors"])
    assert "short 2 scene 1" in r["errors_text"]
    assert c.get(f"/api/film/session/{sid}").json()["shorts"]["previews"] == []

    r = c.post(f"/api/movieshorts/plan/{sid}", json={"text": json.dumps(bad), "force": True}).json()
    assert r["ok"] is True and r["forced"] is True and len(r["previews"]) == 2
    sess = c.get(f"/api/film/session/{sid}").json()
    assert sess["shorts"]["plan"]["forced"] is True

    r = c.post(f"/api/movieshorts/plan/{sid}", json=_shorts_plan()).json()
    assert r["ok"] is True and r["errors"] == [] and r["forced"] is False
    p = r["previews"][0]
    assert p["cuts"] == 15 and p["seconds"] == 55.5                      # 15 x 3.6 + 1.5 hold
    assert [sc["index"] for sc in p["scenes"]] == [0, 1, 2]
    assert p["segments"][0]["start"] == 404.75                            # scene order, not film order
    assert p["scenes"][1]["hold_after"] == 1.5 and "Where are we now 0" in p["scenes"][1]["text"]
    assert c.get("/api/film/sessions").json()["sessions"][0]["shorts_planned"] == 2

    # a hand trim replaces the EDL
    r = c.post(f"/api/movieshorts/segments/{sid}/0", json={"segments": [{"start": 10, "end": 20}, {"start": 30, "end": 35}]}).json()
    assert r["preview"]["cuts"] == 2 and r["preview"]["seconds"] == 15.0 and r["preview"]["edited"] is True
    assert c.post(f"/api/movieshorts/segments/{sid}/0", json={"segments": [{"start": 5, "end": 5.1}]}).status_code == 400
    assert c.post(f"/api/movieshorts/segments/{sid}/7", json={"segments": [{"start": 1, "end": 2}]}).status_code == 404


def test_shorts_render_registers_an_ordinary_clip_job(local, monkeypatch, tmp_path):
    import recut
    c = local["client"]
    sid = _session(local, shorts_ratio="1:1", watermark_text="made by sona", music_db=-12)["id"]
    assert c.post(f"/api/movieshorts/plan/{sid}", json=_shorts_plan()).json()["ok"]

    lib = tmp_path / "music"
    lib.mkdir()
    (lib / "verclub-upbeat.mp3").write_bytes(b"\x00" * 16)
    (lib / "empire-of-shadows-cinematic.mp3").write_bytes(b"\x00" * 16)
    import music as _music
    monkeypatch.setattr(_music, "MUSIC_DIR", str(lib))
    monkeypatch.setattr(_music, "probe_duration", lambda p, timeout=30: 90.0)

    seen = {}

    def fake_recut(*, input_path, segments, output_dir, clean_name, **kw):
        seen.update(input_path=input_path, n=len(segments), fmt=kw.get("output_format"),
                    words=sum(len(s["words"]) for s in kw["captions_transcript"]["segments"]),
                    hooks=(kw.get("effects") is not None, kw.get("captioner") is not None))
        name = f"recut_1_abc_{clean_name}"
        open(os.path.join(output_dir, name), "wb").write(b"\x02" * 8)
        return f"subtitled_2_{name}", name
    monkeypatch.setattr(recut, "perform_recut", fake_recut)

    r = c.post(f"/api/movieshorts/render/{sid}", json={"index": 1}).json()
    assert r["renders"] == ["shorts-1"]
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["shorts-1"]
    assert st["status"] == "completed", st
    job_id = st["job_id"]
    assert st["output"] == f"/videos/{job_id}/subtitled_2_recut_1_abc_The_worst_first_date_in_cinema_{sid[:6]}_clip_1.mp4"
    assert st["cuts"] == 15 and st["seconds"] == 54.0
    assert seen["input_path"] == local["video"] and seen["n"] == 15 and seen["fmt"] == "square"
    assert seen["words"] > 0 and seen["hooks"] == (True, True)

    # An ordinary job: status answers, the clip carries the montage's specs,
    # the editor can find the film as its source.
    job = c.get(f"/api/status/{job_id}").json()
    assert job["status"] == "completed"
    clip = job["result"]["clips"][0]
    assert clip["video_url"] == st["output"] and clip["picked_by"] == "claude"
    assert len(clip["recipe"]["segments"]) == 15 and clip["output_format"] == "square"
    assert clip["music"] == {"track": "empire-of-shadows-cinematic.mp3", "volume_db": -12.0, "duck": 70.0,
                             "start": 0.0, "fade_out": 1.0, "profile": "dialogue"}
    assert clip["overlays"][0]["text"] == "made by sona"
    assert clip["caption_style"]["overrides"]["position"] == "center"
    assert clip["film_session"] == sid and clip["montage"]["mood"] == "tense"
    meta = json.load(open(next((local["out"] / job_id).glob("*_metadata.json"))))
    assert meta["source_path"] == local["video"] and meta["film_session"] == sid
    assert app_module._locate_source(job_id) == local["video"]

    # every short at once skips the one already running/done only when running
    r = c.post(f"/api/movieshorts/render/{sid}", json={}).json()
    assert r["renders"] == ["shorts-0", "shorts-1"]
    assert c.post(f"/api/movieshorts/render/{sid}", json={"index": 9}).status_code == 404


def test_shorts_settings_are_validated(local):
    c = local["client"]
    sid = _session(local)["id"]
    assert c.post(f"/api/film/session/{sid}/settings", json={"shorts_ratio": "4:5"}).status_code == 400
    assert c.post(f"/api/film/session/{sid}/settings", json={"music_track": "nope.mp3"}).status_code == 400
    assert c.post(f"/api/film/session/{sid}/settings", json={"caption_preset": "nope"}).status_code == 400
    got = c.post(f"/api/film/session/{sid}/settings", json={"shorts_count": 4, "watermark_text": "x", "music_track": None}).json()
    assert got["settings"]["shorts_count"] == 4 and got["settings"]["music_track"] is None
