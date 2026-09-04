"""overlays.py (spec + PIL layer) and POST /api/clip/overlays as the outermost
under-caption layer (fx -> music -> overlay)."""

import asyncio
import json
import os

import httpx
import pytest
from PIL import Image

app_module = pytest.importorskip("app")
import cinematic  # noqa: E402
import music  # noqa: E402
import overlays  # noqa: E402

JOB_ID = "overlay-endpoint-test-job"
CLEAN = "mytitle_clip_1.mp4"
LOGO = "abc123abc123_logo.png"


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
    lib = tmp_path / "overlays"
    lib.mkdir()
    Image.new("RGBA", (200, 80), (255, 0, 0, 255)).save(lib / LOGO)
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(overlays, "OVERLAYS_DIR", str(lib))
    monkeypatch.setattr(music, "MUSIC_DIR", str(tmp_path / "music"))
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "beat.mp3").write_bytes(b"x")

    clip = {"start": 10.0, "end": 40.0, "video_title_for_youtube_short": "t",
            "video_url": f"/videos/{JOB_ID}/{CLEAN}"}
    meta = {"shorts": [clip], "transcript": {"language": "en", "segments": []},
            "source_video": "src.mp4", "output_format": "auto", "cost_analysis": {}}
    (job_dir / "mytitle_metadata.json").write_text(json.dumps(meta))
    (job_dir / CLEAN).write_bytes(b"canonical")
    app_module.jobs[JOB_ID] = {"status": "completed", "logs": [],
                               "result": {"clips": [dict(clip)], "cost_analysis": {}},
                               "user_id": None, "watermark": False}
    try:
        yield {"dir": job_dir, "meta_path": job_dir / "mytitle_metadata.json", "lib": lib}
    finally:
        app_module.jobs.pop(JOB_ID, None)


@pytest.fixture()
def fake_passes(monkeypatch):
    """Every under-caption pass stubbed: records (layer, input name)."""
    calls = []

    def fx(video_path, effects, output_path=None):
        calls.append(("fx", os.path.basename(video_path)))
        open(output_path or video_path, "wb").write(b"g")
        return True

    def mix(video_path, spec, output_path, music_dir=None):
        calls.append(("music", os.path.basename(video_path)))
        open(output_path, "wb").write(b"m")
        return True

    def ov(video_path, items, output_path, overlays_dir=None):
        calls.append(("overlay", os.path.basename(video_path)))
        open(output_path, "wb").write(b"o")
        return True

    monkeypatch.setattr(cinematic, "apply_cinematic_effects", fx)
    monkeypatch.setattr(music, "apply_music", mix)
    monkeypatch.setattr(overlays, "apply_overlays", ov)
    return calls


ITEMS = [
    {"type": "image", "asset": LOGO, "x": 0.9, "y": 0.1, "w": 0.2},
    {"type": "text", "text": "Hello\nworld", "x": 0.5, "y": 0.9, "w": 0.8, "size": 0.05,
     "font_family": "Anton", "color": "ffd400", "bg_opacity": 0.5},
]


