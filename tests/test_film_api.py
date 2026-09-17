"""The film routes end to end through the FastAPI app: a session on local
paths, the prompts, pasting each pass back, the shot plan, a (faked) render
and the served result. Self-host only: cloud mode answers 404."""

import json
import os

import pytest
from fastapi.testclient import TestClient

import app as app_module
import film_api
import film_prep as fp
import movierecap as mr
import movieshorts as ms
from test_movieshorts import CUES as SYNTH_CUES, HOOK, _beats
from test_movierecap import SPOILERS, STRUCTURE, _plan as _recap_plan

DURATION = 7200.0


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
    _write_srt(srt, SYNTH_CUES)
    return {"client": TestClient(app_module.app), "out": out, "video": str(video), "srt": str(srt),
            "tmp": tmp_path}


def _session(local, kind="shorts", probe=False, **settings):
    r = local["client"].post("/api/film/session", json={
        "kind": kind, "video_path": local["video"], "subtitle_path": local["srt"],
        "settings": settings, "probe_offset": probe})
    assert r.status_code == 200, r.text
    return r.json()


# --- sessions ---------------------------------------------------------------

def test_cloud_mode_hides_every_film_route(local, monkeypatch):
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
    c = local["client"]
    assert c.post("/api/film/session", json={"kind": "shorts", "video_path": "x", "subtitle_path": "y"}).status_code == 404
    assert c.get("/api/film/sessions").status_code == 404
    assert c.get("/api/movieshorts/prompt/abc").status_code == 404
    assert c.get("/api/film/music-hint?mood=tense").status_code == 404
    assert c.get("/film/abc/x.mp4").status_code == 404


def test_session_reads_the_srt_and_persists(local):
    s = _session(local, ratio="1:1", grade="washed")
    assert s["kind"] == "shorts" and s["duration"] == DURATION
    assert s["cue_count"] == len(SYNTH_CUES)
    assert s["settings"]["ratio"] == "1:1" and s["settings"]["hook_count"] == ms.DEFAULT_HOOK_COUNT
    assert os.path.exists(local["out"] / "film" / s["id"] / "session.json")
    got = local["client"].get(f"/api/film/session/{s['id']}").json()
    assert got["id"] == s["id"]
    lst = local["client"].get("/api/film/sessions").json()["sessions"]
    assert [x["id"] for x in lst] == [s["id"]]
    assert local["client"].get("/api/config").json()["filmModules"] is True


def test_session_rejects_missing_or_wrong_files(local):
    c = local["client"]
    r = c.post("/api/film/session", json={"kind": "shorts", "video_path": "/nope.mp4", "subtitle_path": local["srt"]})
    assert r.status_code == 400 and "video not found" in r.json()["detail"]
    r = c.post("/api/film/session", json={"kind": "shorts", "video_path": local["video"], "subtitle_path": local["video"]})
    assert r.status_code == 400 and ".srt" in r.json()["detail"]
    r = c.post("/api/film/session", json={"kind": "trailer", "video_path": local["video"], "subtitle_path": local["srt"]})
    assert r.status_code == 400


def test_offset_probe_runs_and_user_override_wins(local, monkeypatch):
    monkeypatch.setattr(fp, "probe_offset", lambda *a, **k: {"offset": 3.4, "confidence": 0.9, "matches": 20, "votes": 30})
    s = _session(local, probe=True)
    got = local["client"].get(f"/api/film/session/{s['id']}").json()
    assert got["offset"] == 3.4 and got["offset_probe"]["confidence"] == 0.9
    r = local["client"].post(f"/api/film/session/{s['id']}/offset", json={"offset": -1.25})
    assert r.json()["offset"] == -1.25
    # The digest now carries the shifted times.
    d = local["client"].get(f"/api/film/digest/{s['id']}?start=0&end=120").json()
    assert "00:04 Where are we now 0" in d["digest"]  # 5.0 - 1.25 = 3.75 -> 00:04
    assert local["client"].post(f"/api/film/session/{s['id']}/offset", json={"offset": 9999}).status_code == 400


def test_settings_validation(local):
    s = _session(local)
    c = local["client"]
    assert c.post(f"/api/film/session/{s['id']}/settings", json={"ratio": "4:3"}).status_code == 400
    assert c.post(f"/api/film/session/{s['id']}/settings", json={"grade": "sepia"}).status_code == 400
    got = c.post(f"/api/film/session/{s['id']}/settings", json={"ratio": "9:16", "grade": "cold", "bogus": 1}).json()
    assert got["settings"]["ratio"] == "9:16" and got["settings"]["grade"] == "cold" and "bogus" not in got["settings"]


# --- movie shorts flow ------------------------------------------------------

