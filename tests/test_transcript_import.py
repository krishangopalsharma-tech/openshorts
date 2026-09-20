"""Importing a transcript someone already has (transcript_import.py).

The point of the module is to spend zero GPU on a file we were handed, so
what matters is that both shapes come out as something ``main.py
--transcript`` accepts, and that unusable input says why instead of
producing a transcript full of empty segments.
"""

import json

import pytest

import transcript_import as ti


# --- plain timestamped text (YouTube's transcript panel) --------------------

class TestTimestampedText:
    def test_timestamp_and_text_on_one_line(self):
        out = ti.parse_timestamped_text(
            "0:14 so I said no\n0:18 and he left\n0:25 that was that")
        assert [s["text"] for s in out["segments"]] == [
            "so I said no", "and he left", "that was that"]
        assert out["segments"][0]["start"] == 14.0

    def test_timestamp_on_its_own_line_is_the_same_cue(self):
        """A plain copy out of YouTube's panel puts the time above the line."""
        out = ti.parse_timestamped_text("0:14\nso I said no\n0:18\nand he left")
        assert [s["text"] for s in out["segments"]] == ["so I said no", "and he left"]

    def test_a_cue_runs_until_the_next_one(self):
        out = ti.parse_timestamped_text("0:10 one\n0:30 two")
        assert out["segments"][0]["end"] == 30.0

    def test_the_last_cue_gets_a_bounded_tail(self):
        out = ti.parse_timestamped_text("0:10 only line")
        last = out["segments"][-1]
        assert last["end"] == pytest.approx(10.0 + ti.DEFAULT_TAIL_SECONDS)

    def test_hours_are_read(self):
        out = ti.parse_timestamped_text("1:02:33 late in the film")
        assert out["segments"][0]["start"] == 3753.0

    def test_bracketed_and_dotted_forms(self):
        out = ti.parse_timestamped_text("[00:00:14.500] bracketed")
        assert out["segments"][0]["start"] == pytest.approx(14.5)

    def test_a_time_mid_sentence_is_speech_not_a_cue(self):
        """'we start at 3:30' must not split the line it sits in."""
        out = ti.parse_timestamped_text("0:05 we start at 3:30 sharp")
        assert len(out["segments"]) == 1
        assert out["segments"][0]["text"] == "we start at 3:30 sharp"

    def test_a_header_before_the_first_timestamp_is_dropped(self):
        out = ti.parse_timestamped_text("Transcript\nEnglish (auto-generated)\n0:02 hello")
        assert [s["text"] for s in out["segments"]] == ["hello"]

    def test_words_are_spread_across_the_cue(self):
        """No word timings exist, so film_prep spreads them — and the words
        must carry whisper's leading space or merge_continuation_words breaks."""
        out = ti.parse_timestamped_text("0:00 alpha beta\n0:10 x")
        words = out["segments"][0]["words"]
        assert [w["word"] for w in words] == [" alpha", " beta"]
        assert words[0]["start"] == 0.0
        assert words[-1]["end"] <= 10.0

    def test_no_timestamps_says_so(self):
        with pytest.raises(ti.TranscriptImportError) as e:
            ti.parse_timestamped_text("just some prose with no times in it")
        assert "timestamp" in str(e.value).lower()


# --- whisper json -----------------------------------------------------------

class TestWhisperJson:
    def _doc(self):
        return json.dumps({
            "language": "en",
            "segments": [
                {"start": 0.0, "end": 2.0, "text": "hello there",
                 "words": [{"word": " hello", "start": 0.0, "end": 1.0},
                           {"word": " there", "start": 1.0, "end": 2.0}]},
                {"start": 2.0, "end": 4.0, "text": "second line"},
            ],
        })

    def test_word_timings_survive(self):
        out = ti.parse_whisper_json(self._doc())
        assert out["segments"][0]["words"][1]["start"] == 1.0
        assert out["language"] == "en"

    def test_a_segment_without_words_is_kept(self):
        """The moment picker reads text; only captions need words."""
        out = ti.parse_whisper_json(self._doc())
        assert out["segments"][1]["text"] == "second line"
        assert out["segments"][1]["words"] == []

    def test_a_bare_word_gets_whispers_leading_space(self):
        doc = json.dumps({"segments": [
            {"start": 0, "end": 1, "text": "hi",
             "words": [{"word": "hi", "start": 0, "end": 1}]}]})
        assert ti.parse_whisper_json(doc)["segments"][0]["words"][0]["word"] == " hi"

    def test_empty_segments_are_dropped_and_an_empty_file_is_refused(self):
        doc = json.dumps({"segments": [{"start": 0, "end": 1, "text": "   "}]})
        with pytest.raises(ti.TranscriptImportError):
            ti.parse_whisper_json(doc)

    def test_broken_json_names_the_problem(self):
        with pytest.raises(ti.TranscriptImportError) as e:
            ti.parse_whisper_json("{not json")
        assert "could not be read" in str(e.value).lower()

    def test_json_without_segments_is_refused(self):
        with pytest.raises(ti.TranscriptImportError) as e:
            ti.parse_whisper_json(json.dumps({"text": "hello"}))
        assert "segments" in str(e.value)


