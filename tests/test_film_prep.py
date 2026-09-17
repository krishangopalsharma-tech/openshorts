"""film_prep: SRT/VTT parsing, the internal transcript, the prompt digest, the
SRT offset probe and the speed step. Pure logic against a fixture SRT; ffmpeg
and whisper are never invoked (the probe takes an injected transcriber)."""

import os

import pytest

import film_prep as fp
import recut

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "film_sample.srt")


# --- timestamps -------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("00:00:12,400", 12.4),
    ("00:01:05.900", 65.9),
    ("01:42:10,000", 6130.0),
    ("42:10", 2530.0),
    ("132.5", 132.5),
    (7.0, 7.0),
])
def test_parse_timestamp(text, expected):
    assert fp.parse_timestamp(text) == pytest.approx(expected)


def test_parse_timestamp_rejects_garbage():
    with pytest.raises(fp.SubtitleError):
        fp.parse_timestamp("yesterday")


def test_format_timestamp():
    assert fp.format_timestamp(65) == "01:05"
    assert fp.format_timestamp(6130) == "01:42:10"
    assert fp.format_timestamp(840, hours=True) == "00:14:00"


# --- parsing ----------------------------------------------------------------

def test_parse_srt_fixture_round_trips():
    cues = fp.parse_subtitles(FIXTURE)
    assert len(cues) == 9
    first = cues[0]
    assert first["start"] == pytest.approx(12.4)
    assert first["end"] == pytest.approx(14.9)
    # Dialogue dashes and line breaks are typography, not speech.
    assert first["text"] == "Where are we? Boston Harbor."
    # Markup is stripped.
    assert cues[1]["text"] == "Pull yourself together, Teddy."
    assert cues[-1]["end"] == pytest.approx(6137.5)


def test_parse_srt_tolerates_missing_index_and_crlf():
    text = "00:00:01,000 --> 00:00:02,000\r\nHello there\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nAgain\r\n"
    cues = fp.parse_srt(text)
    assert [c["text"] for c in cues] == ["Hello there", "Again"]


def test_parse_vtt():
    text = ("WEBVTT\n\nNOTE made by hand\n\n"
            "intro\n00:12.400 --> 00:14.900 align:start\nWhere are we?\n\n"
            "00:01:02.000 --> 00:01:05.500\nI never had <b>any</b> children.\n")
    cues = fp.parse_vtt(text)
    assert len(cues) == 2
    assert cues[0]["start"] == pytest.approx(12.4)
    assert cues[1]["text"] == "I never had any children."


def test_parse_vtt_requires_header():
    with pytest.raises(fp.SubtitleError):
        fp.parse_vtt("00:00:01.000 --> 00:00:02.000\nhi\n")


def test_parse_subtitles_rejects_other_extensions(tmp_path):
    p = tmp_path / "film.txt"
    p.write_text("x")
    with pytest.raises(fp.SubtitleError):
        fp.parse_subtitles(str(p))


def test_parse_subtitles_decodes_cp1252(tmp_path):
    p = tmp_path / "film.srt"
    p.write_bytes("1\n00:00:01,000 --> 00:00:02,000\ncaf\xe9 noir\n".encode("cp1252"))
    cues = fp.parse_subtitles(str(p))
    assert cues[0]["text"] == "café noir"


# --- internal transcript ----------------------------------------------------

def test_cues_to_transcript_matches_tree_shape():
    cues = fp.parse_subtitles(FIXTURE)
    transcript = fp.cues_to_transcript(cues)
    assert transcript["language"] == "en"
    seg = transcript["segments"][3]  # "I never had any children."
    assert seg["start"] == pytest.approx(62.0)
    words = seg["words"]
    assert [w["word"] for w in words] == [" I", " never", " had", " any", " children."]
    # Even spacing inside the cue, monotonic, ends at the cue end.
    assert words[0]["start"] == pytest.approx(62.0)
    assert words[-1]["end"] == pytest.approx(65.5)
    for a, b in zip(words, words[1:]):
        assert a["end"] <= b["start"] + 1e-6
    # Longer words get longer slots.
    assert (words[4]["end"] - words[4]["start"]) > (words[0]["end"] - words[0]["start"])
    # And the existing helpers consume it unchanged.
    flat = recut.transcript_words(transcript)
    assert flat[0]["w"] == "Where"
    assert flat[-1]["e"] == pytest.approx(6137.5)


def test_cues_to_transcript_applies_offset():
    cues = fp.parse_srt("1\n00:00:10,000 --> 00:00:12,000\nhello world\n")
    t = fp.cues_to_transcript(cues, offset=3.4)
    assert t["segments"][0]["start"] == pytest.approx(13.4)
    assert t["offset"] == 3.4
    # Negative offsets clamp at zero rather than producing negative times.
    t2 = fp.cues_to_transcript(cues, offset=-11)
    assert t2["segments"][0]["start"] == 0.0


# --- digest -----------------------------------------------------------------

def test_merge_cues_drops_interjections_and_joins_close_lines():
    cues = fp.parse_subtitles(FIXTURE)
    merged = fp.merge_cues(cues)
    texts = [c["text"] for c in merged]
    # "Yeah." (1 word, 0.2 s after the previous) is absorbed, never alone.
    assert not any(t == "Yeah." for t in texts)
    # Cues 0.2 s apart become one line, and the interjection rides along.
    assert "Where are we? Boston Harbor. Pull yourself together, Teddy. Yeah." in texts
    assert "I never had any children. Your daughter, her name was Rachel." in texts
    # Cues a minute apart stay separate.
    assert len(merged) == 4


