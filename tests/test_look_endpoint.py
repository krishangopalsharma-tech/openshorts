"""POST /api/clip/look — output format + cinematic look after generation —
and the layer bookkeeping around it (fx_ prefix, caption style persistence).

Same conventions as test_rerender_endpoint.py: a real ASGI round-trip against
the imported app with the render seams stubbed (recut.perform_recut, the
cinematic ffmpeg pass, the caption burn). These tests own the endpoint
contract: what is re-rendered vs graded in place, 409 on a gone source,
the derivative names, and what lands in metadata.json.
"""

import asyncio
import json
import os

import httpx
import pytest

app_module = pytest.importorskip("app")
import recut  # noqa: E402
import cinematic  # noqa: E402

JOB_ID = "look-endpoint-test-job"
CLEAN = "mytitle_clip_1.mp4"

TRANSCRIPT = {
    "language": "en",
    "segments": [{
        "start": 0.0, "end": 60.0, "text": "hello world again",
        "words": [
            {"word": "hello", "start": 12.0, "end": 12.5},
            {"word": "world", "start": 20.0, "end": 20.4},
            {"word": "again", "start": 30.0, "end": 30.5},
        ],
    }],
}


def _request(method, path, json_body=None):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            return await client.request(method, path, json=json_body)
    return asyncio.run(_do())


def _meta(job):
    return json.loads(job["meta_path"].read_text())


@pytest.fixture()
def job(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    up_root = tmp_path / "uploads"
    job_dir = out_root / JOB_ID
    job_dir.mkdir(parents=True)
    up_root.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(up_root))

    clip = {"start": 10.0, "end": 40.0, "video_title_for_youtube_short": "test clip",
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
        yield {"dir": job_dir, "meta_path": job_dir / "mytitle_metadata.json"}
    finally:
        app_module.jobs.pop(JOB_ID, None)


@pytest.fixture()
def fake_recut(monkeypatch):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        name = f"recut_1_{kwargs['clean_name']}"
        with open(os.path.join(kwargs["output_dir"], name), "wb") as f:
            f.write(b"recut")
        served = name
        # Honour the hooks the way the real perform_recut does, so the test
        # sees the fx layer and the captions land where they should.
        if kwargs.get("effects"):
            graded = kwargs["effects"](os.path.join(kwargs["output_dir"], name))
            if graded:
                served = os.path.basename(graded)
        tr = kwargs.get("captions_transcript")
        if tr and tr.get("segments") and kwargs.get("captioner"):
            captioned = kwargs["captioner"](os.path.join(kwargs["output_dir"], served), tr, 0.0, 30.0)
            if captioned:
                served = os.path.basename(captioned)
        return served, name

    monkeypatch.setattr(recut, "perform_recut", fake)
    return calls


@pytest.fixture()
def fake_fx(monkeypatch):
    """The cinematic ffmpeg pass: just materialize the output file."""
    calls = []

    def fake(video_path, effects, output_path=None):
        calls.append((os.path.basename(video_path), effects, output_path))
        with open(output_path or video_path, "wb") as f:
            f.write(b"graded")
        return True

    monkeypatch.setattr(cinematic, "apply_cinematic_effects", fake)
    return calls


@pytest.fixture()
def fake_captions(monkeypatch):
    """Both caption burners: the styled one and the stock auto_caption_clip."""
    calls = []

    def styled(video_path, transcript, clip_start, clip_end, style, split_ranges=None):
        calls.append(("styled", os.path.basename(video_path), style))
        out = os.path.join(os.path.dirname(video_path), f"subtitled_9_{os.path.basename(video_path)}")
        with open(out, "wb") as f:
            f.write(b"captioned")
        return out

    monkeypatch.setattr(app_module, "_burn_styled_captions", styled)

    import main as main_module

    def stock(video_path, transcript, clip_start, clip_end, split_ranges=None):
        calls.append(("stock", os.path.basename(video_path), None))
        out = os.path.join(os.path.dirname(video_path), f"subtitled_9_{os.path.basename(video_path)}")
        with open(out, "wb") as f:
            f.write(b"captioned")
        return out

    monkeypatch.setattr(main_module, "auto_caption_clip", stock)
    return calls


LOOK = {"color_grade": "warm", "grain": True, "vignette": True}


class TestValidation:
    def test_nothing_to_change_400(self, job):
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0})
        assert r.status_code == 400

    def test_bad_format_400(self, job):
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "output_format": "portrait"})
        assert r.status_code == 400

    def test_unknown_job_and_clip_404(self, job):
        assert _request("POST", "/api/clip/look", {"job_id": "nope", "clip_index": 0, "output_format": "square"}).status_code == 404
        assert _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 4, "output_format": "square"}).status_code == 404


