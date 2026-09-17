"""Render assembly for the film modules: a shot (or chunk) plan -> one finished
short. Shared by Movie Shorts and Movie Recap; only the plan differs.

Order, and why each step sits where it does (docs/film-modules-plan.md §1.3):

1. cut every shot from the source with its own crop and grade (re-encode,
   uniform params, ``-ss`` before ``-i`` so an 86-shot plan does not decode a
   two-hour file 86 times from the head)
2. concat with ``-c copy``
3. speed 1.25x — BEFORE the reframe, because reframe_v2 emits sendcmd crop
   timelines and a PTS change afterwards lands every command on the wrong
   frame
4. reframe to the output format (main.render_clip, injectable)
5. captions from the film's own cues, remapped onto the cut and divided by
   the speed factor (or they drift: fine at 20 s, wrong at 120 s)
6. music bed over the WHOLE short — never per shot, or the track restarts on
   every cut
7. final loudness pass, LRA 7 (this format is flat and loud), and the fps

Every external step is a hook with a default, so tests drive the whole
assembly with fakes and never touch ffmpeg, whisper or the GPU.
"""

import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor

import film_prep as fp
from ffmpeg_utils import METADATA_SCRUB, QUALITY_FAST, video_encode_args

FILM_LOUDNORM = "loudnorm=I=-14:TP=-2.0:LRA=7"
DEFAULT_FPS = 60
OUTPUT_FORMATS = ("vertical", "square")
# Turing consumer cards allow 2-3 concurrent NVENC sessions; the rest of the
# ~86 tiny encodes go to CPU. Both knobs are env because driver behaviour varies.
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


def grade_chain(name):
    """The eq/colorbalance chain for a named grade, or None for neutral."""
    if not name or name in ("none", "neutral"):
        return None
    from cinematic import COLOR_GRADES
    return COLOR_GRADES.get(name)


def shot_filter(shot, grade=None, blur=None, out_size=None):
    """``-vf`` for one shot: static punch-in crop, optional region blur, grade,
    and a scale back to ``out_size`` (w, h). The scale is not cosmetic: every
    crop scale yields a different frame size, and the ``-c copy`` concat joins
    mismatched parts into a stream the next pass cannot decode."""
    parts = []
    crop = fp.crop_filter(shot.get("scale", 1.0), shot.get("anchor", (0.5, 0.5)))
    if crop:
        parts.append(crop)
    if out_size and (crop or shot.get("blur") or blur):
        w, h = int(out_size[0]) - int(out_size[0]) % 2, int(out_size[1]) - int(out_size[1]) % 2
        parts.append(f"scale={w}:{h}:flags=lanczos")
    if shot.get("hflip"):
        # A manual, reasoned editorial flip (movierecap validates the reason
        # and refuses patterns); never set automatically.
        parts.append("hflip")
    blur = blur or shot.get("blur")
    if blur:
        # Blur a rectangle (fractions of the cropped frame) for advertiser
        # safety: split, crop the region, gblur it, overlay it back.
        x, y, w, h = (max(0.0, min(1.0, float(blur[k]))) for k in ("x", "y", "w", "h"))
        parts.append(
            f"split[__b0][__b1];[__b1]crop=iw*{w:.3f}:ih*{h:.3f}:iw*{x:.3f}:ih*{y:.3f},"
            f"gblur=sigma=24[__bb];[__b0][__bb]overlay=W*{x:.3f}:H*{y:.3f}")
    chain = grade_chain(grade)
    if chain:
        parts.append(chain)
    return ",".join(parts) if parts else None


def shot_cut_command(input_path, shot, out_path, *, grade=None, encoder=None, out_size=None):
    """ffmpeg argv cutting one shot with its crop and grade. ``encoder`` is
    "nvenc" / "cpu" / None (None = ffmpeg_utils decides). ``out_size`` is the
    common (w, h) every cropped part is scaled back to."""
    vf = shot_filter(shot, grade, out_size=out_size)
    if encoder == "cpu":
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    elif encoder == "nvenc":
        enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-cq", "21"]
    else:
        enc = video_encode_args(QUALITY_FAST)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{float(shot['start']):.3f}", "-to", f"{float(shot['end']):.3f}",
           "-i", input_path]
    if vf:
        # A filter with labels needs -filter_complex; a plain chain is -vf.
        if "[" in vf:
            cmd += ["-filter_complex", vf]
        else:
            cmd += ["-vf", vf]
    cmd += [*enc, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            *METADATA_SCRUB, "-movflags", "+faststart", out_path]
    return cmd


