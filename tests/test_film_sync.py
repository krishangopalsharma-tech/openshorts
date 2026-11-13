"""SRT sync over the whole film (five probes -> constant / linear / piecewise
model) and the whisper transcript for a film with no SRT."""

import film_api
import film_prep as fp
from test_film_api import CUES, DURATION, _session, local  # noqa: F401  (fixture)


def _pt(t, off, ok=True):
    return {"t": t, "offset": off, "ok": ok}


# --- the model ----------------------------------------------------------------------

def test_no_good_point_is_no_model():
    assert fp.fit_sync([_pt(100, 3, ok=False)], 7200)["kind"] == "none"
    assert fp.fit_sync([], 7200)["kind"] == "none"


def test_matching_points_are_a_constant_offset():
    m = fp.fit_sync([_pt(700, 2.1), _pt(2100, 2.0), _pt(3600, 2.05), _pt(5000, 2.0), _pt(6400, 2.1)], 7200)
    assert m["kind"] == "constant" and m["offset"] == 2.05


def test_a_frame_rate_mismatch_is_fitted_as_drift():
    # 25 fps SRT on a 23.976 fps file: the offset grows ~4.3% of the time.
    rate = 25 / 23.976 - 1
    pts = [_pt(t, 0.5 + rate * t) for t in (700, 2100, 3600, 5000, 6400)]
    pts[2]["ok"] = False                                    # one probe heard music only
    m = fp.fit_sync(pts, 7200)
    assert m["kind"] == "linear" and abs(m["rate"] - rate) < 1e-4 and abs(m["offset"] - 0.5) < 0.01
    assert abs(fp.sync_offset_at(m, 7000) - (0.5 + rate * 7000)) < 0.05


def test_a_jump_is_piecewise_and_switches_between_the_probes():
    m = fp.fit_sync([_pt(700, 1.0), _pt(2100, 1.0), _pt(3600, 1.0), _pt(5000, 4.0), _pt(6400, 4.0)], 7200)
    assert m["kind"] == "piecewise"
    assert fp.sync_offset_at(m, 100) == 1.0 and fp.sync_offset_at(m, 4200) == 1.0
    assert fp.sync_offset_at(m, 4400) == 4.0 and fp.sync_offset_at(m, 7100) == 4.0


def test_apply_sync_moves_each_cue_by_its_own_offset():
    cues = [{"start": 10.0, "end": 12.0, "text": "a"}, {"start": 7000.0, "end": 7002.0, "text": "b"}]
    out = fp.apply_sync(cues, {"kind": "linear", "offset": 1.0, "rate": 0.001, "knots": []})
    assert out[0]["start"] == 11.01 and out[1]["start"] == 7008.0 and out[1]["end"] == 7010.0
    assert fp.apply_sync(cues, {"kind": "none"}) == cues
    assert fp.apply_sync([{"start": 1.0, "end": 2.0, "text": "x"}], {"kind": "constant", "offset": -5.0}) == []


def test_probe_sync_probes_five_points_and_reports_srt_time(monkeypatch):
    seen = []

    def fake_probe(video, cues, duration, transcriber=None, probe_seconds=45.0, at=0.25):
        seen.append(at)
        start = duration * at
        return {"offset": 2.0, "confidence": 0.9, "matches": 20, "votes": 25, "window": [start, start + probe_seconds]}
    monkeypatch.setattr(fp, "probe_offset", fake_probe)
    progress = []
    r = fp.probe_sync("v.mp4", [], 1000.0, on_point=lambda i, p: progress.append(i))
    assert seen == list(fp.SYNC_FRACTIONS) and progress == [0, 1, 2, 3, 4]
    assert r["points"][0]["at"] == 122.5 and r["points"][0]["t"] == 120.5
    assert r["model"]["kind"] == "constant" and r["model"]["offset"] == 2.0


# --- whisper transcript -------------------------------------------------------------

def test_long_whisper_segments_are_split_into_caption_sized_cues(tmp_path):
    words = [{"word": f" w{i}", "start": i * 0.4, "end": i * 0.4 + 0.3} for i in range(25)]
    tr = {"segments": [{"start": 0, "end": 10, "text": "", "words": words},
                       {"start": 20, "end": 22, "text": " no word timings here ", "words": []}]}
    cues = fp.transcript_to_cues(tr, max_words=10, max_seconds=5.0)
    assert [len(c["text"].split()) for c in cues[:3]] == [10, 10, 5]
    assert cues[0]["start"] == 0.0 and cues[0]["end"] == 3.9 and cues[-1]["text"] == "no word timings here"
    path = fp.write_srt(cues, str(tmp_path / "w.srt"))
    back = fp.parse_subtitles(path)
    assert [c["text"] for c in back] == [c["text"] for c in cues] and back[1]["start"] == cues[1]["start"]


# --- the API --------------------------------------------------------------------------

