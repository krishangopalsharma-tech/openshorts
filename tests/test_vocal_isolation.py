"""Vocal isolation: the option, the fail-open contract and the cache.

The separation itself is a 100 MB model on the GPU, so the tests here cover
everything AROUND it — that it is off unless asked for, that every failure
path leaves the caller transcribing the original audio, and that the per-job
flag survives a redeploy.
"""
import os

import pytest

import transcribe_backends as tb
import vocal_isolation as vi


class TestTheOption:
    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("", False), ("no", False),
    ])
    def test_the_env_var_reads_the_usual_truthy_spellings(self, monkeypatch, value, expected):
        monkeypatch.setenv("TRANSCRIBE_VOCALS", value)
        assert vi.enabled() is expected

    def test_it_is_off_when_nothing_is_set(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIBE_VOCALS", raising=False)
        assert vi.enabled() is False

    def test_whisper_gets_the_original_audio_when_the_option_is_off(self, monkeypatch):
        monkeypatch.delenv("TRANSCRIBE_VOCALS", raising=False)
        monkeypatch.setenv("WHISPER_MODEL", "large-v3")
        seen = {}

        def fake_run(path, **params):
            seen["path"] = path
            return [], type("I", (), {"language": "hi", "duration": 1.0})()

        monkeypatch.setattr(tb, "run_whisper_transcription", fake_run)
        monkeypatch.setattr(vi, "isolate_vocals",
                            lambda *a, **k: pytest.fail("must not separate when off"))
        tb._transcribe_with_whisper("video.mp4")
        assert seen["path"] == "video.mp4"


class TestFailOpen:
    """A separation that cannot run must cost time and nothing else."""

    def test_a_missing_demucs_leaves_the_audio_alone(self, monkeypatch, capsys):
        monkeypatch.setitem(__import__("sys").modules, "demucs.pretrained", None)
        monkeypatch.setattr(vi, "_read_wav_mono",
                            lambda p: pytest.fail("should not get that far"))
        # Force the import inside isolate_vocals to fail.
        import builtins
        real_import = builtins.__import__

        def blocked(name, *a, **k):
            if name.startswith("demucs"):
                raise ImportError("No module named 'demucs'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", blocked)
        assert vi.isolate_vocals("video.mp4") is None
        assert "demucs is unavailable" in capsys.readouterr().out

    def test_whisper_still_runs_on_the_original_when_separation_returns_none(self, monkeypatch):
        monkeypatch.setenv("TRANSCRIBE_VOCALS", "1")
        monkeypatch.setenv("WHISPER_MODEL", "large-v3")
        monkeypatch.setattr(vi, "isolate_vocals", lambda *a, **k: None)
        seen = {}

        def fake_run(path, **params):
            seen["path"] = path
            return [], type("I", (), {"language": "hi", "duration": 1.0})()

        monkeypatch.setattr(tb, "run_whisper_transcription", fake_run)
        tb._transcribe_with_whisper("video.mp4")
        assert seen["path"] == "video.mp4"

    def test_the_stem_is_used_when_separation_succeeds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TRANSCRIBE_VOCALS", "1")
        monkeypatch.setenv("WHISPER_MODEL", "large-v3")
        stem = tmp_path / "v.wav"
        stem.write_bytes(b"\x00" * 4096)
        monkeypatch.setattr(vi, "isolate_vocals", lambda *a, **k: str(stem))
        seen = {}

        def fake_run(path, **params):
            seen["path"] = path
            return [], type("I", (), {"language": "hi", "duration": 1.0})()

        monkeypatch.setattr(tb, "run_whisper_transcription", fake_run)
        tb._transcribe_with_whisper("video.mp4")
        assert seen["path"] == str(stem)


class TestCache:
    def test_an_existing_stem_is_reused_without_loading_the_model(self, tmp_path, capsys):
        cached = tmp_path / "source.mp4.vocals.wav"
        cached.write_bytes(b"\x00" * 4096)
        out = vi.isolate_vocals(str(tmp_path / "source.mp4"), cache_path=str(cached))
        assert out == str(cached)
        assert "reusing" in capsys.readouterr().out

    def test_a_truncated_stem_is_not_trusted(self, tmp_path, monkeypatch):
        """A separation killed mid-write leaves a stub, and transcribing that
        would be silently worse than transcribing the original."""
        cached = tmp_path / "source.mp4.vocals.wav"
        cached.write_bytes(b"\x00" * 16)
        monkeypatch.setattr(vi, "_extract_audio",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("no ffmpeg")))
        assert vi.isolate_vocals(str(tmp_path / "source.mp4"), cache_path=str(cached)) is None


def test_the_choice_survives_a_redeploy():
    """A resumed job rebuilds its env from os.environ, so anything not in the
    allowlist is silently replaced by the deployment default."""
    import app
    assert "TRANSCRIBE_VOCALS" in app._RESUMABLE_ENV_KEYS
