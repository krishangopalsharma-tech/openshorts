"""India / USA profiles: picked by transcript language, feed Gemini and chat prompts."""
import json

import audience_profiles as ap
import manual_clips


def test_pick_by_language(monkeypatch):
    monkeypatch.delenv("AUDIENCE", raising=False)
    assert ap.pick("hinglish") == "in"
    assert ap.pick("hi") == "in"
    assert ap.pick("ur") == "in"
    assert ap.pick("en") == "us"
    assert ap.pick("es") is None


def test_explicit_audience_beats_language(monkeypatch):
    monkeypatch.setenv("AUDIENCE", "us")
    assert ap.pick("hinglish") == "us"
    assert ap.pick("hi", override="in") == "in"


def test_cli_defaults_never_override_the_env(monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_LANGUAGE", "hi")
    for k in ("CLIP_MIN_SECONDS", "CLIP_MAX_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    ap.apply_transcription_settings("in")
    import os
    assert os.environ["TRANSCRIBE_LANGUAGE"] == "hi"      # env wins
    assert os.environ["CLIP_MIN_SECONDS"] == "20"         # profile fills the gap


def test_gemini_block_only_with_a_profile():
    assert ap.gemini_block(None) == ""
    assert "Hinglish" in ap.gemini_block("in")
    assert "American English" in ap.gemini_block("us")


def test_chat_round_trip(tmp_path):
    tr = {"language": "hinglish", "segments": [
        {"start": float(i), "end": i + 0.9, "text": f"line {i}",
         "words": [{"word": f" w{i}", "start": float(i), "end": i + 0.9}]}
        for i in range(120)]}
    path = manual_clips.write_ai_transcript(str(tmp_path), tr, 120.0, "ep1", "in")
    text = open(path, encoding="utf-8").read()
    assert "Hinglish" in text and "[5.0-5.9] line 5" in text

    reply = 'Here you go:\n```json\n{"shorts":[{"start":"0:10","end":"0:40",' \
            '"title":"T","hook":"H","tags":"a, b"}]}\n```'
    clips = tmp_path / "clips.json"
    clips.write_text(reply, encoding="utf-8")
    shorts = manual_clips.load_manual_clips(str(clips), tr, 120.0)["shorts"]
    assert len(shorts) == 1
    assert 9.5 <= shorts[0]["start"] <= 10.5
    assert shorts[0]["video_tags"] == "a, b"
    assert shorts[0]["viral_hook_text"] == "H"
