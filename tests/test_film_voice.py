"""film_voice: the Kokoro client and the fit of generated narration to the
cut, against a fake server (httpx.MockTransport). No Kokoro, no GPU."""

import io
import json
import struct
import threading
import wave

import httpx
import pytest

import film_voice as fv
import movierecap as mr
from test_movierecap import SPOILERS, STRUCTURE, DURATION, _plan

SR = 24000


def _wav_bytes(seconds):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(struct.pack("<h", 0) * int(seconds * SR))
    return buf.getvalue()


class FakeKokoro:
    """Speech length = words / wpm at speed 1, divided by the requested speed."""

    def __init__(self, wpm=180.0, voices=("af_heart", "am_michael", "hf_alpha", "bm_george", "af_v0bella")):
        self.wpm = wpm
        self.voice_ids = list(voices)
        self.calls = []
        self.transport = httpx.MockTransport(self.handle)

    def client(self):
        return httpx.Client(base_url="http://kokoro.test", transport=self.transport)

    def handle(self, request):
        if request.url.path == "/v1/audio/voices":
            return httpx.Response(200, json={"voices": [{"id": v, "name": v} for v in self.voice_ids]})
        if request.url.path == "/v1/audio/speech":
            body = json.loads(request.content)
            self.calls.append(body)
            words = len(body["input"].split())
            seconds = words / self.wpm * 60.0 / float(body["speed"])
            return httpx.Response(200, content=_wav_bytes(seconds), headers={"content-type": "audio/wav"})
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def _fresh_cache():
    fv._voices_cache.update(at=0.0, voices=None)


def test_describe_voice():
    v = fv.describe_voice("hm_omega")
    assert v == {"id": "hm_omega", "name": "Omega", "language": "Hindi", "lang_code": "hi",
                 "gender": "male", "legacy": False}
    assert fv.describe_voice("af_v0bella")["legacy"] is True
    assert fv.describe_voice("weird")["language"] == "Other"


def test_voices_and_health_online():
    k = FakeKokoro()
    vs = fv.voices(client=k.client())
    assert [v["id"] for v in vs][:1] == ["af_heart"] or len(vs) == 5
    assert all(v["legacy"] is False for v in vs[:4]) and vs[-1]["legacy"] is True
    h = fv.health(client=k.client())
    assert h["online"] is True and h["voices"] == 5 and h["device"] == "cpu"


def test_health_offline_is_a_state_not_an_exception():
    def down(request):
        raise httpx.ConnectError("refused")
    c = httpx.Client(base_url="http://kokoro.test", transport=httpx.MockTransport(down))
    h = fv.health(client=c)
    assert h["online"] is False and "refused" in h["error"] and "start-cpu" in h["start_command"]
    with pytest.raises(fv.VoiceOffline):
        fv.synthesize("hello there", "am_michael", 1.0, "x.wav", client=httpx.Client(
            base_url="http://kokoro.test", transport=httpx.MockTransport(down)))


def test_synthesize_writes_a_wav_and_measures_it(tmp_path):
    k = FakeKokoro(wpm=180)
    out = tmp_path / "line.wav"
    got = fv.synthesize("one two three four five six", "am_michael", 1.0, str(out), client=k.client())
    assert out.exists() and got["duration"] == pytest.approx(2.0, abs=0.01)
    assert k.calls[0]["voice"] == "am_michael" and k.calls[0]["response_format"] == "wav"
    # Speed is clamped to the allowed range and empty text is refused.
    got = fv.synthesize("a b c", "am_michael", 9.0, str(out), client=k.client())
    assert got["speed"] == fv.SPEED_RANGE[1]
    with pytest.raises(ValueError):
        fv.synthesize("   ", "am_michael", 1.0, str(out), client=k.client())


def test_wav_duration_ignores_a_streaming_header(tmp_path):
    """Kokoro-FastAPI streams WAV with a 0xFFFFFFFF data length; measured
    from the header a 9 s line read as 89,478 s and every chunk 'overran'."""
    good = _wav_bytes(2.0)
    bad = bytearray(good)
    bad[4:8] = b"\xff\xff\xff\xff"     # RIFF size placeholder
    bad[40:44] = b"\xff\xff\xff\xff"   # data chunk size placeholder
    p = tmp_path / "stream.wav"
    p.write_bytes(bytes(bad))
    assert fv.wav_duration(str(p)) == pytest.approx(2.0, abs=0.01)
    p2 = tmp_path / "good.wav"
    p2.write_bytes(good)
    assert fv.wav_duration(str(p2)) == pytest.approx(2.0, abs=0.01)


def test_synthesize_is_serialised():
    k = FakeKokoro()
    active, peak = [0], [0]
    lock = threading.Lock()
    real_post = k.handle

    def slow(request):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        import time
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return real_post(request)
    transport = httpx.MockTransport(slow)

    def work(i):
        fv.synthesize(f"line {i} words here", "am_michael", 1.0, f"{i}.wav",
                      client=httpx.Client(base_url="http://kokoro.test", transport=transport))
    import os
    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i in range(4):
        os.remove(f"{i}.wav")
    assert peak[0] == 1