def test_session_applies_a_drift_model(local, monkeypatch):  # noqa: F811
    rate = 0.001

    def fake_probe(video, cues, duration, transcriber=None, probe_seconds=45.0, at=0.25):
        centre = duration * at + probe_seconds / 2
        off = 1.0 + rate * centre                             # close enough to SRT time for the test
        return {"offset": round(off, 3), "confidence": 0.9, "matches": 30, "votes": 40,
                "window": [centre - probe_seconds / 2, centre + probe_seconds / 2]}
    monkeypatch.setattr(fp, "probe_offset", fake_probe)
    s = _session(local, probe=True)
    c = local["client"]
    got = c.get(f"/api/film/session/{s['id']}").json()
    assert got["sync"]["status"] == "done" and len(got["sync"]["points"]) == 5
    assert got["sync"]["model"]["kind"] == "linear" and got["offset_probe"]["kind"] == "linear"
    cues = film_api._cues(film_api._load(s["id"]))
    first, last = cues[0], cues[-1]
    assert abs((first["start"] - CUES[0]["start"]) - (1.0 + rate * CUES[0]["start"])) < 0.05
    assert abs((last["start"] - CUES[-1]["start"]) - (1.0 + rate * CUES[-1]["start"])) < 0.05

    # a typed offset wins over the model; resync gives the model back
    c.post(f"/api/film/session/{s['id']}/offset", json={"offset": 0.0})
    assert film_api._cues(film_api._load(s["id"]))[-1]["start"] == CUES[-1]["start"]
    r = c.post(f"/api/film/session/{s['id']}/sync").json()
    assert r["offset_set_by_user"] is False
    assert film_api._cues(film_api._load(s["id"]))[-1]["start"] > CUES[-1]["start"] + 5


def test_no_srt_transcribes_the_film_and_writes_whisper_srt(local, monkeypatch):  # noqa: F811
    c = local["client"]
    heard = [{"start": 5.0 + 20 * i, "end": 8.0 + 20 * i, "text": f"heard line {i}"} for i in range(50)]
    monkeypatch.setattr(fp, "transcribe_film", lambda path, transcriber=None: heard)
    r = c.post("/api/film/session", json={"kind": "edits", "video_path": local["video"], "subtitle_path": ""})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    s = c.get(f"/api/film/session/{sid}").json()
    assert s["transcript_source"] == "whisper" and s["transcribing"]["status"] == "done"
    assert s["subtitle_path"].endswith("whisper.srt") and s["cue_count"] == 50
    assert s["offset_probe"]["source"] == "whisper"
    assert "#0 00:05 heard line 0" in c.get(f"/api/movieshorts/prompt/{sid}").json()["prompt"]
    assert c.post(f"/api/film/session/{sid}/sync").status_code == 400


def test_prompts_wait_while_the_film_is_transcribing_and_report_a_failure(local, monkeypatch):  # noqa: F811
    c = local["client"]
    monkeypatch.setattr(film_api, "_start_thread", lambda target, *a: None)     # never runs
    sid = c.post("/api/film/session", json={"video_path": local["video"]}).json()["id"]
    assert c.get(f"/api/movieshorts/prompt/{sid}").status_code == 409
    assert c.get(f"/api/film/status/{sid}").json()["transcribing"]["status"] == "queued"

    def boom(path, transcriber=None):
        raise RuntimeError("cuda out of memory")
    monkeypatch.setattr(fp, "transcribe_film", boom)
    film_api._transcribe_film_job(sid)
    s = c.get(f"/api/film/session/{sid}").json()
    assert s["transcribing"]["status"] == "failed" and "cuda" in s["transcribing"]["error"]
    r = c.get(f"/api/movieshorts/prompt/{sid}")
    assert r.status_code == 409 and "cuda" in r.json()["detail"]



def test_a_jump_is_located_by_halving_the_gap(monkeypatch):
    cut = 4000.0                                           # the removed scene, in video time

    def fake_probe(video, cues, duration, transcriber=None, probe_seconds=45.0, at=0.25):
        start = duration * at
        centre = start + probe_seconds / 2
        off = 1.0 if centre < cut else 4.0
        return {"offset": off, "confidence": 0.9, "support": 0.8, "matches": 20, "votes": 25,
                "window": [start, start + probe_seconds]}
    monkeypatch.setattr(fp, "probe_offset", fake_probe)
    r = fp.probe_sync("v.mp4", [], 7200.0)
    refined = [p for p in r["points"] if p.get("refine")]
    assert len(refined) == fp.SYNC_BISECT_STEPS
    assert [p["at"] for p in r["points"]] == sorted(p["at"] for p in r["points"])
    m = r["model"]
    assert m["kind"] == "piecewise"
    # the switch now sits within ~1/8 of the 1440 s gap of the real cut
    switch = next(t for t in range(3000, 6000, 10) if fp.sync_offset_at(m, t) == 4.0)
    assert abs(switch - cut) < 200


def test_detect_offset_reports_support_and_prefers_cue_first_words():
    cues = [{"start": 10.0 * i, "end": 10.0 * i + 4.0, "text": f"alpha{i} beta{i} gamma{i} delta{i}"} for i in range(12)]
    # audio: each line spoken 2 s later, words packed into the first 1.2 s
    asr = []
    for i in range(12):
        for k, w in enumerate((f"alpha{i}", f"beta{i}", f"gamma{i}", f"delta{i}")):
            asr.append({"word": w, "start": 10.0 * i + 2.0 + 0.3 * k, "end": 10.0 * i + 2.25 + 0.3 * k})
    r = fp.detect_offset(cues, asr, 0, 120)
    assert r["anchors"] == 12 and r["offset"] == 2.0          # not dragged toward the spread words
    assert r["support"] == 0.25                                # one word in four agrees: the first
