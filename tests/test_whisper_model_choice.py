# -*- coding: utf-8 -*-
"""Which whisper model a job gets, and when a prompt is safe to send.

large-v3-turbo mishears Hindustani (measured on real Hindi audio: कापिल for
कपिल, सार for सर, नेलपेंट लगाय for नेल पेंट लगा), and prompting turbo makes it
abandon Hindi for English translation outright. So the model is chosen per
language rather than globally, and initial_prompt is withheld from turbo.
"""

import subtitles


class TestModelChoice:
    def test_turbo_is_replaced_on_hindustani(self, monkeypatch):
        monkeypatch.setenv("WHISPER_MODEL", "large-v3-turbo")
        monkeypatch.delenv("WHISPER_MODEL_HINDUSTANI", raising=False)
        for language in ("hi", "ur", "hinglish"):
            assert subtitles.get_whisper_config(language)["model_size"] == "large-v3"

    def test_other_languages_keep_the_configured_model(self, monkeypatch):
        monkeypatch.setenv("WHISPER_MODEL", "large-v3-turbo")
        for language in ("en", "es", "de", None):
            assert (subtitles.get_whisper_config(language)["model_size"]
                    == "large-v3-turbo")

    def test_a_non_turbo_choice_is_never_overridden(self, monkeypatch):
        """An operator who named a model gets that model, Hindi or not."""
        monkeypatch.setenv("WHISPER_MODEL", "medium")
        assert subtitles.get_whisper_config("hi")["model_size"] == "medium"

    def test_the_replacement_is_configurable(self, monkeypatch):
        monkeypatch.setenv("WHISPER_MODEL", "large-v3-turbo")
        monkeypatch.setenv("WHISPER_MODEL_HINDUSTANI", "vasista22/whisper-hindi-large-v2")
        assert (subtitles.get_whisper_config("hi")["model_size"]
                == "vasista22/whisper-hindi-large-v2")

    def test_device_and_compute_still_come_from_the_env(self, monkeypatch):
        monkeypatch.setenv("WHISPER_MODEL", "large-v3-turbo")
        monkeypatch.setenv("WHISPER_DEVICE", "cuda")
        monkeypatch.setenv("WHISPER_COMPUTE", "float16")
        cfg = subtitles.get_whisper_config("hi")
        assert (cfg["device"], cfg["compute_type"]) == ("cuda", "float16")


class TestPromptSupport:
    def test_turbo_does_not_take_a_prompt(self):
        assert not subtitles.whisper_supports_prompt("large-v3-turbo")
        assert not subtitles.whisper_supports_prompt("LARGE-V3-TURBO")

    def test_every_other_model_does(self):
        for model in ("large-v3", "medium", "small", ""):
            assert subtitles.whisper_supports_prompt(model)

    def test_prompt_is_read_from_the_env(self, monkeypatch):
        monkeypatch.setenv("TRANSCRIBE_PROMPT", "  कपिल शर्मा, अजय देवगन  ")
        assert subtitles.transcribe_prompt() == "कपिल शर्मा, अजय देवगन"

    def test_no_prompt_is_none_not_empty_string(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIBE_PROMPT", raising=False)
        assert subtitles.transcribe_prompt() is None
        monkeypatch.setenv("TRANSCRIBE_PROMPT", "   ")
        assert subtitles.transcribe_prompt() is None


class TestHotwordsStaysOff:
    """`initial_prompt` only reaches whisper's first 30s window here, because
    condition_on_previous_text=False makes faster-whisper reset the prompt after
    every window. `hotwords` persists and is the obvious fix — and measured on a
    110s Hindi slice it dropped ~52s of 110s of speech and drifted toward
    English. Pinned so it is not re-added on the strength of the mechanism
    alone; see CLAUDE.md for the numbers."""

    def test_the_shared_transcribe_params_do_not_carry_hotwords(self):
        assert "hotwords" not in subtitles.WHISPER_TRANSCRIBE_PARAMS

    def test_the_prompt_is_never_sent_as_hotwords(self, monkeypatch):
        import transcribe_backends
        monkeypatch.setenv("TRANSCRIBE_PROMPT", "Kapil Sharma, Ajay Devgn")
        monkeypatch.setenv("WHISPER_MODEL", "large-v3")
        captured = {}

        def fake_run(media_path, **params):
            captured.update(params)
            return [], type("I", (), {"language": "hi"})()

        monkeypatch.setattr(transcribe_backends, "run_whisper_transcription", fake_run)
        # The language comes from TRANSCRIBE_LANGUAGE, not an argument.
        monkeypatch.setenv("TRANSCRIBE_LANGUAGE", "hi")
        transcribe_backends._transcribe_with_whisper("x.wav")

        assert captured.get("initial_prompt") == "Kapil Sharma, Ajay Devgn"
        assert "hotwords" not in captured