# --- dispatch ---------------------------------------------------------------

class TestLoad:
    def test_content_decides_not_the_extension(self):
        """A .txt holding JSON is still JSON — people rename these."""
        doc = json.dumps({"segments": [{"start": 0, "end": 1, "text": "hi"}]})
        assert ti.load_transcript(doc, "notes.txt")["source"] == "imported_json"

    def test_plain_text_routes_to_the_cue_parser(self):
        assert ti.load_transcript("0:01 hello", "yt.txt")["source"] == "imported_text"

    def test_srt_routes_to_srt_parser(self):
        srt = "1\n00:00:01,000 --> 00:00:04,000\nHello from SRT\n"
        out = ti.load_transcript(srt, "captions.srt")
        assert out["source"] == "imported_srt"
        assert out["segments"][0]["text"] == "Hello from SRT"

    def test_vtt_routes_to_vtt_parser(self):
        vtt = "WEBVTT\n\n00:01.000 --> 00:04.000\nHello from VTT\n"
        out = ti.load_transcript(vtt, "captions.vtt")
        assert out["source"] == "imported_vtt"
        assert out["segments"][0]["text"] == "Hello from VTT"

    def test_empty_is_refused(self):
        with pytest.raises(ti.TranscriptImportError):
            ti.load_transcript("   ", "x.txt")

    def test_a_json_extension_forces_the_json_error_not_a_cue_error(self):
        """Told it is JSON, a broken file must complain about JSON — saying
        'no timestamps found' about a .json file sends the user the wrong way."""
        with pytest.raises(ti.TranscriptImportError) as e:
            ti.load_transcript("{broken", "t.json")
        assert "json" in str(e.value).lower()


# --- SRT and WebVTT ---------------------------------------------------------

class TestSubtitles:
    def test_srt_parses_multiline_cues_and_spreads_words(self):
        srt = (
            "1\n"
            "00:00:02,500 --> 00:00:05,000\n"
            "First line of dialogue\n\n"
            "2\n"
            "00:00:06,000 --> 00:00:09,500\n"
            "Second cue here\n"
        )
        out = ti.parse_srt_transcript(srt)
        assert len(out["segments"]) == 2
        assert out["segments"][0]["start"] == 2.5
        assert out["segments"][0]["end"] == 5.0
        assert out["segments"][0]["text"] == "First line of dialogue"
        words = out["segments"][0]["words"]
        assert len(words) == 4
        assert [w["word"] for w in words] == [" First", " line", " of", " dialogue"]

    def test_vtt_parses_webvtt_header_and_cues(self):
        vtt = (
            "WEBVTT - YouTube captions\n\n"
            "NOTE this is a comment\n\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "Caption one\n\n"
            "00:00:04.000 --> 00:00:06.000\n"
            "Caption two\n"
        )
        out = ti.parse_vtt_transcript(vtt)
        assert len(out["segments"]) == 2
        assert out["segments"][0]["start"] == 1.0
        assert out["segments"][0]["text"] == "Caption one"

    def test_srt_without_cues_raises_error(self):
        with pytest.raises(ti.TranscriptImportError) as e:
            ti.parse_srt_transcript("not an srt file at all")
        assert "no subtitle cues found" in str(e.value).lower()

    def test_vtt_without_webvtt_header_raises_error(self):
        with pytest.raises(ti.TranscriptImportError) as e:
            ti.parse_vtt_transcript("00:01.000 --> 00:03.000\nno header")
        assert "not a webvtt file" in str(e.value).lower()


class TestDescribe:
    def test_says_which_precision_was_imported(self):
        text = ti.describe(ti.load_transcript("0:00 a b\n1:00 c", "x.txt"))
        assert "line timings" in text
        doc = json.dumps({"segments": [
            {"start": 0, "end": 1, "text": "hi",
             "words": [{"word": " hi", "start": 0, "end": 1}]}]})
        assert "word timings" in ti.describe(ti.load_transcript(doc, "x.json"))
