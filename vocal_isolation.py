"""Separate the voice from laughter, crowd and music before transcribing.

Comedy shows are the case this exists for. Measured on three 60 s slices of a
Comedy Nights episode (large-v3, `temperature=0` on both arms so only the
audio differs), transcribing the isolated vocals against the raw mix:

    arm      segments   covered   characters
    raw            33      165s         1348
    vocals         53      142s         1877

Raw "covers" MORE seconds and says 39% LESS, which is the whole finding: its
coverage is inflated by long segments that span noise without words. Density
separates them — 8.2 chars/s raw against 13.2 chars/s on the vocals, and real
Hindi speech runs about 12-15. One slice showed it plainly: raw returned the
whole 60 s as two run-on segments and lost the entire 18-50 s exchange
including its punchline, while the vocals stem returned all eighteen lines
with per-line timing.

Off by default (`TRANSCRIBE_VOCALS=1`). Separation is cheap once the weights
are cached — 35x realtime on an RTX 2070 SUPER, so ~2 min for a 72-minute
source — but it is a second model on a shared 8 GB GPU, so it runs inside the
same ASR gate as whisper rather than alongside it.

**Why the model API and not `demucs.api` or the CLI.** Both of those import
`demucs.audio`, which imports `lameenc` — an LGPL LAME binding needed only for
MP3 output. `demucs.pretrained` + `demucs.apply` need neither, so this module
does its own WAV I/O and the dependency stays MIT/Apache. Install with:

    pip install demucs && pip uninstall -y lameenc

demucs is an OPTIONAL import: without it this returns None and the caller
transcribes the raw audio, exactly as before.
"""

import os
import subprocess
import tempfile
import time
import wave

SAMPLE_RATE = 16000

# htdemucs is the v4 default: 4 stems, and we keep one. "htdemucs_ft" is
# better and four times slower for the same stem, which is not worth it when
# the input is a talk show rather than a music master.
# demucs' output tensor is full-length and all-stems, so the window — not
# the video — is what bounds memory. 240 s costs ~340 MB of host RAM, and
# nothing full-length at the model's rate is built any more: each window is
# resampled on its own (see _separate_windowed).
WINDOW_SECONDS = 240.0
# Cross-faded, so a window boundary cannot clip a word in half.
OVERLAP_SECONDS = 3.0

DEFAULT_MODEL = "htdemucs"


def enabled():
    return (os.environ.get("TRANSCRIBE_VOCALS") or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_wav_mono(path):
    import numpy as np
    with wave.open(path, "rb") as f:
        if f.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM, got {f.getsampwidth() * 8}-bit")
        sr, channels = f.getframerate(), f.getnchannels()
        raw = f.readframes(f.getnframes())
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(1)
    return audio, sr


def _write_wav_mono(path, audio, sample_rate):
    import numpy as np
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes())


