import json
import os
import pytest
import numpy as np
from PIL import Image


def test_comedy_card_overlay_geometry_and_verified_badge(tmp_path):
    import comedy_card

    out_png = tmp_path / "card.png"
    box = comedy_card.render_card_overlay(
        str(out_png),
        channel_name="Cheerful Videos",
        handle="@Cheerfulvids",
        pov_text="Zero survival instinct 😂",
        verified=True,
        avatar_path=None,
    )
    assert out_png.exists()
    img = Image.open(out_png)
    assert img.size == (1080, 1920)
    assert img.mode == "RGBA"

    arr = np.array(img)
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    # Upward-shifted header + taller video window ("Needed this" reference)
    assert 900 <= w <= 1020
    assert 280 <= y <= 340
    assert 1380 <= h <= 1500
    assert abs((1080 - w) // 2 - x) <= 2

    # Outer canvas margin (top-left) is solid white
    assert tuple(arr[10, 10]) == (255, 255, 255, 255)
    # Center of video window is transparent so video shows through
    cx, cy = x + w // 2, y + h // 2
    assert arr[cy, cx, 3] == 0
    # Extreme corner of the video window is opaque white (rounded corner mask)
    assert arr[y, x, 3] == 255

    # Header region above the video box contains dark text pixels and Twitter-blue verified badge pixels
    header = arr[:y, :, :]
    blue_mask = (
        (header[:, :, 0] < 60)
        & (header[:, :, 1] > 130)
        & (header[:, :, 2] > 210)
        & (header[:, :, 3] > 200)
    )
    assert blue_mask.any(), "Expected Twitter/X blue verified checkmark pixels in header"


def test_comedy_card_without_verified_badge_omits_blue_check(tmp_path):
    import comedy_card

    out_png = tmp_path / "card_unverified.png"
    box = comedy_card.render_card_overlay(
        str(out_png),
        channel_name="Cheerful Videos",
        handle="@Cheerfulvids",
        pov_text="Wait for the ending",
        verified=False,
        avatar_path=None,
    )
    arr = np.array(Image.open(out_png))
    header = arr[: box["y"], 220:, :]  # Right of avatar
    blue_mask = (
        (header[:, :, 0] < 60)
        & (header[:, :, 1] > 130)
        & (header[:, :, 2] > 210)
        & (header[:, :, 3] > 200)
    )
    assert not blue_mask.any()


def test_metadata_hard_gate_blocks_missing_fields():
    import comedy_studio

    # Completely empty metadata -> blocked
    with pytest.raises(ValueError, match="title"):
        comedy_studio.validate_comedy_metadata({})

    # Series missing season or episode -> blocked
    with pytest.raises(ValueError, match="season"):
        comedy_studio.validate_comedy_metadata({
            "content_type": "series",
            "title": "The Office",
            "season": "",
            "episode": "12",
        })

    with pytest.raises(ValueError, match="episode"):
        comedy_studio.validate_comedy_metadata({
            "content_type": "series",
            "title": "The Office",
            "season": "2",
            "episode": "",
        })

    # Movie only needs title
    movie_meta = comedy_studio.validate_comedy_metadata({
        "content_type": "movie",
        "title": "Superbad",
        "year": "2007",
    })
    assert movie_meta["content_type"] == "movie"
    assert movie_meta["title"] == "Superbad"

    # Series with title + season + episode succeeds
    series_meta = comedy_studio.validate_comedy_metadata({
        "content_type": "series",
        "title": "The Office",
        "season": "5",
        "episode": "14",
        "episode_title": "Stress Relief",
    })
    assert series_meta["content_type"] == "series"
    assert series_meta["season"] == "5"
    assert series_meta["episode"] == "14"


def test_build_comedy_prompt_requires_metadata_and_includes_database_instructions():
    import comedy_studio

    transcript = {
        "language": "en",
        "segments": [
            {"start": 10.0, "end": 14.5, "text": "Today, smoking is going to save lives."},
            {"start": 14.5, "end": 19.0, "text": "Does anyone smell anything smoky?"},
        ],
    }

    # Missing metadata must hard-fail and refuse to build prompt
    with pytest.raises(ValueError):
        comedy_studio.build_comedy_prompt(transcript, 1200.0, {"content_type": "series", "title": "The Office"})

    prompt = comedy_studio.build_comedy_prompt(
        transcript,
        1200.0,
        {
            "content_type": "series",
            "title": "The Office",
            "season": "5",
            "episode": "14",
            "episode_title": "Stress Relief",
        },
    )
    assert "The Office" in prompt
    assert "Season 5" in prompt
    assert "Episode 14" in prompt
    assert "internet" in prompt.lower() or "imdb" in prompt.lower()
    assert "pov_text" in prompt
    assert "[10.0-14.5] Today, smoking is going to save lives." in prompt


def test_verify_and_align_transcript_shifts_timestamps():
    import comedy_studio

    srt_text = (
        "1\n"
        "00:00:10,000 --> 00:00:14,000\n"
        "Today smoking is going to save lives.\n\n"
        "2\n"
        "00:00:15,000 --> 00:00:18,000\n"
        "Everyone stay calm!\n"
    )

    def fake_prober(video_path, cues, duration):
        return {"offset": 2.5, "confidence": 0.88, "matches": 24, "votes": 30}

    aligned = comedy_studio.verify_and_align_transcript(
        "dummy.mp4",
        srt_text,
        duration=600.0,
        prober=fake_prober,
    )
    assert aligned["alignment"]["verified"] is True
    assert aligned["alignment"]["offset"] == 2.5
    assert aligned["alignment"]["confidence"] == 0.88
    assert aligned["segments"][0]["start"] == pytest.approx(12.5, abs=0.05)
    assert aligned["segments"][0]["end"] == pytest.approx(16.5, abs=0.05)


def test_manual_clips_preserves_pov_text(tmp_path):
    import manual_clips

    clips_file = tmp_path / "clips.json"
    clips_file.write_text(
        json.dumps([
            {
                "start": 10.0,
                "end": 35.0,
                "title": "Dwight's Fire Drill",
                "pov_text": "Zero survival instinct 😂",
                "why_funny": "Cold open chaos",
            }
        ]),
        encoding="utf-8",
    )
    transcript = {
        "language": "en",
        "segments": [
            {
                "start": 10.0,
                "end": 35.0,
                "text": "Today smoking is going to save lives.",
                "words": [
                    {"word": "Today", "start": 10.0, "end": 10.5},
                    {"word": "lives.", "start": 34.5, "end": 35.0},
                ],
            }
        ],
    }
    loaded = manual_clips.load_manual_clips(str(clips_file), transcript, 120.0)
    assert loaded["shorts"][0]["pov_text"] == "Zero survival instinct 😂"
    assert loaded["shorts"][0]["viral_hook_text"] == "Zero survival instinct 😂"
    assert loaded["shorts"][0]["video_tags"] != ""
    assert "comedy" in loaded["shorts"][0]["video_tags"].lower()



def test_api_comedy_endpoints_hard_gate_and_prompt_build(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_mod
    import comedy_studio

    monkeypatch.setattr(app_mod, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(app_mod, "UPLOAD_DIR", str(tmp_path / "uploads"))
    os.makedirs(app_mod.OUTPUT_DIR, exist_ok=True)
    os.makedirs(app_mod.UPLOAD_DIR, exist_ok=True)

    dummy_video = tmp_path / "episode.mp4"
    dummy_video.write_bytes(b"fake video bytes")
    monkeypatch.setattr(app_mod, "_media_duration_seconds", lambda p: 600.0)

    def fake_prober(video_path, cues, duration):
        return {"offset": 1.2, "confidence": 0.91, "matches": 18, "votes": 20}

    monkeypatch.setattr(comedy_studio.film_prep, "probe_offset", fake_prober)

    client = TestClient(app_mod.app)

    # 1. Profile GET & POST
    res = client.get("/api/comedy/profile")
    assert res.status_code == 200
    assert res.json()["channel_name"] == "Cheerful Videos"

    res = client.post("/api/comedy/profile", json={
        "channel_name": "Daily Comedy",
        "handle": "dailycomedy",
        "verified": True,
        "burn_subtitles": False,
    })
    assert res.status_code == 200
    assert res.json()["channel_name"] == "Daily Comedy"

    # 2. Preview Card PNG
    res = client.get("/api/comedy/preview-card", params={"pov_text": "Zero survival instinct 😂"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"

    # 3. Hard Metadata Gate on /api/comedy/build-prompt -> must return 400 if forgotten
    res = client.post("/api/comedy/build-prompt", json={
        "video_path": str(dummy_video),
        "metadata": {"content_type": "series", "title": "The Office", "season": "5", "episode": ""},
        "transcript_text": "0:10 Today smoking is going to save lives.\n0:15 Everyone stay calm!",
    })
    assert res.status_code == 400
    assert "episode" in res.json()["detail"].lower()

    # 4. Valid Metadata + Transcript -> Whisper aligns & builds prompt
    res = client.post("/api/comedy/build-prompt", json={
        "video_path": str(dummy_video),
        "metadata": {
            "content_type": "series",
            "title": "The Office",
            "season": "5",
            "episode": "14",
            "episode_title": "Stress Relief",
        },
        "transcript_text": "0:10 Today smoking is going to save lives.\n0:15 Everyone stay calm!",
    })
    assert res.status_code == 200
    data = res.json()
    assert "job_id" in data
    assert "The Office" in data["text"]
    assert "Season 5" in data["text"]
    assert data["alignment"]["verified"] is True
    assert data["alignment"]["offset"] == 1.2


def test_apply_comedy_card_ffmpeg_end_to_end(tmp_path):
    import subprocess
    import cv2
    import comedy_card

    in_mp4 = tmp_path / "source_clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=1080x1920:d=1:r=24",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(in_mp4),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    comedy_card.apply_comedy_card(
        str(in_mp4),
        channel_name="Cheerful Videos",
        handle="@Cheerfulvids",
        pov_text="Zero survival instinct 😂",
        verified=True,
    )

    cap = cv2.VideoCapture(str(in_mp4))
    ok, frame = cap.read()
    cap.release()
    assert ok
    assert frame.shape[:2] == (1920, 1080)
    # Top-left margin is white (BGR ~ 255, 255, 255)
    assert frame[20, 20].mean() > 240
    # Center of video window is red (BGR ~ 0, 0, 255 -> R > 180, B < 60)
    b, g, r = frame[1000, 540]
    assert r > 180 and b < 60


def test_comedy_card_keeps_canonical_clip_uncaptioned_for_subtitle_editor(tmp_path):
    """Regression test: comedy_card must apply onto the clean canonical clip_N.mp4
    BEFORE auto_caption_clip writes subtitled_<ts>_clip_N.mp4, so opening the
    Subtitle Editor or restyling via /api/subtitle strips subtitled_<ts>_ and
    never shows double captions."""
    import app as app_mod

    clean_clip = tmp_path / "show_clip_1.mp4"
    subtitled_clip = tmp_path / "subtitled_1794484500_show_clip_1.mp4"
    clean_clip.write_bytes(b"clean comedy card video")
    subtitled_clip.write_bytes(b"captioned comedy card video")

    stripped = app_mod._strip_burned_captions(str(tmp_path), subtitled_clip.name)
    assert stripped == "show_clip_1.mp4"
    assert app_mod._canonical_clip_file(str(tmp_path), "show", 0) == subtitled_clip.name


def test_pov_text_and_emoji_vertical_alignment_without_head_clipping(tmp_path):
    """Verify the POV text and emoji sit on the same vertical center line and
    the top of the emoji is never clipped off."""
    import comedy_card

    out_png = tmp_path / "card_emoji.png"
    comedy_card.render_card_overlay(
        str(out_png),
        channel_name="Mockingbirds",
        handle="@Mockingbirds",
        pov_text="Who needs Lawson? 😂",
        verified=True,
    )
    arr = np.array(Image.open(out_png))
    # POV line sits around y=180..260
    txt_region = arr[180:260, 70:460, :3]
    emj_region = arr[180:260, 470:560, :3]
    txt_ys = np.where(txt_region.mean(axis=2) < 240)[0] + 180
    emj_ys = np.where(emj_region.mean(axis=2) < 245)[0] + 180

    txt_center = (txt_ys.min() + txt_ys.max()) / 2.0
    emj_center = (emj_ys.min() + emj_ys.max()) / 2.0
    assert abs(txt_center - emj_center) <= 2.0
    # Full emoji height (~45px at font size 44), not clipped to 39px
    assert (emj_ys.max() - emj_ys.min() + 1) >= 43