def test_shorts_flow_prompts_hooks_beats_shots_render(local, monkeypatch):
    c = local["client"]
    s = _session(local, hook_count=5)
    sid = s["id"]

    p = c.get(f"/api/movieshorts/prompt/{sid}?pass=hooks").json()
    assert "Return 5 hooks" in p["prompt"] and "Boston Harbor" in p["prompt"]
    assert c.get(f"/api/movieshorts/prompt/{sid}?pass=hooks&format=text").headers["content-type"].startswith("text/plain")
    assert c.get(f"/api/movieshorts/prompt/{sid}?pass=beats").status_code == 400  # needs a hook

    # Paste the hooks back as chat text with fences.
    hooks_text = "Here:\n```json\n" + json.dumps({"film": {"premise": "x"}, "hooks": [HOOK]}) + "\n```"
    r = c.post(f"/api/movieshorts/hooks/{sid}", json={"text": hooks_text}).json()
    assert r["ok"] is True and r["hooks"]["hooks"][0]["title"] == HOOK["title"]

    bp = c.get(f"/api/movieshorts/prompt/{sid}?pass=beats&hook=1").json()
    assert 'titled "He has a dual personality"' in bp["prompt"]

    # A plan with invented dialogue is refused and says why.
    bad = _beats()
    bad["beats"][2]["captions"][0]["text"] = "Something nobody said"
    r = c.post(f"/api/movieshorts/beats/{sid}/1", json=bad).json()
    assert r["ok"] is False and r["errors"][0]["code"] == "caption_text"
    assert "beat 3:" in r["errors_text"]
    assert c.get(f"/api/movieshorts/shots/{sid}/1").status_code == 400  # nothing validated yet

    r = c.post(f"/api/movieshorts/beats/{sid}/1", json=_beats()).json()
    assert r["ok"] is True and r["summary"]["beats"] == 16

    shots = c.get(f"/api/movieshorts/shots/{sid}/1").json()
    assert 70 <= shots["summary"]["shots"] <= 110
    assert shots["music"] is None  # empty library, render still proceeds
    assert shots["rhythm"]["tier"] == "free"
    first = shots["shots"][0]
    edited = c.post(f"/api/movieshorts/shots/{sid}/1", json={"shots": [
        {"id": first["id"], "scale": 1.6, "blur": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}},
        {"id": 999, "scale": 9}]}).json()
    assert edited["shots"][0]["scale"] == 1.6 and edited["shots"][0]["blur"]["w"] == 0.3
    assert c.get(f"/api/movieshorts/shots/{sid}/1").json()["shots"][0]["scale"] == 1.6  # persisted

    # Render with a fake assembly that writes the file.
    seen = {}

    def fake_render(video, shots_, out_path, work, **kw):
        seen.update(kw, n=len(shots_), video=video)
        open(out_path, "wb").write(b"\x00" * 16)
        return {"shots": len(shots_), "output": out_path}
    monkeypatch.setattr(film_api.film_render, "render_short", fake_render)
    monkeypatch.setattr(film_api.film_render, "measure_cut_rhythm",
                        lambda p: {"cuts": 80, "mean_gap": 1.45, "times": []})
    r = c.post(f"/api/movieshorts/render/{sid}/1", json={"grade": "cold"})
    assert r.status_code == 200
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["short-1"]
    assert st["status"] == "completed", st
    assert seen["grade"] == "cold" and seen["output_format"] == "square" and seen["n"] == edited["summary"]["shots"]
    assert seen["caption_preset"] == "film_pop" and seen["cues"]
    assert st["rhythm"]["mean_gap"] == 1.45 and st["title"] == HOOK["title"]
    # The mount binds the real output dir at import time, so the served URL is
    # checked by shape and the file on disk by content.
    assert st["output"] == f"/film/{sid}/{st['file']}"
    assert (local["out"] / "film" / sid / st["file"]).read_bytes() == b"\x00" * 16
    # A session file is not a deliverable.
    assert c.get(f"/film/{sid}/session.json").status_code == 404


def test_shorts_render_needs_a_plan_and_reports_failures(local, monkeypatch):
    c = local["client"]
    sid = _session(local)["id"]
    assert c.post(f"/api/movieshorts/render/{sid}/1", json={}).status_code == 400
    c.post(f"/api/movieshorts/hooks/{sid}", json={"hooks": [HOOK]})
    c.post(f"/api/movieshorts/beats/{sid}/1", json=_beats())

    def boom(*a, **k):
        raise RuntimeError("ffmpeg exploded")
    monkeypatch.setattr(film_api.film_render, "render_short", boom)
    c.post(f"/api/movieshorts/render/{sid}/1", json={})
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["short-1"]
    assert st["status"] == "failed" and "ffmpeg exploded" in st["error"]
    assert any("failed" in line for line in st["logs"])


