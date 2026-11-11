"""Reframe engine v2: analyze in Python, render natively in ffmpeg."""
import os
import subprocess
import tempfile

import active_speaker
import camera_inset
import gpu_decode
import layout_ranges
import punch_in
import screencast_layout
import split_layout
from ffmpeg_utils import (video_encode_args, blurred_backdrop, escape_filter_value,
                          QUALITY_FAST, METADATA_SCRUB)

ANALYSIS_MAX_WIDTH = 640
DELIVERY_MIN_WIDTH = 1080


# --- pure helpers (CI-testable) --------------------------------------------

def delivery_size(orig_w, orig_h, aspect_ratio):
    """Output (width, height) for a reframe of this source."""
    out_h = orig_h
    out_w = int(out_h * aspect_ratio)
    if out_w > orig_w:
        out_w = orig_w
        out_h = int(out_w / aspect_ratio)

    if out_w < DELIVERY_MIN_WIDTH:
        out_w = DELIVERY_MIN_WIDTH
        out_h = int(round(out_w / aspect_ratio))

    return out_w + (out_w % 2), out_h + (out_h % 2)


def source_already_fits(orig_w, orig_h, aspect_ratio, tol=0.01):
    """True when the source is already at (or past) the target aspect."""
    return orig_w / float(orig_h) <= aspect_ratio * (1 + tol)


def dedupe_sendcmd_lines(xs, fps, target="crop@c"):
    """sendcmd lines setting crop x per frame, deduped to change-points."""
    lines = []
    prev = None
    for i, x in enumerate(xs):
        if x != prev:
            lines.append(f"{i / fps:.4f} {target} x {x};")
            prev = x
    return lines


def scene_frame_ranges(scene_boundaries, strategies, total_frames):
    """Clamp scene (start, end) frame ranges to the decoded frame count."""
    ranges = []
    for i, (start_f, end_f) in enumerate(scene_boundaries):
        strategy = strategies[i] if i < len(strategies) else 'TRACK'
        start_f = max(0, min(start_f, total_frames))
        end_f = max(start_f, min(end_f, total_frames))
        if end_f > start_f:
            ranges.append((start_f, end_f, strategy))
    return ranges


def concat_list_content(segment_paths):
    return "".join(f"file '{p}'\n" for p in segment_paths)


GENERAL_CONTENT_HEIGHT_RATIO = float(
    os.environ.get("GENERAL_CONTENT_HEIGHT_RATIO", "0.42"))


def full_width_content_height(orig_w, orig_h, out_w):
    """Height the source fills when its FULL width is kept (even)."""
    fg_h = int(round(out_w * orig_h / float(orig_w)))
    return fg_h + (fg_h % 2)


def _wide_crop(input_video, orig_w, orig_h, out_w, out_h, start_f, end_f, focus):
    """The columns an automatic WIDE scene without a narrow reading area shows
    (screencast_layout.wide_crop), centred on the scene's motion, else on the
    shot check's reading area, else on the frame. None keeps the whole width."""
    probe = screencast_layout.wide_crop(orig_w, orig_h, out_w, out_h)
    if not probe:
        return None
    centre = None
    try:
        centre = screencast_layout.wide_action_centre(
            input_video, start_f, end_f, probe[1] / float(orig_w))
    except Exception as e:
        print(f"   ⚠️ WIDE motion probe failed ({e}); centring the crop.")
    if centre is None:
        centre = (focus[0] + focus[1]) / 2.0 if focus else 0.5
    return screencast_layout.wide_crop(orig_w, orig_h, out_w, out_h, centre)


def general_filtergraph(out_w, out_h, content_h=None, orig_w=None, orig_h=None):
    """Blurred-background 'general shot' layout."""
    fg_h = content_h if content_h else int(out_h * GENERAL_CONTENT_HEIGHT_RATIO)
    if orig_w and orig_h:
        fg_h = max(fg_h, full_width_content_height(orig_w, orig_h, out_w))
    fg_h += fg_h % 2
    return (
        f"[0:v]split=2[bga][fga];"
        f"[bga]{blurred_backdrop(out_w, out_h, 12)}[bg];"
        f"[fga]scale=-2:{fg_h},crop=w=min(iw\\,{out_w}):h=ih[fg];"
        f"[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2,setsar=1[v]"
    )


