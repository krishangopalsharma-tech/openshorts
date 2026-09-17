"""Generated voiceover for Movie Recap through a Kokoro-FastAPI server.

Kokoro-82M (Apache 2.0) reads the Pass C narration line by line and the
result is fitted to the cut without whisper. Two rules, both measured on the
render box on 17-sep-2026 (docs/film-modules-plan.md §3.8):

- **CPU only, separate process.** A 150 s part synthesises in 16 s on the
  Ryzen 7 with zero VRAM; on the GPU it would hold 1.4 GB reserved and load
  for 15 s to save 15 seconds, on a card that idles at 3.9 of 8 GB before a
  job puts TransNetV2, YOLO, NVENC and whisper on it. The server also keeps
  espeak-ng (GPL) out of this process: nothing here imports kokoro or misaki.
- **Synthesis never overlaps the render.** One synthesis at a time
  (``_GATE``), finished before the cut phase starts its parallel encodes.

Offline is a state, not an error: ``health()`` reports it with the command
that starts the server, the tab disables the button, and "upload voiceover"
stays available.
"""

import os
import threading
import time
import wave
from contextlib import contextmanager

import httpx

KOKORO_URL = os.environ.get("KOKORO_URL", "http://127.0.0.1:8880").rstrip("/")
KOKORO_TIMEOUT = float(os.environ.get("KOKORO_TIMEOUT", "120"))
KOKORO_HOME = os.environ.get("KOKORO_HOME", r"F:\kokoro\Kokoro-FastAPI")
START_COMMAND = f"cd {KOKORO_HOME}; .\\start-cpu.ps1"

DEFAULT_VOICE = "am_michael"
# Measured on Machine Gun Preacher part 1 (411 words, 18 lines): at 1.0 the
# part ran 173 s against the 150 s target with every line fitting through
# head/tail room; 1.1 lands it near the target without any line needing the
# speed step.
DEFAULT_SPEED = 1.1
SPEED_RANGE = (0.7, 1.4)
MAX_FIT_SPEED = 1.25      # never read faster than this to make a line fit
LEAD_SECONDS = 0.15       # picture before the voice starts (compilation.py's padding)
TAIL_SECONDS = 0.25       # picture after the voice ends
MIN_AVAILABLE = 0.5

LANGUAGES = {
    "a": ("en-us", "English (US)"), "b": ("en-gb", "English (UK)"), "h": ("hi", "Hindi"),
    "e": ("es", "Spanish"), "f": ("fr", "French"), "i": ("it", "Italian"),
    "p": ("pt-br", "Portuguese (BR)"), "j": ("ja", "Japanese"), "z": ("zh", "Chinese"),
}

_GATE = threading.Semaphore(1)
_voices_cache = {"at": 0.0, "voices": None}
_VOICES_TTL = 60.0


class VoiceOffline(RuntimeError):
    """The Kokoro server did not answer."""


@contextmanager
def _session(client=None):
    """An injected client (tests, a shared pool) is borrowed and left open;
    a client made here is closed on the way out."""
    if client is not None:
        yield client
        return
    c = httpx.Client(base_url=KOKORO_URL, timeout=KOKORO_TIMEOUT)
    try:
        yield c
    finally:
        c.close()


def describe_voice(voice_id):
    """``am_michael`` -> language, gender and a display name from the prefix."""
    vid = str(voice_id)
    lang = LANGUAGES.get(vid[:1], ("", "Other"))
    gender = {"f": "female", "m": "male"}.get(vid[1:2], "")
    name = vid.split("_", 1)[1] if "_" in vid else vid
    return {"id": vid, "name": name.replace("v0", "v0 ").title(), "language": lang[1],
            "lang_code": lang[0], "gender": gender, "legacy": "_v0" in vid}


def voices(client=None, force=False):
    """The server's voice list, described and cached for a minute. Raises
    VoiceOffline when the server cannot be reached."""
    now = time.time()
    if not force and _voices_cache["voices"] is not None and now - _voices_cache["at"] < _VOICES_TTL:
        return _voices_cache["voices"]
    try:
        with _session(client) as c:
            r = c.get("/v1/audio/voices")
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise VoiceOffline(str(exc)[:200]) from exc
    ids = [v["id"] if isinstance(v, dict) else str(v) for v in data.get("voices", [])]
    out = [describe_voice(v) for v in ids]
    out.sort(key=lambda v: (v["legacy"], v["language"], v["gender"], v["name"]))
    _voices_cache.update(at=now, voices=out)
    return out


def health(client=None):
    """Never raises: the dashboard renders this as a state."""
    try:
        vs = voices(client=client, force=True)
        return {"online": True, "url": KOKORO_URL, "voices": len(vs), "device": "cpu",
                "start_command": START_COMMAND}
    except VoiceOffline as exc:
        return {"online": False, "url": KOKORO_URL, "voices": 0, "error": str(exc),
                "start_command": START_COMMAND}


def wav_duration(path):
    """Seconds of audio in a wav, from the BYTES ON DISK, not the header.
    Kokoro-FastAPI writes a streaming header whose data length is a
    placeholder (0xFFFFFFFF), which `wave` reports as ~89,000 s per line."""
    with wave.open(path, "rb") as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        header_frames = w.getnframes()
    bytes_per_frame = max(1, width * channels)
    data_bytes = max(0, os.path.getsize(path) - 44)
    frames = data_bytes // bytes_per_frame
    if 0 < header_frames < frames:
        frames = header_frames
    return frames / float(rate) if rate else 0.0