def cut_shots(input_path, shots, workdir, *, grade=None, runner=None,
              nvenc_parallel=NVENC_PARALLEL, cpu_parallel=CPU_PARALLEL, out_size=None):
    """Encode every shot, a few in parallel, ALL with the same encoder.

    Splitting the lanes between NVENC and libx264 was tried first (the plan's
    idea for an 8 GB Turing card) and produced parts whose streams differ in
    SPS/profile; the ``-c copy`` concat then joins them without complaint and
    the next ffmpeg pass fails on the result with an empty stderr. Uniform
    parameters are what make the concat demuxer safe (recut.cut_commands says
    the same). Parallelism is capped at ``nvenc_parallel`` on the GPU (Turing
    allows 2-3 sessions) and ``cpu_parallel`` on libx264. Returns the part
    paths in shot order.
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
        run(shot_cut_command(input_path, shots[i], parts[i], grade=grade, encoder=None,
                             out_size=out_size))

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


def finalize_command(input_path, out_path, fps=DEFAULT_FPS, loudnorm=FILM_LOUDNORM):
    """Last pass: frame rate and the one loudness normalisation."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", input_path]
    if fps:
        cmd += ["-vf", f"fps={int(fps)}", *video_encode_args(QUALITY_FAST)]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-af", loudnorm, "-c:a", "aac", "-b:a", "160k",
            *METADATA_SCRUB, "-movflags", "+faststart", out_path]
    return cmd


def captions_for_cut(cues, shots, speed):
    """The film's own dialogue on the FINISHED timeline: cues -> transcript,
    remapped through the shot list (recut.virtual_transcript), divided by the
    speed factor."""
    from recut import virtual_transcript
    transcript = fp.cues_to_transcript(cues)
    segments = [{"start": s["start"], "end": s["end"]} for s in shots]
    remapped = virtual_transcript(transcript, segments)
    return fp.scale_transcript(remapped, speed)


def _default_reframe(work_path, out_path, output_format):
    from main import render_clip
    return render_clip(work_path, out_path, output_format)


def _default_captioner(video_path, transcript, out_path, preset, video_w, video_h):
    import subtitles
    ass_path = out_path + ".ass"
    dur = fp.transcript_duration(transcript) + 2.0
    if not subtitles.generate_ass_styled(transcript, 0.0, dur, ass_path, preset=preset,
                                         video_w=video_w, video_h=video_h):
        return False
    try:
        subtitles.burn_subtitles(video_path, ass_path, out_path)
    finally:
        try:
            os.remove(ass_path)
        except OSError:
            pass
    return os.path.exists(out_path)


def _default_music(video_path, track_path, out_path, spec):
    import music
    return music.apply_music(video_path, spec, out_path, profile="dialogue",
                             track_path=track_path)


def _probe_size(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            stderr=subprocess.STDOUT, timeout=60).decode().strip().split("x")
        return int(out[0]), int(out[1])
    except Exception:
        return (1080, 1920)


def render_short(video_path, shots, out_path, workdir, *, speed=fp.DEFAULT_SPEED,
                 output_format="vertical", grade=None, fps=DEFAULT_FPS,
                 cues=None, caption_preset="film_pop", music_track=None,
                 music_spec=None, runner=None, reframe=None, captioner=None,
                 mixer=None, probe_size=None, keep_parts=False, log=None):
    """The whole assembly. Returns a dict describing what was produced."""
    if output_format not in OUTPUT_FORMATS:
        raise RenderError(f"output_format must be one of {OUTPUT_FORMATS}")
    if not shots:
        raise RenderError("no shots to render")
    log = log or _safe_print
    run = runner or _run
    reframe = reframe or _default_reframe
    captioner = captioner or _default_captioner
    mixer = mixer or _default_music
    probe_size = probe_size or _probe_size
    os.makedirs(workdir, exist_ok=True)
    token = uuid.uuid4().hex[:6]
    joined = os.path.join(workdir, f"temp_film_join_{token}.mp4")
    sped = os.path.join(workdir, f"temp_film_speed_{token}.mp4")
    framed = os.path.join(workdir, f"temp_film_framed_{token}.mp4")
    captioned = os.path.join(workdir, f"temp_film_cap_{token}.mp4")
    mixed = os.path.join(workdir, f"temp_film_mix_{token}.mp4")
    temps = [joined, sped, framed, captioned, mixed]
    report = {"shots": len(shots), "speed": speed, "output_format": output_format,
              "grade": grade or "none", "captions": False, "music": None}
    parts = []
    try:
        src_size = probe_size(video_path)
        log(f"🎞️  Cutting {len(shots)} shots ({src_size[0]}x{src_size[1]})")
        parts = cut_shots(video_path, shots, workdir, grade=grade, runner=run, out_size=src_size)
        concat_parts(parts, joined, workdir, runner=run)

        log(f"⏩ Speed {speed}x")
        run(fp.speed_command(joined, sped, speed))

        log(f"📐 Reframe -> {output_format}")
        if not reframe(sped, framed, output_format):
            raise RenderError("reframe failed")
        current = framed

        if cues:
            transcript = captions_for_cut(cues, shots, speed)
            if transcript["segments"]:
                w, h = probe_size(current)
                log(f"💬 Captions ({caption_preset})")
                if captioner(current, transcript, captioned, caption_preset, w, h):
                    current = captioned
                    report["captions"] = True

        if music_track:
            log(f"🎵 Music bed {os.path.basename(music_track)}")
            spec = {"volume_db": -18.0, "duck": 100.0, "fade_out": 1.0, **(music_spec or {})}
            if mixer(current, music_track, mixed, spec):
                current = mixed
                report["music"] = os.path.basename(music_track)

        log("🔊 Loudness + fps")
        run(finalize_command(current, out_path, fps=fps))
        report["output"] = out_path
        return report
    finally:
        if not keep_parts:
            for p in parts + temps:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass


def narration_mix_command(video_path, vo_chunks, out_path, *, keep_ranges=(),
                          bed_level=0.18, duration=None, loudnorm=FILM_LOUDNORM):
    """Recap audio: the recorded narration chunks placed at their measured
    ``voice_start`` over a picture whose own soundtrack is MUTED, except in
    ``keep_ranges`` (finished seconds) where the film's dialogue is the
    evidence; there the bed sits at ``bed_level`` and ducks under the voice
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
                    keep_parts=False, log=None):
    """A recap part: fitted source ``segments`` (from
    movierecap.fitted_to_source) cut, joined, sped, reframed, then the
    narration laid over a muted bed. Segments carry ``voice_start`` /
    ``chunk`` (VO wav) / ``keep_audio`` / ``shot_start`` / ``shot_length``."""
    if not segments:
        raise RenderError("no segments to render")
    log = log or _safe_print
    run = runner or _run
    reframe = reframe or _default_reframe
    os.makedirs(workdir, exist_ok=True)
    token = uuid.uuid4().hex[:6]
    joined = os.path.join(workdir, f"temp_film_join_{token}.mp4")
    sped = os.path.join(workdir, f"temp_film_speed_{token}.mp4")
    framed = os.path.join(workdir, f"temp_film_framed_{token}.mp4")
    mixed = os.path.join(workdir, f"temp_film_mix_{token}.mp4")
    parts = []
    try:
        shots = [{"start": s["start"], "end": s["end"], "scale": 1.0,
                  "hflip": bool(s.get("flip"))} for s in segments]
        log(f"🎞️  Cutting {len(shots)} chunks")
        parts = cut_shots(video_path, shots, workdir, runner=run, out_size=_probe_size(video_path))
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
            for p in parts + [joined, sped, framed, mixed]:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass


def scene_cuts_provider(video_path, workdir, runner=None):
    """``(start, end) -> [cut times]`` using scene_detection on a stream-copied
    slice of the beat. Used by expand_plan so real cuts become shot
    boundaries; any failure returns no cuts and the beat is jittered instead."""
    run = runner or _run

    def cuts(start, end):
        slice_path = os.path.join(workdir, f"temp_film_scene_{uuid.uuid4().hex[:6]}.mp4")
        try:
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", video_path,
                 "-c", "copy", "-an", slice_path])
            from scene_detection import detect_scenes
            scenes, _fps = detect_scenes(slice_path)
            out = []
            for a, _b in scenes[1:]:
                out.append(start + float(a.get_seconds()))
            return out
        except Exception as exc:  # noqa: BLE001 - optional refinement
            print(f"   ⚠️ scene cuts skipped for {start:.1f}-{end:.1f}: {exc}")
            return []
        finally:
            if os.path.exists(slice_path):
                try:
                    os.remove(slice_path)
                except OSError:
                    pass
    return cuts


def measure_cut_rhythm(video_path, threshold=0.3):
    """Detected cut times of a finished short (ffmpeg scene score). The
    acceptance check from the plan: mean gap should land in 1.3-1.7 s, or the
    crop ladder is not varying enough for a viewer to register the cuts."""
    cmd = ["ffmpeg", "-hide_banner", "-i", video_path,
           "-vf", f"select='gt(scene,{threshold})',metadata=print:file=-",
           "-an", "-f", "null", "-"]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600)
    times = []
    for line in res.stdout.decode("utf-8", "replace").splitlines():
        if "pts_time:" in line:
            try:
                times.append(float(line.split("pts_time:")[1].split()[0]))
            except (IndexError, ValueError):
                continue
    gaps = [b - a for a, b in zip(times, times[1:])]
    return {"cuts": len(times), "mean_gap": (sum(gaps) / len(gaps)) if gaps else None,
            "times": times}


def cleanup_workdir(workdir):
    for name in os.listdir(workdir) if os.path.isdir(workdir) else []:
        if name.startswith("temp_film_"):
            try:
                os.remove(os.path.join(workdir, name))
            except OSError:
                pass
    shutil.rmtree(workdir, ignore_errors=True) if os.path.basename(workdir).startswith("film_work_") else None