# --- analysis ---------------------------------------------------------------

def apply_crop_overrides(xs, strategies, scene_boundaries, overrides,
                         crop_w, orig_w, orig_h=None, splits=None):
    """Frame the scenes the user positioned by hand."""
    max_x = max(0, orig_w - crop_w)

    def to_x(fraction):
        return max(0, min(int(round(float(fraction) * orig_w - crop_w / 2)), max_x))

    for raw_idx, value in (overrides or {}).items():
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(scene_boundaries):
            continue
        start_f, end_f = scene_boundaries[idx]
        end_f = min(end_f, len(xs))
        if end_f <= start_f:
            continue

        if isinstance(value, dict):
            def point(half):
                if isinstance(half, dict):
                    fx, fy = float(half['x']), float(half.get('y', 0.5))
                else:
                    fx, fy = float(half), 0.5
                return (fx * orig_w, fy * orig_h)

            try:
                centres = (point(value['top']), point(value['bottom']))
            except (KeyError, TypeError, ValueError):
                continue
            if splits is None or not orig_h:
                continue
            splits[start_f] = centres
            strategies[idx] = 'SPLIT'
            continue

        try:
            x = to_x(value)
        except (TypeError, ValueError):
            continue
        xs[start_f:end_f] = [x] * (end_f - start_f)
        strategies[idx] = 'TRACK'
        if splits is not None:
            splits.pop(start_f, None)

    return xs, strategies


_STATIC_LAYOUTS = ('GENERAL', 'SPLIT', 'SCREENCAST', 'WIDE', 'INSET', 'ALTERNATE')
SPARSE_TRAJECTORY = os.environ.get("SPARSE_TRAJECTORY", "1").strip() != "0"


class _SparseMismatch(Exception):
    """The sparse pass did not see the frames it expected; redo it in full."""


def _analysis_size(orig_w, orig_h):
    small_w = min(ANALYSIS_MAX_WIDTH, orig_w)
    if small_w % 2:
        small_w -= 1
    small_h = max(int(orig_h * small_w / orig_w), 2)
    if small_h % 2:
        small_h += 1
    return small_w, small_h


def _walk_scenes(scenes_boundaries, scene_strategies, n_frames):
    current = 0
    for frame_number in range(n_frames):
        if current < len(scenes_boundaries):
            _start_f, end_f = scenes_boundaries[current]
            if frame_number >= end_f and current < len(scenes_boundaries) - 1:
                current += 1
        strategy = (scene_strategies[current]
                    if current < len(scene_strategies) else 'TRACK')
        yield frame_number, current, strategy


def _detection_frames(scenes_boundaries, scene_strategies, n_frames, stride,
                      cut_reset):
    frames = []
    for frame_number, current, strategy in _walk_scenes(
            scenes_boundaries, scene_strategies, n_frames):
        if strategy in _STATIC_LAYOUTS:
            continue
        is_scene_start = (current < len(scenes_boundaries)
                          and frame_number == scenes_boundaries[current][0])
        if frame_number % stride == 0 or (is_scene_start and cut_reset):
            frames.append(frame_number)
    return frames


def select_expr(frames, stride):
    terms = []
    i = 0
    while i < len(frames):
        j = i
        if frames[i] % stride == 0:
            while j + 1 < len(frames) and frames[j + 1] == frames[j] + stride:
                j += 1
        if j - i >= 2:
            terms.append(f"between(n,{frames[i]},{frames[j]})*not(mod(n,{stride}))")
        else:
            j = i
            terms.append(f"eq(n,{frames[i]})")
        i = j + 1
    return gpu_decode.any_of(terms)


