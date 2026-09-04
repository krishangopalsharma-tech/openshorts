"""POST /api/clip/music and the under-caption layer chain (fx -> music) that
carries every post-generation layer through re-renders.

Render seams are stubbed (music.apply_music, cinematic.apply_cinematic_effects,
the caption burners); the tests own the endpoint contract, the derivative
names, the partial rebuild (a music change keeps the fx_ file under it) and
what lands in metadata.json.
"""

import asyncio
import json
import os

import httpx
import pytest

app_module = pytest.importorskip("app")
import cinematic  # noqa: E402
import music  # noqa: E402
import recut  # noqa: E402

JOB_ID = "music-endpoint-test-job"
CLEAN = "mytitle_clip_1.mp4"
TRACK = "beat.mp3"

TRANSCRIPT = {
    "language": "en",
    "segments": [{
        "start": 0.0, "end": 60.0, "text": "hello world again",
        "words": [{"word": "hello", "start": 12.0, "end": 12.5},
                  {"word": "world", "start": 20.0, "end": 20.4}],
    }],
}


def _request(method, path, json_body=None):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, json=json_body)
    return asyncio.run(_do())


def _meta(job):
    return json.loads(job["meta_path"].read_text())


@pytest.fixture()
def job(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    job_dir = out_root / JOB_ID
    job_dir.mkdir(parents=True)
    (tmp_path / "uploads").mkdir()
    lib = tmp_path / "music"
    lib.mkdir()
    (lib / TRACK).write_bytes(b"mp3")
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(music, "MUSIC_DIR", str(lib))

    clip = {"start": 10.0, "end": 40.0, "video_title_for_youtube_short": "t",
            "video_url": f"/videos/{JOB_ID}/{CLEAN}"}
    meta = {"shorts": [clip], "transcript": TRANSCRIPT, "source_video": "src.mp4",
            "output_format": "auto", "cost_analysis": {}}
    (job_dir / "mytitle_metadata.json").write_text(json.dumps(meta))
    (job_dir / CLEAN).write_bytes(b"canonical")
    (job_dir / "src.mp4").write_bytes(b"source")
    app_module.jobs[JOB_ID] = {"status": "completed", "logs": [],
                               "result": {"clips": [dict(clip)], "cost_analysis": {}},
                               "user_id": None, "watermark": False}
    try:
        yield {"dir": job_dir, "meta_path": job_dir / "mytitle_metadata.json", "lib": lib}
    finally:
        app_module.jobs.pop(JOB_ID, None)


@pytest.fixture()
def fake_mix(monkeypatch):
    calls = []

    def fake(video_path, spec, output_path, music_dir=None):
        calls.append((os.path.basename(video_path), dict(spec), os.path.basename(output_path)))
        with open(output_path, "wb") as f:
            f.write(b"mixed")
        return True

    monkeypatch.setattr(music, "apply_music", fake)
    return calls


@pytest.fixture()
def fake_fx(monkeypatch):
    calls = []

    def fake(video_path, effects, output_path=None):
        calls.append(os.path.basename(video_path))
        with open(output_path or video_path, "wb") as f:
            f.write(b"graded")
        return True

    monkeypatch.setattr(cinematic, "apply_cinematic_effects", fake)
    return calls


@pytest.fixture()
def fake_captions(monkeypatch):
    calls = []

    def styled(video_path, transcript, clip_start, clip_end, style, split_ranges=None):
        calls.append(("styled", os.path.basename(video_path)))
        out = os.path.join(os.path.dirname(video_path), f"subtitled_9_{os.path.basename(video_path)}")
        with open(out, "wb") as f:
            f.write(b"c")
        return out

    monkeypatch.setattr(app_module, "_burn_styled_captions", styled)
    import main as main_module

    def stock(video_path, transcript, clip_start, clip_end, split_ranges=None):
        calls.append(("stock", os.path.basename(video_path)))
        out = os.path.join(os.path.dirname(video_path), f"subtitled_9_{os.path.basename(video_path)}")
        with open(out, "wb") as f:
            f.write(b"c")
        return out

    monkeypatch.setattr(main_module, "auto_caption_clip", stock)
    return calls


SPEC = {"track": TRACK, "volume_db": -20, "duck": 50, "start": 3}


class TestNormalize:
    def test_clamps_and_defaults(self):
        spec = music.normalize({"track": "../x/beat.mp3", "volume_db": 5, "duck": 140, "start": -2, "junk": 1})
        assert spec == {"track": "beat.mp3", "volume_db": 0.0, "duck": 100.0, "start": 0.0, "fade_out": 1.0}
        assert music.normalize({"track": TRACK})["volume_db"] == -18.0
        assert music.normalize({}) is None and music.normalize(None) is None

    def test_graph_ducks_the_music_under_the_voice(self):
        g = music.build_audio_graph(music.normalize(SPEC), 30.0)
        assert "volume=-20.0dB" in g
        assert "sidechaincompress=threshold=0.02:ratio=10.50" in g   # duck 50 -> 1 + 9.5
        assert "amix=inputs=2:duration=first:normalize=0[aout]" in g
        assert "afade=t=out:st=29.000:d=1.000" in g
        silent = music.build_audio_graph(music.normalize(SPEC), 30.0, voice=False)
        assert "sidechaincompress" not in silent and silent.endswith("[aout]")

    def test_resolve_track_never_escapes_the_library(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"x")
        assert music.resolve_track("a.mp3", str(tmp_path)) == os.path.abspath(str(tmp_path / "a.mp3"))
        assert music.resolve_track("../a.mp3", str(tmp_path)).endswith("a.mp3")  # basename only
        assert music.resolve_track("nope.mp3", str(tmp_path)) is None
        assert music.resolve_track("a.exe", str(tmp_path)) is None


class TestMusicEndpoint:
    def test_unknown_track_404(self, job):
        r = _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0,
                                                 "music": {"track": "missing.mp3"}})
        assert r.status_code == 404

    def test_library_listing(self, job):
        r = _request("GET", "/api/music")
        assert r.status_code == 200
        assert [t["file"] for t in r.json()["tracks"]] == [TRACK]

    def test_mixes_into_a_music_layer_over_the_bare_clip(self, job, fake_mix):
        r = _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0, "music": SPEC})
        assert r.status_code == 200, r.text
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("mu_") and served.endswith(f"_{CLEAN}")
        assert fake_mix[0][0] == CLEAN
        assert fake_mix[0][1]["volume_db"] == -20.0 and fake_mix[0][1]["start"] == 3.0
        meta = _meta(job)["shorts"][0]
        assert meta["music"]["track"] == TRACK and meta["video_url"].endswith(served)
        assert r.json()["captions"] is False

    def test_music_change_keeps_the_fx_layer_under_it(self, job, fake_mix, fake_fx):
        look = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0,
                                                   "cinematic": {"color_grade": "warm"}})
        fx_file = look.json()["new_video_url"].split("/")[-1]
        assert fx_file.startswith("fx_") and fake_fx == [CLEAN]

        r = _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0, "music": SPEC})
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("mu_") and served.endswith(f"_{fx_file}")
        assert fake_fx == [CLEAN]                       # the grade was NOT re-run
        assert fake_mix[0][0] == fx_file                # mixed onto the graded file

        # Swapping the track re-mixes from the fx file, never music over music.
        r2 = _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0,
                                                  "music": dict(SPEC, volume_db=-10)})
        served2 = r2.json()["new_video_url"].split("/")[-1]
        assert served2.count("mu_") == 1 and fake_mix[-1][0] == fx_file

    def test_look_change_reapplies_music_on_top_of_the_new_grade(self, job, fake_mix, fake_fx):
        _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0, "music": SPEC})
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0,
                                                "cinematic": {"color_grade": "bw"}})
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("mu_") and "_fx_" in served
        assert fake_fx[-1] == CLEAN                     # graded from bare
        assert fake_mix[-1][0].startswith("fx_")        # music re-mixed onto the grade

    def test_removal_returns_to_the_layer_below(self, job, fake_mix):
        _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0, "music": SPEC})
        r = _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0, "music": None})
        assert r.status_code == 200
        assert r.json()["new_video_url"].endswith(f"/{CLEAN}") and r.json()["music"] is None
        assert _meta(job)["shorts"][0]["music"] is None

    def test_captions_come_back_on_top(self, job, fake_mix, fake_captions):
        (job["dir"] / f"subtitled_5_{CLEAN}").write_bytes(b"old")
        meta = _meta(job)
        meta["shorts"][0]["video_url"] = f"/videos/{JOB_ID}/subtitled_5_{CLEAN}"
        meta["shorts"][0]["caption_style"] = {"preset": "beast_red", "overrides": {}}
        job["meta_path"].write_text(json.dumps(meta))
        r = _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0, "music": SPEC})
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("subtitled_9_mu_") and r.json()["captions"] is True
        assert fake_captions[0][0] == "styled"
        assert fake_mix[0][0] == CLEAN                  # mixed under the captions, not over them

    def test_format_change_carries_the_music(self, job, fake_mix, monkeypatch):
        _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0, "music": SPEC})
        calls = []

        def fake_recut(**kwargs):
            calls.append(kwargs)
            name = f"recut_1_{kwargs['clean_name']}"
            with open(os.path.join(kwargs["output_dir"], name), "wb") as f:
                f.write(b"recut")
            graded = kwargs["effects"](os.path.join(kwargs["output_dir"], name))
            return (os.path.basename(graded) if graded else name), name

        monkeypatch.setattr(recut, "perform_recut", fake_recut)
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "output_format": "square"})
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("mu_") and f"_recut_1_{CLEAN}" in served
        assert fake_mix[-1][0] == f"recut_1_{CLEAN}"


class TestLayerHelpers:
    def test_strip_all_layers_handles_music_over_fx(self, tmp_path):
        fx = f"fx_1_abc123_{CLEAN}"
        mus = f"mu_def456_{fx}"
        for name in (CLEAN, fx, mus, f"subtitled_3_{mus}"):
            (tmp_path / name).write_bytes(b"x")
        assert app_module._strip_all_layers(str(tmp_path), f"subtitled_3_{mus}") == CLEAN
        assert app_module._strip_music(str(tmp_path), mus) == fx

    def test_canonical_clip_file_sees_the_music_layer(self, tmp_path):
        (tmp_path / CLEAN).write_bytes(b"x")
        m = tmp_path / f"mu_abc123_{CLEAN}"
        m.write_bytes(b"y")
        os.utime(m, (2_000_000_000, 2_000_000_000))
        assert app_module._canonical_clip_file(str(tmp_path), "mytitle", 0) == m.name