class TestLookOnSameCut:
    def test_grades_the_bare_file_into_an_fx_derivative(self, job, fake_recut, fake_fx):
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "cinematic": LOOK})
        assert r.status_code == 200, r.text
        body = r.json()
        assert fake_recut == []                       # no re-render for a look
        served = body["new_video_url"].split("/")[-1]
        assert served.startswith("fx_") and served.endswith(f"_{CLEAN}")
        assert (job["dir"] / served).exists() and (job["dir"] / CLEAN).exists()
        assert body["rerendered"] is False and body["output_format"] == "vertical"
        # Normalized, every key present, unknowns dropped.
        assert body["cinematic"]["color_grade"] == "warm" and body["cinematic"]["grain"] is True
        assert body["cinematic"]["glow"] is False
        meta = _meta(job)["shorts"][0]
        assert meta["video_url"].endswith(served) and meta["cinematic"]["vignette"] is True
        assert meta["output_format"] == "vertical"
        assert app_module.jobs[JOB_ID]["result"]["clips"][0]["video_url"].endswith(served)
        assert fake_fx[0][0] == CLEAN                 # graded from the bare render

    def test_replacing_a_look_starts_from_the_clean_file(self, job, fake_recut, fake_fx):
        first = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "cinematic": LOOK}).json()
        second = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0,
                                                     "cinematic": {"color_grade": "bw"}}).json()
        served = second["new_video_url"].split("/")[-1]
        assert served.count("fx_") == 1               # not fx_ over fx_
        assert fake_fx[-1][0] == CLEAN
        assert first["new_video_url"] != second["new_video_url"]

    def test_all_off_removes_the_look(self, job, fake_recut, fake_fx):
        _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "cinematic": LOOK})
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0,
                                                "cinematic": {"color_grade": "none"}})
        assert r.status_code == 200
        assert r.json()["new_video_url"].endswith(f"/{CLEAN}")
        assert r.json()["cinematic"] is None
        assert _meta(job)["shorts"][0]["cinematic"] is None

    def test_captions_go_back_on_top_in_the_users_style(self, job, fake_recut, fake_fx, fake_captions):
        # The clip currently wears styled captions.
        (job["dir"] / f"subtitled_5_{CLEAN}").write_bytes(b"old captions")
        meta = _meta(job)
        meta["shorts"][0]["video_url"] = f"/videos/{JOB_ID}/subtitled_5_{CLEAN}"
        meta["shorts"][0]["caption_style"] = {"preset": "hormozi_green", "overrides": {"pos_y": 80}}
        job["meta_path"].write_text(json.dumps(meta))

        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "cinematic": LOOK})
        assert r.status_code == 200, r.text
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("subtitled_9_fx_") and served.endswith(f"_{CLEAN}")
        assert r.json()["captions"] is True
        assert fake_captions[0][0] == "styled"
        assert fake_captions[0][2]["preset"] == "hormozi_green"
        # Graded from the bare render, not from the captioned file.
        assert fake_fx[0][0] == CLEAN

    def test_generation_time_captions_are_seen_through_the_in_memory_job(self, job, fake_recut, fake_fx, fake_captions):
        """main.py never writes video_url into metadata.json; the auto-captioned
        file only shows up in the in-memory job (or on disk). A first look
        change on an untouched clip must still put its captions back."""
        (job["dir"] / f"subtitled_5_{CLEAN}").write_bytes(b"gen-time captions")
        meta = _meta(job)
        meta["shorts"][0].pop("video_url", None)
        job["meta_path"].write_text(json.dumps(meta))
        app_module.jobs[JOB_ID]["result"]["clips"][0]["video_url"] = f"/videos/{JOB_ID}/subtitled_5_{CLEAN}"

        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "cinematic": LOOK})
        assert r.status_code == 200, r.text
        assert r.json()["captions"] is True
        assert fake_captions[0][0] == "stock"          # no caption_style -> stock look
        assert fake_fx[0][0] == CLEAN

    def test_uncaptioned_clip_stays_uncaptioned(self, job, fake_recut, fake_fx, fake_captions):
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "cinematic": LOOK})
        assert r.json()["captions"] is False and fake_captions == []


