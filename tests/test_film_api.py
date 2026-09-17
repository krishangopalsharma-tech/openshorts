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