class TestSpec:
    def test_normalize_clamps_and_drops_junk(self):
        items = overlays.normalize(ITEMS + [{"type": "nope"}, "junk", {"type": "text", "text": "   "}])
        assert [i["type"] for i in items] == ["image", "text"]
        assert items[0]["opacity"] == 1.0 and items[0]["asset"] == LOGO
        assert items[1]["color"] == "#FFD400" and items[1]["align"] == "center"
        assert overlays.normalize([{"type": "text", "text": "x", "x": 3, "w": 0, "size": 9}])[0]["x"] == 1.0
        assert overlays.normalize("nope") == [] and overlays.normalize([]) == []

    def test_text_is_capped(self):
        item = overlays.normalize([{"type": "text", "text": "a\nb\nc\nd\ne\nf"}])[0]
        assert item["text"].count("\n") == 3
        assert len(overlays.normalize([{"type": "text", "text": "x" * 500}])[0]["text"]) == 200

    def test_layer_renders_logo_and_text_at_fractions(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        Image.new("RGBA", (100, 50), (0, 255, 0, 255)).save(lib / LOGO)
        items = [{"type": "image", "asset": LOGO, "x": 0.5, "y": 0.5, "w": 0.5},
                 {"type": "text", "text": "HI", "x": 0.5, "y": 0.1, "w": 0.6, "size": 0.08, "color": "#FF0000",
                  "outline": 0, "bg_opacity": 1.0, "bg_color": "#0000FF"}]
        layer = overlays.render_layer(items, 400, 800, str(lib))
        assert layer.size == (400, 800)
        # logo: 200px wide (0.5 * 400), centred -> green at the frame centre
        assert layer.getpixel((200, 400))[:3] == (0, 255, 0)
        assert layer.getpixel((60, 400))[3] == 0                    # outside the 200px logo
        # text box: blue background around y = 10% of 800 = 80
        assert layer.getpixel((90, 80))[:3] == (0, 0, 255)
        assert layer.getpixel((5, 80))[3] == 0                      # outside the 60% box

    def test_missing_asset_and_empty_list_draw_nothing(self, tmp_path):
        assert overlays.render_layer([{"type": "image", "asset": "gone.png", "x": .5, "y": .5, "w": .3}],
                                     100, 100, str(tmp_path)) is None
        assert overlays.render_layer([], 100, 100, str(tmp_path)) is None

    def test_asset_library_roundtrip(self, tmp_path):
        from io import BytesIO
        buf = BytesIO()
        Image.new("RGB", (30, 20), (1, 2, 3)).save(buf, format="PNG")
        buf.seek(0)
        entry = overlays.save_asset("My Logo.png", buf, str(tmp_path))
        assert entry["width"] == 30 and entry["name"] == "My Logo"
        assert overlays.list_assets(str(tmp_path))[0]["file"] == entry["file"]
        assert overlays.resolve_asset("../" + entry["file"], str(tmp_path)).endswith(entry["file"])
        with pytest.raises(ValueError):
            overlays.save_asset("x.gif", BytesIO(b"gif"), str(tmp_path))
        with pytest.raises(ValueError):
            overlays.save_asset("x.png", BytesIO(b"not an image"), str(tmp_path))


class TestEndpoint:
    def test_missing_logo_404(self, job):
        r = _request("POST", "/api/clip/overlays", {"job_id": JOB_ID, "clip_index": 0,
                                                    "overlays": [{"type": "image", "asset": "nope.png"}]})
        assert r.status_code == 404

    def test_assets_listing(self, job):
        r = _request("GET", "/api/overlays/assets")
        assert r.status_code == 200 and r.json()["assets"][0]["file"] == LOGO

    def test_overlay_layer_sits_above_music_and_fx(self, job, fake_passes):
        _request("POST", "/api/clip/look", {"job_id": JOB_ID, "clip_index": 0, "cinematic": {"color_grade": "warm"}})
        _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0, "music": {"track": "beat.mp3"}})
        r = _request("POST", "/api/clip/overlays", {"job_id": JOB_ID, "clip_index": 0, "overlays": ITEMS})
        assert r.status_code == 200, r.text
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("ov_") and "_mu_" in served and "_fx_" in served
        layers = [c[0] for c in fake_passes]
        assert layers == ["fx", "music", "overlay"]           # nothing below was re-run
        assert fake_passes[-1][1].startswith("mu_")
        meta = _meta(job)["shorts"][0]
        assert [i["type"] for i in meta["overlays"]] == ["image", "text"]
        assert meta["overlays"][1]["color"] == "#FFD400"

    def test_music_change_reapplies_overlays_on_top(self, job, fake_passes):
        _request("POST", "/api/clip/overlays", {"job_id": JOB_ID, "clip_index": 0, "overlays": ITEMS})
        r = _request("POST", "/api/clip/music", {"job_id": JOB_ID, "clip_index": 0, "music": {"track": "beat.mp3"}})
        served = r.json()["new_video_url"].split("/")[-1]
        assert served.startswith("ov_") and "_mu_" in served
        assert [c[0] for c in fake_passes] == ["overlay", "music", "overlay"]
        assert fake_passes[-1][1].startswith("mu_")

    def test_empty_list_removes_the_layer(self, job, fake_passes):
        _request("POST", "/api/clip/overlays", {"job_id": JOB_ID, "clip_index": 0, "overlays": ITEMS})
        r = _request("POST", "/api/clip/overlays", {"job_id": JOB_ID, "clip_index": 0, "overlays": []})
        assert r.status_code == 200
        assert r.json()["new_video_url"].endswith(f"/{CLEAN}") and r.json()["overlays"] == []
        assert _meta(job)["shorts"][0]["overlays"] == []

    def test_strip_all_layers_walks_the_full_stack(self, tmp_path):
        fx = f"fx_1_abc123_{CLEAN}"
        mus = f"mu_abc123_{fx}"
        ov = f"ov_abc123_{mus}"
        for n in (CLEAN, fx, mus, ov, f"subtitled_4_{ov}"):
            (tmp_path / n).write_bytes(b"x")
        assert app_module._strip_all_layers(str(tmp_path), f"subtitled_4_{ov}") == CLEAN
        assert app_module._strip_overlay(str(tmp_path), ov) == mus