class TestFormatChange:
    def test_rerenders_from_source_with_the_recipe(self, job, fake_recut, fake_fx):
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "output_format": "1:1"})
        assert r.status_code == 200, r.text
        assert len(fake_recut) == 1
        call = fake_recut[0]
        assert call["input_path"].endswith("src.mp4")
        assert call["reframe"] is True and call["output_format"] == "square"
        assert call["segments"] == [{"start": 10.0, "end": 40.0}]
        assert call["clean_name"] == CLEAN
        assert callable(call["effects"]) and callable(call["captioner"])
        body = r.json()
        assert body["rerendered"] is True and body["output_format"] == "square"
        assert body["new_video_url"].endswith(f"/recut_1_{CLEAN}")
        meta = _meta(job)["shorts"][0]
        assert meta["output_format"] == "square"
        assert "layout_ranges" in meta
        assert fake_fx == []                          # no look on this clip

    def test_same_format_is_not_a_rerender(self, job, fake_recut, fake_fx):
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0,
                                                "output_format": "vertical", "cinematic": LOOK})
        assert r.status_code == 200 and fake_recut == [] and r.json()["rerendered"] is False

    def test_format_and_look_together_grade_the_new_render(self, job, fake_recut, fake_fx):
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0,
                                                "output_format": "horizontal", "cinematic": LOOK})
        assert r.status_code == 200, r.text
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("fx_") and f"_recut_1_{CLEAN}" in served and len(served) < 40 + len(CLEAN)
        assert fake_fx[0][0] == f"recut_1_{CLEAN}"

    def test_existing_look_survives_a_format_change(self, job, fake_recut, fake_fx):
        _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "cinematic": LOOK})
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "output_format": "square"})
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("fx_") and "recut_1_" in served
        assert r.json()["cinematic"]["color_grade"] == "warm"

    def test_gone_source_409(self, job, fake_recut):
        os.remove(job["dir"] / "src.mp4")
        r = _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "output_format": "square"})
        assert r.status_code == 409 and fake_recut == []

    def test_framing_and_hand_crops_ride_along(self, job, fake_recut):
        meta = _meta(job)
        meta["shorts"][0]["recipe"] = {"v": 1, "segments": [{"start": 10.0, "end": 40.0}],
                                       "canonical_range": {"start": 10.0, "end": 40.0}, "framing": "full"}
        meta["shorts"][0]["crop_overrides"] = {"2": 0.25}
        job["meta_path"].write_text(json.dumps(meta))
        _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "output_format": "square"})
        call = fake_recut[0]
        assert call["force_strategy"] == "WIDE"
        assert call["crop_overrides"] == {2: 0.25}


