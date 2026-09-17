"""Render assembly for Movie Recap: a chunk plan -> one finished part.

Order, and why each step sits where it does (docs/film-modules-plan.md §1.3):

1. cut every chunk from the source (re-encode, uniform params, ``-ss`` before
   ``-i`` so an 18-chunk plan does not decode a two-hour file 18 times from
   the head; every part scaled to one frame size so the concat is clean)
2. concat with ``-c copy``
3. speed 1.25x — BEFORE the reframe, because reframe_v2 emits sendcmd crop
   timelines and a PTS change afterwards lands every command on the wrong
   frame
4. reframe to the output format (main.render_clip, injectable)
5. audio: the silent preview drops the soundtrack (``mute``); the narrated
   part lays the voiceover over a muted bed, kept only on ``keep_audio``
   chunks, and normalises once
6. fps

Every external step is a hook with a default, so tests drive the whole
assembly with fakes and never touch ffmpeg or the GPU.

The Movie Shorts render (per-shot crop ladder, beat-locked cutting, music
bed) lived here until 17-sep-2026 and was removed after the first real render:
the crop changes read as vibration after the reframe. Movie shorts will go
through the clip maker instead.
"""

import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor

import film_prep as fp
from ffmpeg_utils import METADATA_SCRUB, QUALITY_FAST, video_encode_args

FILM_LOUDNORM = "loudnorm=I=-14:TP=-2.0:LRA=7"
DEFAULT_FPS = 60
OUTPUT_FORMATS = ("vertical", "square")
# Turing consumer cards allow 2-3 concurrent NVENC sessions.
NVENC_PARALLEL = int(os.environ.get("NVENC_PARALLEL", "2"))
CPU_PARALLEL = int(os.environ.get("CPU_PARALLEL", "4"))
FFMPEG_TIMEOUT = 1800


class RenderError(RuntimeError):
    pass


def _safe_print(message):
    """print() that survives a cp1252 console (Windows without
    PYTHONIOENCODING): the emoji prefixes are dropped, the words stay."""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "ignore").decode("ascii").strip(), flush=True)


def _run(cmd):
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RenderError(f"ffmpeg timed out ({cmd[1:6]}...)")
    if res.returncode != 0:
        tail = (res.stderr or b"").decode("utf-8", "replace")[-400:]
        raise RenderError(f"ffmpeg failed ({cmd[1:6]}...): {tail}")


def shot_filter(shot, out_size=None):
    """``-vf`` for one chunk: an optional reasoned ``hflip`` (movierecap
    validates the reason and refuses patterns; never set automatically) and a
    scale to the common ``out_size`` so every part matches for the concat."""
    parts = []
    if shot.get("hflip"):
        parts.append("hflip")
    if out_size:
        w, h = int(out_size[0]) - int(out_size[0]) % 2, int(out_size[1]) - int(out_size[1]) % 2
        parts.append(f"scale={w}:{h}:flags=lanczos")
    return ",".join(parts) if parts else None


def shot_cut_command(input_path, shot, out_path, *, out_size=None):
    """ffmpeg argv cutting one chunk with uniform encode parameters."""
    vf = shot_filter(shot, out_size=out_size)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{float(shot['start']):.3f}", "-to", f"{float(shot['end']):.3f}",
           "-i", input_path]
    if vf:
        cmd += ["-vf", vf]
    cmd += [*video_encode_args(QUALITY_FAST), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            *METADATA_SCRUB, "-movflags", "+faststart", out_path]
    return cmd


def cut_shots(input_path, shots, workdir, *, runner=None, nvenc_parallel=NVENC_PARALLEL,
              cpu_parallel=CPU_PARALLEL, out_size=None):
    """Encode every chunk, a few in parallel, ALL with the same encoder.

    Splitting the lanes between NVENC and libx264 was tried first and produced
    parts whose streams differ in SPS/profile; the ``-c copy`` concat joins them
    without complaint and the next ffmpeg pass fails on the result with an
    empty stderr. Uniform parameters are what make the concat demuxer safe.
    """
    run = runner or _run
    token = uuid.uuid4().hex[:8]
    parts = [os.path.join(workdir, f"temp_film_{token}_{i:03d}.mp4") for i in range(len(shots))]
    use_gpu = os.environ.get("FFMPEG_ENCODER", "x264").lower() in ("nvenc", "auto")
    if use_gpu:
        from ffmpeg_utils import nvenc_available
        use_gpu = nvenc_available()
    lanes = max(1, nvenc_parallel if use_gpu else cpu_parallel)

    def job(i):
        run(shot_cut_command(input_path, shots[i], parts[i], out_size=out_size))

    with ThreadPoolExecutor(max_workers=lanes) as pool:
        list(pool.map(job, range(len(shots))))
    return parts


