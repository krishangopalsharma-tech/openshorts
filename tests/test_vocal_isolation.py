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


class TestLongInputsAreWindowed:
    """The bug this class exists for: demucs allocates its output for the WHOLE
    input and for EVERY stem on the mix's own device, and `split=True` chunks
    only the compute. A 72-minute video therefore asked for 29 GiB on an 8 GB
    card and fell back to the raw audio on the first real job — while the 60 s
    slices the feature was measured on needed ~7 MB and could never show it.
    These tests are about SHAPE and DEVICE, so they need no GPU and no demucs.
    """

    def _fake_model(self, torch):
        class M:
            samplerate = 44100
            sources = ["drums", "bass", "other", "vocals"]
        return M()

    def _run(self, monkeypatch, seconds):
        import numpy as np
        import torch
        import torchaudio
        calls = []

        def fake_apply_model(model, mix, device=None, **kw):
            # What the real one does wrong if handed a cuda mix: allocate
            # (stems x channels x full length) here.
            calls.append({"samples": mix.shape[-1], "device": str(mix.device),
                          "split": kw.get("split")})
            return torch.zeros(1, len(model.sources), mix.shape[-2], mix.shape[-1])

        import sys
        import types
        mod = types.ModuleType("demucs.apply")
        mod.apply_model = fake_apply_model
        monkeypatch.setitem(sys.modules, "demucs.apply", mod)

        model = self._fake_model(torch)
        wav = torch.zeros(2, int(seconds * model.samplerate))
        out = vi._separate_windowed(model, wav, "cuda", 1.0, 0.0,
                                    torch, torchaudio, np)
        return out, calls

    def test_a_long_input_is_split_into_several_windows(self, monkeypatch):
        out, calls = self._run(monkeypatch, vi.WINDOW_SECONDS * 2.5)
        assert len(calls) >= 3, f"expected windowing, got {len(calls)} call(s)"

    def test_no_window_is_longer_than_the_window_size(self, monkeypatch):
        _, calls = self._run(monkeypatch, vi.WINDOW_SECONDS * 2.5)
        cap = int(vi.WINDOW_SECONDS * 44100)
        assert all(c["samples"] <= cap for c in calls), [c["samples"] for c in calls]

    def test_the_mix_never_goes_to_the_gpu(self, monkeypatch):
        """This is the fix. The compute device is still cuda; it is the MIX
        that has to stay on CPU, because demucs sizes its output tensor from
        `mix.device`."""
        _, calls = self._run(monkeypatch, vi.WINDOW_SECONDS * 2.5)
        assert all(c["device"] == "cpu" for c in calls), [c["device"] for c in calls]

    def test_a_short_input_is_one_window_and_still_works(self, monkeypatch):
        out, calls = self._run(monkeypatch, 30.0)
        assert len(calls) == 1
        assert len(out) > 0

    def test_the_stem_keeps_the_length_of_the_input(self, monkeypatch):
        seconds = vi.WINDOW_SECONDS * 2.5
        out, _ = self._run(monkeypatch, seconds)
        expected = seconds * vi.SAMPLE_RATE
        # Cross-fades and resampling move the tail by a few ms, not seconds.
        assert abs(len(out) - expected) < vi.SAMPLE_RATE, (len(out), expected)