def _extract_audio(media_path, out_path):
    """16 kHz mono PCM, which is what whisper wants anyway."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", media_path,
         "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", out_path],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def isolate_vocals(media_path, cache_path=None, gate=None):
    """Write the vocals-only track of ``media_path`` and return its path.

    Returns None — never raises — when the option is off, demucs is not
    installed, or anything fails. The caller then transcribes the original
    audio, so a broken separation costs a little time and nothing else.

    ``cache_path`` is reused when it already exists, so a resumed job does not
    pay for the separation twice. ``gate`` is a context manager (the ASR
    semaphore) held for the GPU work only.
    """
    if cache_path and os.path.exists(cache_path) and os.path.getsize(cache_path) > 1024:
        print(f"🎚️  [vocals] reusing {os.path.basename(cache_path)}")
        return cache_path
    try:
        import numpy as np
        import torch
        import torchaudio
        from demucs.apply import apply_model
        from demucs.pretrained import get_model
    except ImportError as e:
        print(f"🎚️  [vocals] TRANSCRIBE_VOCALS is on but demucs is unavailable ({e}); "
              f"transcribing the raw audio. Install: pip install demucs && pip uninstall -y lameenc")
        return None

    tmp_wav = None
    try:
        started = time.time()
        source = media_path
        if not str(media_path).lower().endswith(".wav"):
            fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            _extract_audio(media_path, tmp_wav)
            source = tmp_wav

        audio, sr = _read_wav_mono(source)
        duration = len(audio) / float(sr or SAMPLE_RATE)
        if duration <= 0:
            return None

        model_name = os.environ.get("VOCALS_MODEL", DEFAULT_MODEL)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = get_model(model_name).eval().to(device)

        # demucs normalises against the mixture's own statistics. Doing that
        # HERE, at the source rate and in place, is what keeps a long input
        # cheap: the old code first resampled the whole track to the model's
        # 44.1 kHz and repeated it to two channels, which is 1.55 GB on a
        # 73-minute source (measured; 2.35 GB peak for the step) allocated
        # before a single window was separated — and MAX_CONCURRENT_JOBS
        # lets several jobs do it at once, which is how the machine ran out
        # of memory. Normalisation is affine and resampling is linear, so
        # the order does not matter; the same two constants de-normalise
        # each window, so the round trip is exact whichever rate they were
        # computed at.
        ref_mean = float(audio.mean())
        ref_std = float(audio.std())
        audio -= ref_mean
        audio /= (ref_std + 1e-8)

        ctx = gate if gate is not None else _NullGate()
        with ctx:
            with torch.inference_mode():
                vocals = _separate_windowed(
                    model, audio, sr or SAMPLE_RATE, device, ref_std, ref_mean,
                    torch, torchaudio, np)

        out_path = cache_path or (tempfile.mkstemp(suffix=".vocals.wav")[1])
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        _write_wav_mono(out_path, np.asarray(vocals), SAMPLE_RATE)
        took = time.time() - started
        print(f"🎚️  [vocals] {model_name} on {device}: {duration:.0f}s of audio in "
              f"{took:.0f}s ({duration / max(took, 0.01):.0f}x realtime)")
        return out_path
    except Exception as e:
        print(f"🎚️  [vocals] separation failed ({type(e).__name__}: {e}); "
              f"transcribing the raw audio")
        return None
    finally:
        if tmp_wav and os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except OSError:
                pass


def _separate_windowed(model, audio, sr, device, ref_std, ref_mean, torch, torchaudio, np):
    """The vocals stem at SAMPLE_RATE, mono, computed a window at a time.

    demucs allocates its output for the WHOLE input and for EVERY stem on the
    mix's own device (`apply.py`: ``out = th.zeros(batch, len(model.sources),
    channels, length, device=mix.device)``), and `split=True` chunks only the
    compute, not that tensor. A 72-minute source therefore asks for 29 GiB on
    an 8 GB card — which is exactly what happened on the first real video,
    while the 60 s slices this was measured on needed about 7 MB and could
    never have shown it. Moving the mix to CPU only moves the problem: the
    same tensor is then ~6 GB of RAM.

    So the window is ours. Each one allocates its own output, only the vocals
    stem is kept, and it is downmixed and resampled to 16 kHz before the next
    window runs, which bounds both GPU and host memory by WINDOW_SECONDS
    rather than by the length of the video. Windows overlap and are
    cross-faded, because a hard cut at a boundary can clip a word in half and
    the whole point of this is to not lose speech.

    ``audio`` arrives as normalised mono at the SOURCE rate, and each window
    is resampled to the model's rate on its own. Resampling the whole track
    up front was the last full-length allocation left here — 1.55 GB for the
    tensor on a 73-minute input — and it existed only to be sliced. The cost
    is that each window's resample has its own filter transients at the
    edges; they land inside the 3 s overlap that is already cross-faded, so
    nothing reaches the output that was not already being faded.
    """
    from demucs.apply import apply_model   # the caller already proved it imports

    sr_m = model.samplerate
    total = len(audio)
    win = int(WINDOW_SECONDS * sr)
    ov = int(OVERLAP_SECONDS * sr)
    step = max(1, win - ov)
    vocals_idx = model.sources.index("vocals")

    # Built once rather than per window: torchaudio recomputes the sinc
    # kernel on every functional.resample call, and there is one window
    # every four minutes of input.
    up = torchaudio.transforms.Resample(sr, sr_m)
    down = torchaudio.transforms.Resample(sr_m, SAMPLE_RATE)

    out_len = int(total / sr * SAMPLE_RATE) + SAMPLE_RATE
    acc = np.zeros(out_len, dtype=np.float32)
    wsum = np.zeros(out_len, dtype=np.float32)

    starts = list(range(0, total, step)) or [0]
    for n, start in enumerate(starts):
        chunk = audio[start:start + win]
        if len(chunk) < int(0.2 * sr):
            break
        # This window only, at the model's rate and in the two channels it
        # wants. Mix stays on CPU: apply_model moves each sub-chunk to the
        # GPU itself, so even this window never lands in VRAM whole.
        stereo = up(torch.from_numpy(chunk)[None, :]).repeat(2, 1)
        stems = apply_model(model, stereo[None], device=device,
                            split=True, overlap=0.25, progress=False)[0]
        del stereo
        v = stems[vocals_idx].mean(0, keepdim=True) * ref_std + ref_mean
        del stems
        v16 = down(v.cpu())[0].numpy().astype(np.float32)

        ramp = np.ones(len(v16), dtype=np.float32)
        ov16 = int(OVERLAP_SECONDS * SAMPLE_RATE)
        if n > 0 and ov16 and len(ramp) > ov16:
            ramp[:ov16] = np.linspace(0.0, 1.0, ov16, dtype=np.float32)
        if start + win < total and ov16 and len(ramp) > ov16:
            ramp[-ov16:] = np.linspace(1.0, 0.0, ov16, dtype=np.float32)

        pos = int(start / sr * SAMPLE_RATE)
        end = min(pos + len(v16), out_len)
        take = end - pos
        if take <= 0:
            break
        acc[pos:end] += v16[:take] * ramp[:take]
        wsum[pos:end] += ramp[:take]

    keep = wsum > 1e-6
    acc[keep] /= wsum[keep]
    last = int(np.flatnonzero(keep)[-1]) + 1 if keep.any() else 0
    return acc[:last]


class _NullGate:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