def synthesize(text, voice, speed, out_wav, client=None):
    """One line -> a 24 kHz mono wav. Serialised through ``_GATE``. Returns
    ``{"path", "duration", "speed"}``; raises VoiceOffline / ValueError."""
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError("nothing to say")
    speed = float(min(SPEED_RANGE[1], max(SPEED_RANGE[0], float(speed))))
    body = {"model": "kokoro", "input": text, "voice": voice, "speed": speed,
            "response_format": "wav", "stream": False}
    with _GATE:
        try:
            with _session(client) as c:
                r = c.post("/v1/audio/speech", json=body)
        except httpx.HTTPError as exc:
            raise VoiceOffline(str(exc)[:200]) from exc
    if r.status_code >= 400:
        detail = r.text[:300]
        if r.status_code in (400, 422):
            raise ValueError(f"Kokoro refused the line: {detail}")
        raise VoiceOffline(f"Kokoro answered {r.status_code}: {detail}")
    os.makedirs(os.path.dirname(os.path.abspath(out_wav)), exist_ok=True)
    with open(out_wav, "wb") as fh:
        fh.write(r.content)
    return {"path": out_wav, "duration": round(wav_duration(out_wav), 3), "speed": speed}


def narrate_plan(plan, part, protected, duration, voice, speed, workdir, *, client=None,
                 speed_video=1.25, lead=LEAD_SECONDS, tail=TAIL_SECONDS, max_speed=MAX_FIT_SPEED,
                 log=None):
    """Every chunk's narration as a wav, placed on the cut. Returns
    ``{"segments", "report", "total_finished", "voice", "speed", "overruns"}``;
    ``segments`` is the ``movierecap.fitted_to_source`` shape that
    ``film_render.render_narrated`` consumes.

    Per chunk, in order and deterministic: the slot is the chunk's footage at
    playback speed minus lead and tail; a line that fits is placed; a longer
    one first grows the chunk with its head/tail room (bounded by the
    neighbours and by protected ranges, from movierecap.compilation_plan),
    then is re-read faster up to ``max_speed``, and past that is an
    ``overrun`` the report names. The picture is never time-stretched.
    """
    import movierecap as mr
    plan = plan if isinstance(plan, mr.PartPlan) else mr.PartPlan.model_validate(plan)
    cplan = mr.compilation_plan(plan, protected, duration, speed=speed_video)
    log = log or (lambda *_: None)
    os.makedirs(workdir, exist_ok=True)
    segments, report = [], []
    timeline = 0.0
    overruns = 0
    for i, (chunk, shot) in enumerate(zip(plan.chunks, cplan["shots"])):
        src_in, src_out = shot["src_in"], shot["src_out"]     # finished-equivalent units
        text = " ".join(chunk.narration.split())
        entry = {"chunk": i + 1, "slot": round(src_out - src_in, 2), "words": len(text.split()),
                 "voice_seconds": 0.0, "action": "silent", "room_used": 0.0, "speed": speed, "overrun": 0.0}
        if not text:
            length = src_out - src_in
            segments.append({"start": round(src_in * speed_video, 3), "end": round(src_out * speed_video, 3),
                             "shot_start": round(timeline, 3), "shot_length": round(length, 3),
                             "voice_start": None, "silent": True, "keep_audio": chunk.keep_audio,
                             "flip": chunk.flip, "chunk": None})
            timeline += length
            report.append(entry)
            continue

        wav = os.path.join(workdir, f"line_{i + 1:02d}.wav")
        log(f"🗣️  line {i + 1}/{len(plan.chunks)}: {entry['words']} words")
        got = synthesize(text, voice, speed, wav, client=client)
        vo = got["duration"]
        used_speed = got["speed"]
        available = max(MIN_AVAILABLE, (src_out - src_in) - lead - tail)
        action = "fit"
        room_used = 0.0
        if vo > available:
            need = vo - available
            take_tail = min(need, shot["tail_room"])
            src_out += take_tail
            need -= take_tail
            take_head = min(need, shot["head_room"])
            src_in -= take_head
            need -= take_head
            room_used = take_tail + take_head
            available = (src_out - src_in) - lead - tail
            action = "room"
            if vo > available + 1e-6:
                faster = min(max_speed, used_speed * vo / available)
                if faster > used_speed + 0.01:
                    got = synthesize(text, voice, faster, wav, client=client)
                    vo, used_speed = got["duration"], got["speed"]
                    action = "speed"
                if vo > available + 1e-6:
                    action = "overrun"
                    entry["overrun"] = round(vo - available, 2)
                    overruns += 1
        length = max(src_out - src_in, lead + vo + tail) if action != "overrun" else (src_out - src_in)
        entry.update(voice_seconds=round(vo, 2), action=action, room_used=round(room_used, 2),
                     speed=round(used_speed, 3), slot=round(src_out - src_in, 2))
        segments.append({"start": round(src_in * speed_video, 3), "end": round(src_out * speed_video, 3),
                         "shot_start": round(timeline, 3), "shot_length": round(length, 3),
                         "voice_start": round(timeline + lead, 3), "silent": False,
                         "keep_audio": chunk.keep_audio, "flip": chunk.flip,
                         "chunk": {"path": wav, "duration": vo}})
        timeline += length
        report.append(entry)
    return {"segments": segments, "report": report, "total_finished": round(timeline, 2),
            "voice": voice, "speed": speed, "overruns": overruns,
            "words": sum(r["words"] for r in report)}