def test_digest_blocks_and_band():
    cues = fp.parse_subtitles(FIXTURE)
    text = fp.digest(cues)
    lines = text.splitlines()
    assert lines[0] == "[00:00:00-00:02:00]"
    assert lines[1].startswith("00:12 Where are we?")
    assert "[00:02:00-00:04:00]" in lines
    assert "[01:42:00-01:44:00]" in lines
    # A band digest carries only its act.
    band = fp.digest(cues, start=0, end=300)
    assert "monster" not in band
    assert "Boston" in band
    assert fp.digest_word_count(band) > 0


# --- offset probe -----------------------------------------------------------

def _asr_words_from(cues, shift, drop_every=0):
    """Fake what whisper would hear: the cue words, moved by ``shift`` in
    video time, with some words missing and one hallucinated."""
    words = []
    for i, w in enumerate(recut.transcript_words(fp.cues_to_transcript(cues))):
        if drop_every and i % drop_every == 0:
            continue
        words.append({"word": w["w"], "start": w["s"] + shift, "end": w["e"] + shift})
    words.append({"word": "banana", "start": 40.0, "end": 40.3})
    return words


def test_detect_offset_finds_a_positive_shift():
    cues = fp.parse_subtitles(FIXTURE)
    asr = _asr_words_from(cues, shift=+3.4, drop_every=4)
    result = fp.detect_offset(cues, asr, window_start=0, window_end=180)
    assert result["offset"] == pytest.approx(3.4, abs=0.3)
    assert result["confidence"] > 0.5
    assert result["matches"] >= 8


def test_detect_offset_finds_a_negative_shift():
    cues = fp.parse_subtitles(FIXTURE)
    asr = _asr_words_from(cues, shift=-12.0)
    result = fp.detect_offset(cues, asr, window_start=0, window_end=180)
    assert result["offset"] == pytest.approx(-12.0, abs=0.3)


def test_detect_offset_reports_no_confidence_without_matches():
    cues = fp.parse_subtitles(FIXTURE)
    result = fp.detect_offset(cues, [{"word": "zzz", "start": 1.0, "end": 1.2}], 0, 60)
    assert result == {"offset": 0.0, "confidence": 0.0, "matches": 0, "votes": 0}


def test_probe_window_sits_at_a_quarter():
    assert fp.probe_window(7200) == (1800.0, 1860.0)
    # Short inputs: the window is the whole file.
    assert fp.probe_window(30) == (0.0, 30.0)


def test_probe_offset_uses_injected_transcriber(monkeypatch, tmp_path):
    cues = fp.parse_subtitles(FIXTURE)
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        # The slice is written where ffmpeg was told to.
        open(cmd[-1], "wb").close()

    monkeypatch.setattr(fp.subprocess, "run", fake_run)

    def transcriber(path):
        # Whisper returns SLICE-relative times; probe_offset re-bases them.
        start, _ = fp.probe_window(240)
        segs = [{"start": 0, "end": 60, "words": [
            {"word": w["w"], "start": w["s"] + 2.0 - start, "end": w["e"] + 2.0 - start}
            for w in recut.transcript_words(fp.cues_to_transcript(cues))
            if start <= w["s"] < start + 60]}]
        return {"language": "en", "segments": segs}

    result = fp.probe_offset("film.mp4", cues, duration=240, transcriber=transcriber,
                             workdir=str(tmp_path))
    assert "-ss" in calls["cmd"] and "film.mp4" in calls["cmd"]
    assert result["window"] == [60.0, 120.0]
    assert result["offset"] == pytest.approx(2.0, abs=0.3)


def test_probe_offset_never_raises(monkeypatch):
    def boom(cmd, **kw):
        raise RuntimeError("no ffmpeg here")
    monkeypatch.setattr(fp.subprocess, "run", boom)
    result = fp.probe_offset("film.mp4", [], duration=100)
    assert result["offset"] == 0.0 and result["confidence"] == 0.0
    assert "no ffmpeg" in result["error"]


# --- speed ------------------------------------------------------------------

def test_speed_command_shape():
    cmd = fp.speed_command("in.mp4", "out.mp4", 1.25, fps=60,
                           encode_args=["-c:v", "libx264"], audio_args=["-c:a", "aac"])
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-filter:v") + 1] == "setpts=PTS/1.2500,fps=60"
    assert cmd[cmd.index("-filter:a") + 1] == "atempo=1.2500"
    assert cmd[-1] == "out.mp4"
    # No loudnorm here: the mix step normalises once at the end.
    assert not any("loudnorm" in a for a in cmd)


def test_speed_command_rejects_out_of_range():
    with pytest.raises(ValueError):
        fp.speed_command("a", "b", 3.0, encode_args=[], audio_args=[])


def test_run_speed_uses_runner():
    seen = []
    fp.run_speed("a.mp4", "b.mp4", 1.25, runner=seen.append)
    assert seen and seen[0][-1] == "b.mp4"


def test_scale_transcript_divides_every_time():
    cues = fp.parse_subtitles(FIXTURE)
    t = fp.cues_to_transcript(cues)
    s = fp.scale_transcript(t, 1.25)
    assert s["segments"][0]["start"] == pytest.approx(12.4 / 1.25)
    w0, w1 = t["segments"][0]["words"][0], s["segments"][0]["words"][0]
    assert w1["start"] == pytest.approx(w0["start"] / 1.25)
    assert w1["word"] == w0["word"]
    assert s["language"] == "en"
    assert fp.scale_ranges([{"start": 10, "end": 20}], 2)[0] == {"start": 5.0, "end": 10.0}