def _trajectory_loop(next_frame, sparse, n_frames, scenes_boundaries,
                     scene_strategies, orig_w, scale, cameraman, tracker):
    import main as m

    xs = []
    frame_number = 0
    current_scene_index = 0
    while not sparse or frame_number < n_frames:
        frame = None
        if not sparse:
            frame = next_frame()
            if frame is None:
                break

        if current_scene_index < len(scenes_boundaries):
            start_f, end_f = scenes_boundaries[current_scene_index]
            if frame_number >= end_f and current_scene_index < len(scenes_boundaries) - 1:
                current_scene_index += 1

        strategy = (scene_strategies[current_scene_index]
                    if current_scene_index < len(scene_strategies) else 'TRACK')

        if strategy in _STATIC_LAYOUTS:
            cameraman.current_center_x = orig_w / 2
            cameraman.target_center_x = orig_w / 2
            xs.append(None)
        else:
            is_scene_start = (
                current_scene_index < len(scenes_boundaries)
                and frame_number == scenes_boundaries[current_scene_index][0])
            cut = is_scene_start and m.SCENE_CUT_RESET
            if cut:
                tracker.reset()
                cameraman.begin_scene()

            if frame_number % m.DETECT_STRIDE == 0 or cut:
                if sparse:
                    frame = next_frame(frame_number)
                candidates = m.detect_face_candidates(frame)
                for cand in candidates:
                    cand['box'] = [int(v * scale) for v in cand['box']]
                    cand['score'] = cand['box'][2] * cand['box'][3]
                target_box = tracker.get_target(candidates, frame_number, orig_w)
                if target_box:
                    cameraman.update_target(target_box)
                elif frame_number % m.YOLO_FALLBACK_STRIDE == 0 or cut:
                    person_box = m.detect_person_yolo(frame)
                    if person_box:
                        cameraman.update_target([int(v * scale) for v in person_box])

            x1, _y1, _x2, _y2 = cameraman.get_crop_box(force_snap=is_scene_start)
            xs.append(x1)

        frame_number += 1
    return xs


def _read_frame(stream, frame_bytes, shape):
    import numpy as np
    buf = stream.read(frame_bytes)
    if len(buf) < frame_bytes:
        return None
    return np.frombuffer(buf, dtype=np.uint8).reshape(shape)


def _sparse_trajectory(input_video, n_frames, on_gpu, small_w, small_h,
                       scenes_boundaries, scene_strategies, orig_w, scale,
                       cameraman, tracker):
    import main as m

    wanted = _detection_frames(scenes_boundaries, scene_strategies, n_frames,
                               m.DETECT_STRIDE, m.SCENE_CUT_RESET)
    piped = sorted(set(wanted) | {n_frames - 1})
    expr = gpu_decode.any_of([select_expr(piped, m.DETECT_STRIDE), f"gte(n,{n_frames})"])
    chain = f"scale={small_w}:{small_h}"
    vf = (f"select='{expr}',{gpu_decode.DOWNLOAD},{chain}" if on_gpu
          else f"select='{expr}',{chain}")
    frame_bytes = small_w * small_h * 3
    shape = (small_h, small_w, 3)
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", *gpu_decode.input_args(on_gpu),
         "-i", input_video, "-vf", vf, "-fps_mode", "passthrough",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=frame_bytes * 4)
    queue = iter(piped)

    def next_frame(frame_number):
        expected = next(queue, None)
        if expected != frame_number:
            raise _SparseMismatch(f"asked frame {frame_number}, stream has {expected}")
        frame = _read_frame(proc.stdout, frame_bytes, shape)
        if frame is None:
            raise _SparseMismatch(f"stream ended before frame {frame_number}")
        return frame

    try:
        xs = _trajectory_loop(next_frame, True, n_frames, scenes_boundaries,
                              scene_strategies, orig_w, scale, cameraman, tracker)
        for frame_number in queue:
            if _read_frame(proc.stdout, frame_bytes, shape) is None:
                raise _SparseMismatch(f"stream ended before frame {frame_number}")
        if proc.stdout.read(1):
            raise _SparseMismatch(f"clip has more than {n_frames} frames")
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
    return xs