def test_beats_can_be_forced_past_rule_errors_but_not_schema_errors(local):
    c = local["client"]
    sid = _session(local)["id"]
    c.post(f"/api/movieshorts/hooks/{sid}", json={"hooks": [HOOK]})
    bad = _beats(n=9)                                   # too few beats, footage short
    bad["beats"][2]["captions"][0]["text"] = "Invented line"
    bad["beats"][4]["start"], bad["beats"][4]["end"] = 9000.0, 9009.0   # outside the film: dropped
    r = c.post(f"/api/movieshorts/beats/{sid}/1", json={"json": bad}).json()
    assert r["ok"] is False and r["forceable"] is True
    r = c.post(f"/api/movieshorts/beats/{sid}/1", json={"json": bad, "force": True}).json()
    assert r["ok"] is True and r["forced"] is True and r["errors"]
    assert r["summary"]["beats"] == 8                   # the uncuttable beat is gone
    sess = c.get(f"/api/film/session/{sid}").json()
    stored = sess["beats"]["1"]
    assert stored["forced"] is True and len(stored["beats"]) == 8 and stored["ignored_errors"]
    # Forced plans build shots and render like any other.
    assert c.get(f"/api/movieshorts/shots/{sid}/1").json()["summary"]["shots"] > 0
    # A plan that does not parse cannot be forced.
    r = c.post(f"/api/movieshorts/beats/{sid}/1", json={"json": {"hook_index": "x"}, "force": True}).json()
    assert r["ok"] is False and r["forceable"] is False


def test_recap_plan_force_never_crosses_the_wall(local):
    c = local["client"]
    sid = _session(local, kind="recap")["id"]
    c.post(f"/api/movierecap/spoilers/{sid}", json=SPOILERS)
    c.post(f"/api/movierecap/structure/{sid}", json=STRUCTURE)
    data = _recap_plan(n=12)                            # too few chunks
    data["chunks"][3]["start"], data["chunks"][3]["end"] = 3125.0, 3135.0   # midpoint range
    data["chunks"][5]["start"], data["chunks"][5]["end"] = 5500.0, 5510.0   # past the wall
    r = c.post(f"/api/movierecap/plan/{sid}/1", json={"json": data, "force": True}).json()
    assert r["ok"] is True and r["forced"] is True
    assert r["kept_chunks"] == 10                       # both protected chunks dropped, never rendered
    stored = c.get(f"/api/film/session/{sid}").json()["recap"]["plans"]["1"]
    assert stored["forced"] is True and len(stored["chunks"]) == 10
    assert all(ch["end"] <= 5400 for ch in stored["chunks"])


def test_music_hint_and_library_endpoints(local):
    c = local["client"]
    h = c.get("/api/film/music-hint?mood=tense").json()
    assert h["primary"][0] == "tension underscore" and h["pixabay"]
    assert any(s["name"] == "Pixabay Music" for s in h["sources"])
    lib = c.get("/api/film/library").json()
    assert "recipes" in lib and "by_mood" in lib


