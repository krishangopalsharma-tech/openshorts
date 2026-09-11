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
    india, usa = ap.gemini_block("in"), ap.gemini_block("us")
    # The load-bearing parts, not the wording: the block has to outrank the
    # repo's own prompt, and the India profile has to demand Roman script or
    # the captions land in Devanagari that the Latin presets cannot draw.
    for block in (india, usa):
        assert "take priority" in block
    assert "ROMAN script" in india and "Hinglish" in india
    assert "Plain English" in usa


def test_the_profiles_claim_nothing_about_the_channel_itself():
    """The prose is a prompt, so an invented claim about the niche or the
    viewer steers every title and hook. Until there are real numbers the
    profiles say only what is known: the language, and what any clip must do
    to stand alone. Guard against the placeholder copy coming back."""
    invented = ("shaadi", "padosi", "WhatsApp", "Bollywood", "cricket",
                "personalfinance", "counterintuitive", "18-35", "desi")
    for key, profile in ap.PROFILES.items():
        for word in invented:
            assert word not in profile["context"], f"{word!r} back in {key}"


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


# --- who picked the clip ------------------------------------------------------
# Phase 2 of plan/VIRAL_UPGRADE_PLAN.md. This cannot be reconstructed after the
# fact, and it is the whole question the picker A/B asks, so it is stamped at
# the source and every export that projects fields has to name it.

def test_manual_clips_are_recorded_as_picked_by_claude(tmp_path):
    import json
    import manual_clips
    path = tmp_path / "clips.json"
    path.write_text(json.dumps({"shorts": [
        {"start": 1.0, "end": 20.0, "title": "t", "hook": "h", "description": "d"},
    ]}), encoding="utf-8")

    out = manual_clips.load_manual_clips(str(path), None, 120.0)
    assert out["shorts"][0]["picked_by"] == "claude"


def test_an_explicit_picked_by_in_the_json_is_kept(tmp_path):
    import json
    import manual_clips
    path = tmp_path / "clips.json"
    path.write_text(json.dumps({"shorts": [
        {"start": 1.0, "end": 20.0, "picked_by": "me, by hand"},
    ]}), encoding="utf-8")
    out = manual_clips.load_manual_clips(str(path), None, 120.0)
    assert out["shorts"][0]["picked_by"] == "me, by hand"


# --- how many clips the chat route asks for -----------------------------------

def _transcript(seconds, every=6.0):
    segs, t = [], 0.0
    while t < seconds:
        segs.append({"start": t, "end": min(t + every, seconds),
                     "text": "some spoken words here", "words": []})
        t += every
    return {"language": "en", "segments": segs}


def test_the_chat_brief_asks_for_the_same_band_gemini_gets():
    """The count was hardcoded "3 to 8" whatever the source, so a long episode
    was asked for fewer clips through the chat route than through the
    dashboard — and the picked_by A/B was then comparing a picker allowed
    6-12 against one allowed 3-8."""
    import manual_clips
    from clip_selection import clip_count_targets, build_transcript_windows

    duration = 3600.0
    tr = _transcript(duration)
    got = manual_clips.chat_clip_counts(tr, duration)

    # the same computation main.get_viral_clips does
    windows = build_transcript_windows(tr, duration, window_seconds=90)
    target = max(3, min(10, int(duration // 90) + 2))
    assert got == clip_count_targets(min(target, len(windows)))
    assert got[0] > 3, "a 60-minute source should ask for more than the old floor"


def test_a_short_source_asks_for_fewer_than_a_long_one():
    import manual_clips
    short = manual_clips.chat_clip_counts(_transcript(60.0), 60.0)
    long_ = manual_clips.chat_clip_counts(_transcript(3600.0), 3600.0)
    assert short[1] < long_[1], (short, long_)


def test_the_env_override_reaches_the_chat_route_too(monkeypatch):
    import manual_clips
    monkeypatch.setenv("CLIP_TARGET_MIN", "9")
    monkeypatch.setenv("CLIP_TARGET_MAX", "9")
    assert manual_clips.chat_clip_counts(_transcript(3600.0), 3600.0) == (9, 9)


def test_an_unusable_transcript_never_stops_the_export():
    """This only decides prompt wording. It must not be the reason
    --transcribe-only fails."""
    import manual_clips
    assert manual_clips.chat_clip_counts(None, 600.0) == (3, 8)
    assert manual_clips.chat_clip_counts({"segments": "not a list"}, 600.0) == (3, 8)


def test_the_band_reaches_the_written_prompt(tmp_path):
    import manual_clips
    duration = 3600.0
    out = manual_clips.write_ai_transcript(str(tmp_path), _transcript(duration),
                                           duration, "A show", "us")
    text = open(out, encoding="utf-8").read()
    lo, hi = manual_clips.chat_clip_counts(_transcript(duration), duration)
    assert f"Pick {lo} to {hi} clips" in text
    assert "Pick 3 to 8 clips" not in text