def _analyze_trajectory(input_video, scenes_boundaries, scene_strategies,
                        fps, orig_w, orig_h, cameraman, tracker):
    import copy

    small_w, small_h = _analysis_size(orig_w, orig_h)
    scale = orig_w / small_w
    n_frames = gpu_decode.decoded_frames(input_video) if SPARSE_TRAJECTORY else None
    on_card = gpu_decode.usable(input_video)
    saved = copy.deepcopy(cameraman.__dict__), copy.deepcopy(tracker.__dict__)

    def restore():
        cameraman.__dict__.clear()
        cameraman.__dict__.update(copy.deepcopy(saved[0]))
        tracker.__dict__.clear()
        tracker.__dict__.update(copy.deepcopy(saved[1]))

    if n_frames:
        for on_gpu in ([True, False] if on_card else [False]):
            try:
                xs = _sparse_trajectory(input_video, n_frames, on_gpu, small_w, small_h,
                                        scenes_boundaries, scene_strategies, orig_w,
                                        scale, cameraman, tracker)
                gpu_decode.note(on_gpu)
                return xs
            except _SparseMismatch as e:
                print(f"   ⚠️ Sparse trajectory pass ({'GPU' if on_gpu else 'CPU'}): {e}")
                if on_gpu:
                    gpu_decode.note(True, failed=True)
                restore()

    for on_gpu in ([True, False] if on_card and not n_frames else [False]):
        frame_bytes = small_w * small_h * 3
        proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", *gpu_decode.input_args(on_gpu),
             "-i", input_video,
             "-vf", gpu_decode.vf(f"scale={small_w}:{small_h}", on_gpu),
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=frame_bytes * 4)
        try:
            xs = _trajectory_loop(
                lambda: _read_frame(proc.stdout, frame_bytes, (small_h, small_w, 3)),
                False, None, scenes_boundaries, scene_strategies, orig_w, scale,
                cameraman, tracker)
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if not on_gpu:
            return xs
        if rc == 0 and xs:
            gpu_decode.note(True)
            return xs
        gpu_decode.note(True, failed=True)
        print("   ⚠️ GPU trajectory decode failed, decoding on the CPU")
        restore()


# --- render -----------------------------------------------------------------

def _run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, timeout=1800)


def _run_segment(input_video, ss, dur, graph, seg_path):
    """Encode one scene, decoding on GPU when usable."""
    def build(on_gpu):
        on_gpu = on_gpu and gpu_decode.graph_ok(graph)
        return [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{ss:.4f}", "-t", f"{dur:.4f}",
            *gpu_decode.input_args(on_gpu), "-i", input_video,
            "-filter_complex", gpu_decode.graph(graph, on_gpu), "-map", "[v]",
            *video_encode_args(QUALITY_FAST), "-an", seg_path,
        ]

    def runner(cmd):
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=1800)

    result = gpu_decode.run(build, runner, path=input_video)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args,
                                            result.stdout, result.stderr)