def test_library_upload_endpoint(local, monkeypatch, tmp_path):
    import music_library
    seen = {}

    def fake_upload(filename, fileobj, mood, meta=None, **kw):
        seen.update(filename=filename, mood=mood, meta=meta, size=len(fileobj.read()))
        return {"path": f"{mood}/{filename}", "file": filename, "mood": mood, "bpm": 124.0,
                "beat_confidence": 2.1, "flags": []}
    monkeypatch.setattr(music_library, "upload_track", fake_upload)
    monkeypatch.setattr(music_library, "library_summary", lambda *a, **k: {"tracks": 1, "by_mood": {"tense": 1}, "thin": []})
    r = local["client"].post("/api/film/library/upload",
                             files={"file": ("pulse.mp3", b"\x00" * 64, "audio/mpeg")},
                             data={"mood": "tense", "source": "pixabay", "licence": "Pixabay Content License",
                                   "attribution_required": "false"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["track"]["bpm"] == 124.0 and body["by_mood"]["tense"] == 1
    assert seen["mood"] == "tense" and seen["size"] == 64
    assert seen["meta"]["source"] == "pixabay" and seen["meta"]["attribution_required"] is False

    def bad(*a, **k):
        raise ValueError("Unsupported file type")
    monkeypatch.setattr(music_library, "upload_track", bad)
    r = local["client"].post("/api/film/library/upload", files={"file": ("x.txt", b"x", "text/plain")}, data={"mood": "tense"})
    assert r.status_code == 400 and "Unsupported" in r.json()["detail"]


# --- movie recap flow -------------------------------------------------------

def test_recap_flow(local, monkeypatch):
    c = local["client"]
    s = _session(local, kind="recap")
    sid = s["id"]
    assert c.get(f"/api/movieshorts/prompt/{sid}").status_code == 400  # wrong kind

    a = c.get(f"/api/movierecap/prompt/{sid}?pass=a").json()
    assert "Runtime: 02:00:00" in a["prompt"] and a["wall"] == 5400.0
    assert c.get(f"/api/movierecap/prompt/{sid}?pass=b").status_code == 400  # spoilers first

    r = c.post(f"/api/movierecap/spoilers/{sid}", json=SPOILERS).json()
    assert r["ok"] is True and r["protected"][-1]["end"] == DURATION
    r = c.post(f"/api/movierecap/exclusions/{sid}", json={"ranges": [{"start": 100, "end": 130}]}).json()
    assert any(x["tier"] == "manual" for x in r["protected"])

    b = c.get(f"/api/movierecap/prompt/{sid}?pass=b").json()
    assert "Nothing from after 01:30:00" in b["prompt"]
    r = c.post(f"/api/movierecap/structure/{sid}", json={"text": json.dumps(STRUCTURE)}).json()
    assert r["ok"] is True and len(r["structure"]["parts"]) == 3

    cprompt = c.get(f"/api/movierecap/prompt/{sid}?pass=c&part=2").json()
    assert 'Part 2 of three' in cprompt["prompt"]

    # Part 1 plan, first with a chunk inside the manual exclusion.
    bad = _recap_plan()
    bad["chunks"][0]["start"], bad["chunks"][0]["end"] = 105.0, 115.0
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

    def fake_render(video, shots, out_path, work, **kw):
        open(out_path, "wb").write(b"\x01" * 8)
        return {"shots": len(shots)}
    monkeypatch.setattr(film_api.film_render, "render_short", fake_render)
    r = c.post(f"/api/movierecap/render/{sid}", json={}).json()
    assert r["renders"] == ["recap-1-silent"]
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["recap-1-silent"]
    assert st["status"] == "completed" and st["report"]["shots"] == 18
    assert (local["out"] / "film" / sid / st["file"]).exists()
    assert c.post(f"/api/movierecap/render/{sid}", json={"parts": [3]}).status_code == 400


def test_recap_voiceover_uses_compilation_alignment(local, monkeypatch):
    c = local["client"]
    sid = _session(local, kind="recap")["id"]
    c.post(f"/api/movierecap/spoilers/{sid}", json=SPOILERS)
    c.post(f"/api/movierecap/structure/{sid}", json=STRUCTURE)
    assert c.post(f"/api/movierecap/plan/{sid}/1", json=_recap_plan()).json()["ok"]

    import compilation
    plan_words = [w for c_ in _recap_plan()["chunks"] for w in c_["narration"].split()]
    # 486 words spoken over ~150 s.
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
        return {"chunks": len(segments)}
    monkeypatch.setattr(film_api.film_render, "render_narrated", fake_narrated)

    r = c.post(f"/api/movierecap/voiceover/{sid}/1", files={"file": ("vo.wav", b"RIFF" + b"\x00" * 64, "audio/wav")})
    assert r.status_code == 200, r.text
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["recap-1"]
    assert st["status"] == "completed", st
    assert st["vo_check"]["ok"] is True
    segs = captured["segments"]
    assert len(segs) == 18
    # Source seconds came back out (chunk 0 begins at 60 s) and the VO placement is carried.
    assert segs[0]["start"] == pytest.approx(60.0, abs=0.01)
    assert segs[0]["voice_start"] is not None and segs[0]["chunk"]["path"].endswith("vo_0.wav")
    assert c.post(f"/api/movierecap/voiceover/{sid}/1", files={"file": ("vo.txt", b"x", "text/plain")}).status_code == 400


def test_recap_voiceover_refuses_a_recording_far_off_target(local, monkeypatch):
    c = local["client"]
    sid = _session(local, kind="recap")["id"]
    c.post(f"/api/movierecap/spoilers/{sid}", json=SPOILERS)
    c.post(f"/api/movierecap/structure/{sid}", json=STRUCTURE)
    c.post(f"/api/movierecap/plan/{sid}/1", json=_recap_plan())
    import compilation
    monkeypatch.setattr(compilation, "transcribe_vo", lambda p: [{"w": "x", "s": 0.0, "e": 210.0}])
    c.post(f"/api/movierecap/voiceover/{sid}/1", files={"file": ("vo.mp3", b"\x00" * 64, "audio/mpeg")})
    st = c.get(f"/api/film/status/{sid}").json()["renders"]["recap-1"]
    assert st["status"] == "failed" and "+40%" in st["error"]