class TestLayerHelpers:
    def test_strip_all_layers_resolves_nested_prefixes(self, tmp_path):
        fx = f"fx_1_abc123_{CLEAN}"
        for name in (CLEAN, fx, f"hooked_2_{fx}", f"subtitled_3_hooked_2_{fx}"):
            (tmp_path / name).write_bytes(b"x")
        assert app_module._strip_all_layers(str(tmp_path), f"subtitled_3_hooked_2_{fx}") == CLEAN
        assert app_module._strip_cinematic(str(tmp_path), fx) == CLEAN
        # A missing underlying file stops the walk (library restore kept only the top).
        assert app_module._strip_cinematic(str(tmp_path), f"fx_7_abc123_gone_{CLEAN}") == f"fx_7_abc123_gone_{CLEAN}"

    def test_canonical_clip_file_prefers_the_fx_layer(self, tmp_path):
        (tmp_path / CLEAN).write_bytes(b"x")
        fx = tmp_path / f"fx_1_abc123_{CLEAN}"
        fx.write_bytes(b"y")
        os.utime(fx, (2_000_000_000, 2_000_000_000))
        assert app_module._canonical_clip_file(str(tmp_path), "mytitle", 0) == f"fx_1_abc123_{CLEAN}"

    def test_clip_output_format_prefers_the_clip(self):
        assert app_module._clip_output_format({}, {"output_format": "auto"}) == "vertical"
        assert app_module._clip_output_format({"output_format": "square"}, {"output_format": "auto"}) == "square"
        assert app_module._clip_output_format({}, {}) == "vertical"


class TestStyledSubtitleEndpoint:
    def test_caption_styles_and_fonts_are_public(self, job):
        styles = _request("GET", "/api/caption-styles")
        assert styles.status_code == 200
        body = styles.json()
        assert len(body["presets"]) == 19 and body["default"] == "bold_white"
        assert any(f["family"] == "Montserrat" for f in body["fonts"])
        assert len(body["themes"]) == 5 and len(body["position_grid"]) == 9
        assert _request("GET", "/api/fonts").json()["fonts"]

    def test_unknown_preset_400(self, job):
        r = _request("POST", "/api/subtitle", {"job_id": JOB_ID, "clip_index": 0, "preset": "nope"})
        assert r.status_code == 400

    def test_styled_burn_persists_the_normalized_style(self, job, monkeypatch):
        import subtitles as subs_module

        seen = {}

        def fake_ass(transcript, start, end, out, **kw):
            seen.update(kw)
            with open(out, "w") as f:
                f.write("[Script Info]")
            return True

        def fake_burn(video_path, srt_path, output_path, **kw):
            with open(output_path, "wb") as f:
                f.write(b"burned")

        monkeypatch.setattr(subs_module, "generate_ass_styled", fake_ass)
        monkeypatch.setattr(app_module, "burn_subtitles", fake_burn)

        r = _request("POST", "/api/subtitle", {
            "job_id": JOB_ID, "clip_index": 0, "preset": "beast_red",
            "overrides": {"pos_x": 50, "pos_y": 200, "bogus": 1, "glow_enabled": "true"}})
        assert r.status_code == 200, r.text
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("subtitled_") and served.endswith(f"_{CLEAN}")
        assert seen["preset"] == "beast_red"
        meta = _meta(job)["shorts"][0]
        assert meta["caption_style"] == {"preset": "beast_red",
                                         "overrides": {"pos_x": 50.0, "pos_y": 100.0, "glow_enabled": True}}
        assert app_module.jobs[JOB_ID]["result"]["clips"][0]["caption_style"]["preset"] == "beast_red"

    def test_legacy_request_clears_the_style(self, job, monkeypatch):
        meta = _meta(job)
        meta["shorts"][0]["caption_style"] = {"preset": "beast_red", "overrides": {}}
        job["meta_path"].write_text(json.dumps(meta))

        def fake_srt(transcript, start, end, out, *a, **kw):
            with open(out, "w") as f:
                f.write("1\n")
            return True

        def fake_burn(video_path, srt_path, output_path, **kw):
            with open(output_path, "wb") as f:
                f.write(b"burned")

        monkeypatch.setattr(app_module, "generate_srt", fake_srt)
        monkeypatch.setattr(app_module, "burn_subtitles", fake_burn)
        r = _request("POST", "/api/subtitle", {"job_id": JOB_ID, "clip_index": 0, "style": "classic"})
        assert r.status_code == 200, r.text
        assert _meta(job)["shorts"][0]["caption_style"] is None