def _part_and_protected():
    protected = mr.union_protected(SPOILERS, DURATION)
    st, errors = mr.validate_structure(STRUCTURE, DURATION, protected)
    assert errors == []
    return st.parts[0], protected


def test_narrate_plan_places_lines_that_fit(tmp_path):
    # 27 words at 240 wpm = 6.75 s against a 10.4 / 1.25 = 8.32 s slot: fits.
    k = FakeKokoro(wpm=240)
    part, protected = _part_and_protected()
    plan, errors, _ = mr.validate_plan(_plan(), part, DURATION, protected)
    assert errors == []
    out = fv.narrate_plan(plan, part, protected, DURATION, "am_michael", 1.0, str(tmp_path), client=k.client())
    assert len(out["segments"]) == 18 and out["overruns"] == 0
    assert {r["action"] for r in out["report"]} == {"fit"}
    s0 = out["segments"][0]
    assert s0["start"] == pytest.approx(60.0) and s0["end"] == pytest.approx(70.4)
    assert s0["shot_start"] == 0.0 and s0["voice_start"] == pytest.approx(fv.LEAD_SECONDS)
    assert s0["shot_length"] == pytest.approx(8.32, abs=0.01)
    assert s0["chunk"]["path"].endswith("line_01.wav")
    # Consecutive shots tile the finished timeline.
    for a, b in zip(out["segments"], out["segments"][1:]):
        assert b["shot_start"] == pytest.approx(a["shot_start"] + a["shot_length"], abs=1e-3)
    assert out["total_finished"] == pytest.approx(18 * 8.32, abs=0.1)
    assert len(k.calls) == 18


def test_narrate_plan_grows_the_chunk_then_speeds_up_then_reports_overrun(tmp_path):
    # 27 words at 150 wpm = 10.8 s against 8.32 - 0.4 = 7.92 s available:
    # 2.88 s short. Room is 3 s each side (finished 2.4 s) -> room covers it.
    part, protected = _part_and_protected()
    plan, errors, _ = mr.validate_plan(_plan(), part, DURATION, protected)
    k = FakeKokoro(wpm=150)
    out = fv.narrate_plan(plan, part, protected, DURATION, "am_michael", 1.0, str(tmp_path), client=k.client())
    actions = {r["action"] for r in out["report"]}
    assert actions == {"room"}, actions
    r0 = out["report"][0]
    assert r0["room_used"] == pytest.approx(2.88, abs=0.05)
    seg = out["segments"][0]
    assert seg["end"] > 70.4  # the tail grew, in source seconds
    assert seg["shot_length"] == pytest.approx(r0["voice_seconds"] + 0.4, abs=0.02)

    # 27 words at 100 wpm = 16.2 s: room (4.8 s) is not enough, speed to 1.25
    # gives 12.96 s, still over 12.72 s available -> overrun, reported.
    k = FakeKokoro(wpm=100)
    out = fv.narrate_plan(plan, part, protected, DURATION, "am_michael", 1.0, str(tmp_path / "b"), client=k.client())
    r0 = out["report"][0]
    assert r0["action"] == "overrun" and r0["speed"] == pytest.approx(1.25) and r0["overrun"] > 0
    assert out["overruns"] == 18
    # The re-read happened at the faster speed and the footage was not stretched.
    assert any(abs(c["speed"] - 1.25) < 1e-6 for c in k.calls)
    seg = out["segments"][0]
    assert seg["shot_length"] == pytest.approx((seg["end"] - seg["start"]) / 1.25, abs=0.01)


def test_narrate_plan_room_stops_at_a_protected_range(tmp_path):
    part, _ = _part_and_protected()
    # A manual exclusion 1.2 s after chunk 1 ends caps its tail room.
    protected = mr.union_protected(SPOILERS, DURATION, manual=[{"start": 142.0, "end": 150.0}])
    plan, errors, _ = mr.validate_plan(_plan(), part, DURATION, protected)
    assert errors == []
    k = FakeKokoro(wpm=150)
    out = fv.narrate_plan(plan, part, protected, DURATION, "am_michael", 1.0, str(tmp_path), client=k.client())
    seg1 = out["segments"][1]
    assert seg1["end"] <= 142.0 + 1e-3   # never into the exclusion
    assert seg1["start"] < 130.4          # the head room paid the rest


def test_narrate_plan_keeps_silent_chunks(tmp_path):
    part, protected = _part_and_protected()
    data = _plan()
    data["chunks"][2]["narration"] = ""
    plan = mr.PartPlan.model_validate(data)
    k = FakeKokoro(wpm=240)
    out = fv.narrate_plan(plan, part, protected, DURATION, "af_heart", 1.0, str(tmp_path), client=k.client())
    assert out["report"][2]["action"] == "silent"
    assert out["segments"][2]["silent"] is True and out["segments"][2]["chunk"] is None
    assert len(k.calls) == 17