def concat_parts(parts, out_path, workdir, runner=None):
    from recut import concat_command
    run = runner or _run
    list_path = os.path.join(workdir, f"temp_film_concat_{uuid.uuid4().hex[:8]}.txt")
    with open(list_path, "w") as fh:
        for p in parts:
            fh.write(f"file '{os.path.abspath(p)}'\n")
    try:
        run(concat_command(list_path, out_path))
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass
    return out_path


def finalize_command(input_path, out_path, fps=DEFAULT_FPS, loudnorm=FILM_LOUDNORM, mute=False):
    """Last pass: frame rate and the one loudness normalisation. ``mute``
    drops the audio stream: the recap's silent preview must not carry the
    film's soundtrack (it did, in the first test)."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", input_path]
    if fps:
        cmd += ["-vf", f"fps={int(fps)}", *video_encode_args(QUALITY_FAST)]
    else:
        cmd += ["-c:v", "copy"]
    if mute:
        cmd += ["-an"]
    else:
        cmd += ["-af", loudnorm, "-c:a", "aac", "-b:a", "160k"]
    cmd += [*METADATA_SCRUB, "-movflags", "+faststart", out_path]
    return cmd


def _default_reframe(work_path, out_path, output_format):
    from main import render_clip
    return render_clip(work_path, out_path, output_format)


def _probe_size(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            stderr=subprocess.STDOUT, timeout=60).decode().strip().split("x")
        return int(out[0]), int(out[1])
    except Exception:
        return (1920, 1080)


def _cleanup(paths):
    for p in paths:
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def render_silent(video_path, shots, out_path, workdir, *, speed=fp.DEFAULT_SPEED,
                  output_format="vertical", fps=DEFAULT_FPS, runner=None, reframe=None,
                  probe_size=None, keep_parts=False, log=None, mute=True):
    """The silent preview of a part: chunks cut, joined, sped, reframed, no
    soundtrack. Returns a dict describing what was produced."""
    if output_format not in OUTPUT_FORMATS:
        raise RenderError(f"output_format must be one of {OUTPUT_FORMATS}")
    if not shots:
        raise RenderError("no chunks to render")
    log = log or _safe_print
    run = runner or _run
    reframe = reframe or _default_reframe
    probe_size = probe_size or _probe_size
    os.makedirs(workdir, exist_ok=True)
    token = uuid.uuid4().hex[:6]
    joined = os.path.join(workdir, f"temp_film_join_{token}.mp4")
    sped = os.path.join(workdir, f"temp_film_speed_{token}.mp4")
    framed = os.path.join(workdir, f"temp_film_framed_{token}.mp4")
    parts = []
    try:
        src_size = probe_size(video_path)
        log(f"🎞️  Cutting {len(shots)} chunks ({src_size[0]}x{src_size[1]})")
        parts = cut_shots(video_path, shots, workdir, runner=run, out_size=src_size)
        concat_parts(parts, joined, workdir, runner=run)
        log(f"⏩ Speed {speed}x")
        run(fp.speed_command(joined, sped, speed))
        log(f"📐 Reframe -> {output_format}")
        if not reframe(sped, framed, output_format):
            raise RenderError("reframe failed")
        log("🔇 Mute + fps" if mute else "🔊 Loudness + fps")
        run(finalize_command(framed, out_path, fps=fps, mute=mute))
        return {"shots": len(shots), "speed": speed, "output_format": output_format,
                "muted": bool(mute), "output": out_path}
    finally:
        if not keep_parts:
            _cleanup(parts + [joined, sped, framed])


# Kept for callers that still say "short"; same function.
render_short = render_silent


def narration_mix_command(video_path, vo_chunks, out_path, *, keep_ranges=(),
                          bed_level=0.18, duration=None, loudnorm=FILM_LOUDNORM):
    """Recap audio: the narration chunks placed at their ``voice_start`` over
    a picture whose own soundtrack is MUTED, except in ``keep_ranges``
    (finished seconds) where the film's dialogue is the evidence; there the
    bed sits at ``bed_level`` and ducks under the voice
    (music.MIX_PROFILES['narration']). Video is stream-copied."""
    from music import MIX_PROFILES
    p = MIX_PROFILES["narration"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", video_path]
    for c in vo_chunks:
        cmd += ["-i", c["path"]]
    filters = []
    vo_labels = []
    for i, c in enumerate(vo_chunks):
        delay = int(round(float(c["voice_start"]) * 1000))
        filters.append(f"[{i + 1}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                       f"adelay={delay}:all=1[vo{i}]")
        vo_labels.append(f"[vo{i}]")
    if vo_labels:
        filters.append(f"{''.join(vo_labels)}amix=inputs={len(vo_labels)}:duration=longest:normalize=0[vo]")
    else:
        filters.append("anullsrc=r=48000:cl=stereo[vo]")
    if keep_ranges:
        enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep_ranges)
        filters.append(f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                       f"volume=0:enable='not({enable})',volume={bed_level:.3f}:enable='{enable}'[bed]")
        filters.append("[vo]asplit=2[vo1][vo2]")
        filters.append(f"[bed][vo2]sidechaincompress=threshold={p['threshold']}:ratio={p['max_ratio']:.0f}"
                       f":attack={p['attack']}:release={p['release']}[bedd]")
        filters.append("[vo1][bedd]amix=inputs=2:duration=first:normalize=0[mix]")
    else:
        filters.append("[vo]anull[mix]")
    filters.append(f"[mix]{loudnorm}[aout]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[aout]"]
    if duration:
        cmd += ["-t", f"{float(duration):.3f}"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k", *METADATA_SCRUB,
            "-movflags", "+faststart", out_path]
    return cmd


def render_narrated(video_path, segments, out_path, workdir, *, speed=fp.DEFAULT_SPEED,
                    output_format="vertical", fps=DEFAULT_FPS, runner=None, reframe=None,
                    probe_size=None, keep_parts=False, log=None):
    """A recap part: fitted source ``segments`` (from
    movierecap.fitted_to_source or film_voice.narrate_plan) cut, joined,
    sped, reframed, then the narration laid over a muted bed. Segments carry
    ``voice_start`` / ``chunk`` (VO wav) / ``keep_audio`` / ``shot_start`` /
    ``shot_length``."""
    if not segments:
        raise RenderError("no segments to render")
    log = log or _safe_print
    run = runner or _run
    reframe = reframe or _default_reframe
    probe_size = probe_size or _probe_size
    os.makedirs(workdir, exist_ok=True)
    token = uuid.uuid4().hex[:6]
    joined = os.path.join(workdir, f"temp_film_join_{token}.mp4")
    sped = os.path.join(workdir, f"temp_film_speed_{token}.mp4")
    framed = os.path.join(workdir, f"temp_film_framed_{token}.mp4")
    mixed = os.path.join(workdir, f"temp_film_mix_{token}.mp4")
    parts = []
    try:
        shots = [{"start": s["start"], "end": s["end"], "hflip": bool(s.get("flip"))} for s in segments]
        log(f"🎞️  Cutting {len(shots)} chunks")
        parts = cut_shots(video_path, shots, workdir, runner=run, out_size=probe_size(video_path))
        concat_parts(parts, joined, workdir, runner=run)
        log(f"⏩ Speed {speed}x")
        run(fp.speed_command(joined, sped, speed))
        log(f"📐 Reframe -> {output_format}")
        if not reframe(sped, framed, output_format):
            raise RenderError("reframe failed")
        vo_chunks = [{"path": s["chunk"]["path"], "voice_start": s["voice_start"]}
                     for s in segments if s.get("chunk") and s.get("voice_start") is not None]
        keep = [(s["shot_start"], s["shot_start"] + s["shot_length"])
                for s in segments if s.get("keep_audio")]
        total = sum(s["shot_length"] for s in segments)
        log(f"🎙️  Narration: {len(vo_chunks)} lines, bed kept on {len(keep)} chunks")
        run(narration_mix_command(framed, vo_chunks, mixed, keep_ranges=keep, duration=total))
        log("🔊 Loudness + fps")
        run(finalize_command(mixed, out_path, fps=fps, loudnorm="anull"))
        return {"output": out_path, "chunks": len(segments), "narrated": len(vo_chunks),
                "bed_kept": len(keep), "duration": round(total, 2)}
    finally:
        if not keep_parts:
            _cleanup(parts + [joined, sped, framed, mixed])