def render(input_video, final_output_video, aspect_ratio, content_ranges=None,
           force_strategy=None, crop_overrides=None):
    """Full v2 reframe of one clip. Raises on failure (caller falls back)."""
    import main as m
    content_ranges = content_ranges or []

    print("   🚀 Reframe engine v2 (ffmpeg-native render)")
    scenes, fps = m.detect_scenes(input_video)
    fps = float(fps)
    orig_w, orig_h = m.get_video_resolution(input_video)

    out_w, out_h = delivery_size(orig_w, orig_h, aspect_ratio)

    if not scenes:
        import cv2
        cap = cv2.VideoCapture(input_video)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        from scenedetect import FrameTimecode
        scenes = [(FrameTimecode(0, fps), FrameTimecode(total, fps))]

    scene_boundaries = [(s.get_frames(), e.get_frames()) for s, e in scenes]
    passthrough = source_already_fits(orig_w, orig_h, aspect_ratio)
    if force_strategy:
        strategies = [force_strategy] * len(scenes)
        content_ranges = []
        print(f"   🎯 Framing override: every scene -> {force_strategy}")
    elif passthrough:
        strategies = ['TRACK'] * len(scenes)
        content_ranges = []
        print(f"   ↕️  Source is already {orig_w}x{orig_h} vertical — "
              f"passing it through, no reframe")
    else:
        strategies = m.analyze_scenes_strategy(input_video, scenes)
        if not content_ranges and screencast_layout.ENABLED:
            dur = scene_boundaries[-1][1] / fps if scene_boundaries else 0.0
            content_ranges = screencast_layout.screen_ranges(
                input_video, dur, scenes, strategies)

    splits = {}
    split_scene_of = {}
    detected_splits = {} if passthrough else split_layout.detect_split_scenes(
        input_video, scenes, strategies)
    for scene_idx, centres in detected_splits.items():
        strategies[scene_idx] = 'SPLIT'
        start_f = scene_boundaries[scene_idx][0]
        splits[start_f] = centres
        split_scene_of[start_f] = scene_idx

    alternates = {}
    if splits and active_speaker.ENABLED:
        for start_f in list(splits):
            scene_idx = split_scene_of[start_f]
            end_f = scene_boundaries[scene_idx][1]
            verdicts = active_speaker.verdicts_for_scene(
                input_video, start_f, end_f, fps, splits[start_f])
            if not active_speaker.is_conversation(verdicts):
                a, b = active_speaker.shares(verdicts)
                print(f"   🔇 Scene {scene_idx}: one speaker holds the floor "
                      f"({max(a, b):.0%}) — not stacking")
                del splits[start_f]
                strategies[scene_idx] = 'GENERAL'
            elif active_speaker.CUT_MODE:
                strategies[scene_idx] = 'ALTERNATE'
                alternates[start_f] = (
                    active_speaker.hold(verdicts), splits.pop(start_f))
    if splits:
        print(f"   🪞 SPLIT layout on {len(splits)} scene(s)")
    if alternates:
        print(f"   🎬 Speaker-cut layout on {len(alternates)} scene(s)")

    inset = None
    if content_ranges and screencast_layout.ENABLED:
        try:
            inset = camera_inset.detect(input_video)
        except Exception as e:
            print(f"   ⚠️ Inset check failed ({e}) — using the screen layouts.")
        if inset:
            print(f"   📹 Webcam inset at {inset}")

    screencasts = {}
    focus_crops = {}
    insets = {}
    wide_count = 0
    inset_count = 0
    if content_ranges:
        for scene_idx, (plan, centre) in screencast_layout.detect_screencast_scenes(
                input_video, scenes, strategies, content_ranges).items():
            start_f, end_f = scene_boundaries[scene_idx]
            box = None
            if inset and camera_inset.present_in_scene(
                    input_video, inset, start_f, end_f):
                box = inset
            elif plan == 'WIDE' and screencast_layout.presenter_cam(
                    start_f / fps, end_f / fps, content_ranges):
                box = camera_inset.detect_in_scene(input_video, start_f, end_f)
            if box:
                plan, centre = 'INSET', None
            strategies[scene_idx] = plan
            splits.pop(start_f, None)
            if plan == 'SCREENCAST':
                screencasts[start_f] = centre
            elif plan == 'INSET':
                insets[start_f] = box
                inset_count += 1
            else:
                wide_count += 1
                crop = screencast_layout.focus_crop(
                    orig_w, orig_h, out_w, out_h, centre)
                if not crop:
                    crop = _wide_crop(input_video, orig_w, orig_h, out_w, out_h,
                                      start_f, end_f, centre)
                if crop:
                    focus_crops[start_f] = crop
    if screencasts:
        print(f"   🖥️ SCREENCAST layout on {len(screencasts)} scene(s)")
    if wide_count:
        print(f"   📐 Full-width layout on {wide_count} scene(s)")
    if inset_count:
        print(f"   📹 Camera-inset layout on {inset_count} scene(s)")

    cameraman = m.SmoothedCameraman(out_w, out_h, orig_w, orig_h, aspect_ratio=aspect_ratio)
    tracker = m.SpeakerTracker(cooldown_frames=30)

    xs = _analyze_trajectory(input_video, scene_boundaries, strategies, fps,
                             orig_w, orig_h, cameraman, tracker)
    if not xs:
        raise RuntimeError("analysis produced no frames")

    beats = []
    if punch_in.ENABLED:
        beats = punch_in.emphasis_times(input_video, len(xs) / fps)
        if beats:
            print(f"   🔍 Punch-in on {len(beats)} beat(s)")

    crop_w, crop_h = cameraman.crop_width, cameraman.crop_height

    for start_f, (held, centres) in alternates.items():
        end_f = scene_boundaries[split_scene_of[start_f]][1]
        end_f = min(end_f, len(xs))
        if end_f <= start_f:
            continue
        xs[start_f:end_f] = active_speaker.speaker_xs(
            held, centres, crop_w, orig_w, end_f - start_f, fps)

    if crop_overrides:
        xs, strategies = apply_crop_overrides(
            xs, strategies, scene_boundaries, crop_overrides, crop_w,
            orig_w, orig_h=orig_h, splits=splits)
        print(f"   ✋ Manual framing on {len(crop_overrides)} scene(s)")

    ranges = scene_frame_ranges(scene_boundaries, strategies, len(xs))
    if not ranges:
        raise RuntimeError("no usable scene ranges")
    workdir = tempfile.mkdtemp(prefix="reframe_v2_")
    segments = []
    bands = {}
    try:
        for idx, (start_f, end_f, strategy) in enumerate(ranges):
            seg_path = os.path.join(workdir, f"seg_{idx:03d}.mp4")
            ss = start_f / fps
            dur = (end_f - start_f) / fps

            if strategy == 'INSET':
                graph = camera_inset.inset_filtergraph(
                    orig_w, orig_h, out_w, out_h, insets.get(start_f, inset))
            elif strategy == 'SCREENCAST':
                sc_val = screencasts[start_f]
                if isinstance(sc_val, tuple) and len(sc_val) == 2 and isinstance(sc_val[0], tuple):
                    graph = screencast_layout.screencast_filtergraph(
                        orig_w, orig_h, out_w, out_h, *sc_val)
                else:
                    graph = screencast_layout.screencast_filtergraph(
                        orig_w, orig_h, out_w, out_h, sc_val)
            elif strategy == 'WIDE' and start_f in focus_crops:
                graph = screencast_layout.focus_filtergraph(
                    orig_w, orig_h, out_w, out_h, focus_crops[start_f])
                bands[start_f] = screencast_layout.content_band(
                    out_h, screencast_layout.crop_content_height(
                        orig_h, out_w, out_h, focus_crops[start_f][1]))
            elif strategy == 'WIDE':
                fg_h = full_width_content_height(orig_w, orig_h, out_w)
                graph = general_filtergraph(out_w, out_h, fg_h)
                bands[start_f] = screencast_layout.content_band(out_h, fg_h)
            elif strategy == 'SPLIT':
                left, right = splits[start_f]
                graph = split_layout.split_filtergraph(
                    orig_w, orig_h, out_w, out_h, left, right)
            elif strategy == 'GENERAL':
                graph = general_filtergraph(out_w, out_h,
                                            orig_w=orig_w, orig_h=orig_h)
            else:
                seg_xs = [x if x is not None else 0 for x in xs[start_f:end_f]]
                cmd_path = os.path.join(workdir, f"cmd_{idx:03d}.txt")
                if beats:
                    zooms = punch_in.zoom_curve(len(seg_xs), fps, beats,
                                                start_offset=ss)
                    boxes = punch_in.crop_boxes(seg_xs, zooms, crop_w, crop_h,
                                                orig_w, orig_h)
                    lines = punch_in.sendcmd_lines(boxes, fps)
                    first = boxes[0]
                    init = f"w={first[0]}:h={first[1]}:x={first[2]}:y={first[3]}"
                else:
                    lines = dedupe_sendcmd_lines(seg_xs, fps)
                    crop_y = max(0, (orig_h - crop_h) // 2)
                    init = f"w={crop_w}:h={crop_h}:x={seg_xs[0]}:y={crop_y}"
                with open(cmd_path, "w") as f:
                    f.write("\n".join(lines) + "\n")
                graph = (
                    f"[0:v]sendcmd=f='{escape_filter_value(cmd_path)}',"
                    f"crop@c={init},"
                    f"scale={out_w}:{out_h},setsar=1[v]"
                )

            _run_segment(input_video, ss, dur, graph, seg_path)
            segments.append(seg_path)

        list_path = os.path.join(workdir, "concat.txt")
        with open(list_path, "w") as f:
            f.write(concat_list_content(segments))

        _run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-i", input_video,
            "-map", "0:v:0", "-map", "1:a:0?",
            "-c:v", "copy", "-c:a", "copy", *METADATA_SCRUB,
            "-movflags", "+faststart",
            final_output_video,
        ])
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

    layout_ranges.write(final_output_video,
                        [(s / fps, e / fps, strategy, bands.get(s))
                         for s, e, strategy in ranges])
    print(f"   ✅ Clip saved to {final_output_video}")
    return True
