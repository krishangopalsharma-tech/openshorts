"""SCREENCAST layout: full-width content on top, the speaker underneath."""
import json
import os
import time

import numpy as np

ENABLED = os.environ.get("SCREENCAST_LAYOUT", "0") == "1"

MIN_WIDTH_FRACTION = 0.5
STACK_MAX_WIDTH_FRACTION = 0.85
MIN_OVERLAP_SECONDS = 0.25
MIN_FACE_WIDTH = 0.05

# Content columns beside the presenter (non-duplicating crop)
MIN_CONTENT_SIDE = 0.3
PERSON_GAP = 0.02
MAX_CONTENT_BAND = 0.55

# Standing presenter in a filmed room (whiteboard/TV) guard
FILMED_PERSON_HEIGHT = 0.6
PERSON_BOX_SHARE = 0.4
BODY_FACE_WIDTHS = 3.0

FOCUS_MAX_WIDTH = 0.9
FOCUS_PADDING = 0.04
FOCUS_MAX_HEIGHT_RATIO = 0.6

WIDE_CONTENT_HEIGHT_RATIO = float(
    os.environ.get("WIDE_CONTENT_HEIGHT_RATIO", "0.41"))
WIDE_MAX_ZOOM = 1.4
WIDE_MIN_ZOOM = 1.05
WIDE_ACTION_MIN_GAIN = 0.1
WIDE_ACTION_TIE = 0.005
WIDE_ACTION_SAMPLES = 8
WIDE_ACTION_WIDTH = 160
WIDE_ACTION_NOISE = 16


def content_bands(orig_w, orig_h, out_w, out_h):
    """(content_height, speaker_height) for the stacked screencast frame."""
    content_h = int(round(out_w * orig_h / float(orig_w)))
    content_h -= content_h % 2
    content_h = max(2, min(content_h, out_h - 2))
    speaker_h = out_h - content_h
    return content_h, speaker_h


def speaker_crop(orig_w, orig_h, out_w, speaker_h, face_centre):
    """Crop box (w, h, x, y) for the speaker band, framed on the face."""
    aspect = out_w / float(speaker_h)

    crop_h = orig_h
    crop_w = int(round(crop_h * aspect))
    if crop_w > orig_w:
        crop_w = orig_w
        crop_h = int(round(crop_w / aspect))

    crop_w -= crop_w % 2
    crop_h -= crop_h % 2

    cx, cy = face_centre
    x = int(round(cx - crop_w / 2.0))
    x = max(0, min(x, orig_w - crop_w))
    y = int(round(cy - crop_h * 0.42))
    y = max(0, min(y, orig_h - crop_h))

    return crop_w, crop_h, x - (x % 2), y - (y % 2)


def content_side(frame_w, person_left, person_right):
    """(x, w) of the columns beside the presenter, or None when there are none."""
    gap = frame_w * PERSON_GAP
    centre = (person_left + person_right) / 2.0
    if centre >= frame_w / 2.0:
        x, w = 0, int(person_left - gap)
    else:
        x = int(person_right + gap)
        w = frame_w - x
    if w < frame_w * MIN_CONTENT_SIDE:
        return None
    x += x % 2
    w -= w % 2
    return x, w


def screencast_filtergraph(orig_w, orig_h, out_w, out_h, face_centre, content=None):
    """Full-width (or content_side) content above, face-framed speaker below."""
    if content is not None:
        from ffmpeg_utils import blurred_backdrop

        cx_, cw_ = content
        max_h = int(out_h * MAX_CONTENT_BAND)
        scale = min(out_w / float(cw_), max_h / float(orig_h))
        sw = min(out_w, int(round(cw_ * scale)))
        sh = min(max_h, int(round(orig_h * scale)))
        sw -= sw % 2
        sh -= sh % 2
        speaker_h = out_h - sh
        cw, ch, cx, cy = speaker_crop(orig_w, orig_h, out_w, speaker_h, face_centre)

        return (
            f"[0:v]split=3[bga][ca][sa];"
            f"[bga]{blurred_backdrop(out_w, out_h, 14)}[bg];"
            f"[ca]crop=w={cw_}:h={orig_h}:x={cx_}:y=0,scale={sw}:{sh}[content];"
            f"[sa]crop=w={cw}:h={ch}:x={cx}:y={cy},scale={out_w}:{speaker_h}[speaker];"
            f"[bg][content]overlay=x={(out_w - sw) // 2}:y=0[top];"
            f"[top][speaker]overlay=x=0:y={sh},setsar=1[v]"
        )

    content_h, speaker_h = content_bands(orig_w, orig_h, out_w, out_h)
    cw, ch, cx, cy = speaker_crop(orig_w, orig_h, out_w, speaker_h, face_centre)

    return (
        f"[0:v]split=2[ca][sa];"
        f"[ca]scale={out_w}:{content_h}[content];"
        f"[sa]crop=w={cw}:h={ch}:x={cx}:y={cy},scale={out_w}:{speaker_h}[speaker];"
        f"[content][speaker]vstack=inputs=2,"
        f"pad={out_w}:{out_h}:0:0,setsar=1[v]"
    )


def detect_faces_full_res(frame):
    """Face boxes from the UNSCALED frame, in original coordinates."""
    import cv2
    import main as m

    h, w, _ = frame.shape
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    with m.DETECT_LOCK:
        results = m.face_detection.process(rgb)
    if not results.detections:
        return []

    out = []
    for detection in results.detections:
        b = detection.location_data.relative_bounding_box
        box = [int(b.xmin * w), int(b.ymin * h),
               int(b.width * w), int(b.height * h)]
        out.append({'box': box, 'score': box[2] * box[3]})
    return out


def _face_centre(candidates, frame_w):
    """Centre of the biggest usable face in a frame, or None."""
    big = [c for c in candidates if c['box'][2] >= MIN_FACE_WIDTH * frame_w]
    if not big:
        return None
    box = max(big, key=lambda c: c['score'])['box']
    return box[0] + box[2] / 2.0, box[1] + box[3] / 2.0


def overlapping_range(scene_start, scene_end, ranges):
    """(width_fraction, focus) of the widest range the scene overlaps."""
    widest, focus = 0.0, None
    for r in ranges:
        start, end = r[0], r[1]
        width = r[3] if len(r) > 3 else 1.0
        if min(scene_end, end) - max(scene_start, start) > MIN_OVERLAP_SECONDS:
            if width > widest:
                widest = width
                focus = r[4] if len(r) > 4 else None
    return widest, focus


def overlapping_width(scene_start, scene_end, ranges):
    """Widest content the scene overlaps, as a fraction of frame width."""
    return overlapping_range(scene_start, scene_end, ranges)[0]


def presenter_cam(scene_start, scene_end, ranges):
    """Whether the shot check saw a live webcam window over this scene's screen."""
    for r in ranges:
        if len(r) > 5 and r[5] and \
                min(scene_end, r[1]) - max(scene_start, r[0]) > MIN_OVERLAP_SECONDS:
            return True
    return False


def detect_content_ranges(video_path, video_duration):
    """Time ranges where on-screen content spans most of the frame width."""
    if not ENABLED:
        return []
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return []

    from google import genai
    from google.genai import types as genai_types
    import gemini_worker

    model_name = os.environ.get("GEMINI_MODEL") or 'gemini-3.1-flash-lite'
    print("🔎 Checking for full-width on-screen content…")
    client = None
    file_upload = None
    try:
        client = genai.Client(api_key=api_key)
        file_upload = client.files.upload(file=video_path)
        deadline = time.time() + 180
        while True:
            info = client.files.get(name=file_upload.name)
            state = str(getattr(getattr(info, "state", info), "name", "")).upper()
            if state == "ACTIVE":
                break
            if state == "FAILED" or time.time() > deadline:
                print("   ⚠️ Upload not usable — keeping face-only routing.")
                return []
            time.sleep(2)

        response = client.models.generate_content(
            model=model_name,
            contents=[file_upload,
                      gemini_worker.WIDE_CONTENT_PROMPT_TEMPLATE.format(
                          video_duration=video_duration)],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=gemini_worker.WideContentResponse,
            ))
        gemini_worker.raise_if_blocked(response)
        raw = (json.loads(response.text) or {}).get("ranges") or []
    except Exception as e:
        print(f"   ⚠️ On-screen check failed ({e}) — keeping face-only routing.")
        return []
    finally:
        if client is not None and file_upload is not None:
            try:
                client.files.delete(name=file_upload.name)
            except Exception as e:
                print(f"   ⚠️ Could not delete the uploaded source from Gemini "
                      f"Files ({e}) — it expires there in 48 h.")

    ranges = []
    for r in raw:
        try:
            s = max(0.0, float(r.get("start", 0)))
            e = min(float(video_duration), float(r.get("end", 0)))
            width = float(r.get("width_fraction", 0))
        except (TypeError, ValueError):
            continue
        if e - s >= 0.5 and width >= MIN_WIDTH_FRACTION:
            ranges.append((s, e, str(r.get("what", ""))[:40], width))
    ranges.sort()

    if ranges:
        print("   📊 " + ", ".join(
            f"{w}@{s:.0f}-{e:.0f}s ({frac:.0%} wide)"
            for s, e, w, frac in ranges[:5]))
    else:
        print("   ✅ No full-width content — routing unchanged.")
    return ranges


def fallback_ranges(scenes, strategies, fps=None):
    """When GEMINI_API_KEY is absent or returns nothing, treat GENERAL scenes as
    0.75-wide content so explicit screencast mode still routes them to SCREENCAST/WIDE.
    """
    out = []
    for i, (start, end) in enumerate(scenes):
        if i < len(strategies) and strategies[i] == "GENERAL":
            if hasattr(start, "get_seconds"):
                s, e = start.get_seconds(), end.get_seconds()
            else:
                s, e = start.get_frames() / float(fps or 30.0), end.get_frames() / float(fps or 30.0)
            if e - s >= MIN_OVERLAP_SECONDS:
                out.append((s, e, "general", 0.75))
    return out


def screen_ranges(video_path, video_duration, scenes=None, strategies=None):
    """Gemini content ranges when available, falling back to GENERAL scenes."""
    if not ENABLED:
        return []
    ranges = detect_content_ranges(video_path, video_duration)
    if not ranges and scenes and strategies:
        ranges = fallback_ranges(scenes, strategies)
    return ranges


def focus_crop(orig_w, orig_h, out_w, out_h, focus):
    """(x, w) of the source columns to show for a screen shot, or None."""
    if not focus:
        return None
    left = max(0.0, focus[0] - FOCUS_PADDING)
    right = min(1.0, focus[1] + FOCUS_PADDING)
    if right <= left or right - left >= FOCUS_MAX_WIDTH:
        return None
    min_w = out_w * orig_h / (FOCUS_MAX_HEIGHT_RATIO * out_h)
    crop_w = min(orig_w, int(round(max((right - left) * orig_w, min_w))))
    crop_w -= crop_w % 2
    if crop_w >= orig_w * FOCUS_MAX_WIDTH:
        return None
    x = int(round((left + right) / 2.0 * orig_w - crop_w / 2.0))
    x = max(0, min(x, orig_w - crop_w))
    return x - (x % 2), crop_w


def crop_content_height(orig_h, out_w, out_h, crop_w):
    """Height (even) that ``crop_w`` source columns fill at the output width."""
    fg_h = min(out_h, int(round(out_w * orig_h / float(crop_w))))
    return fg_h - fg_h % 2


def content_band(out_h, fg_h):
    """(top, bottom) of a vertically centred foreground, as frame fractions."""
    top = (out_h - fg_h) / 2.0 / out_h
    return round(top, 3), round(1.0 - top, 3)


def focus_filtergraph(orig_w, orig_h, out_w, out_h, crop):
    """A screen shot cut down to its reading area, over a blurred backdrop."""
    from ffmpeg_utils import blurred_backdrop

    x, w = crop
    fg_h = crop_content_height(orig_h, out_w, out_h, w)
    return (
        f"[0:v]split=2[bga][fga];"
        f"[bga]{blurred_backdrop(out_w, out_h, 12)}[bg];"
        f"[fga]crop=w={w}:h={orig_h}:x={x}:y=0,scale={out_w}:{fg_h}[fg];"
        f"[bg][fg]overlay=x=0:y=(H-h)/2,setsar=1[v]"
    )


def wide_crop(orig_w, orig_h, out_w, out_h, centre=0.5):
    """(x, w) of the source columns a WIDE scene shows, or None for full width."""
    full_h = out_w * orig_h / float(orig_w)
    zoom = min(WIDE_MAX_ZOOM, WIDE_CONTENT_HEIGHT_RATIO * out_h / full_h)
    if zoom < WIDE_MIN_ZOOM:
        return None
    w = int(round(orig_w / zoom))
    w -= w % 2
    x = int(round(centre * orig_w - w / 2.0))
    x = max(0, min(x, orig_w - w))
    return x - (x % 2), w


def action_window_centre(profile, frac):
    """Centre (fraction of width) of the window of width ``frac`` holding the most motion."""
    profile = np.asarray(profile, dtype=np.float64)
    n = len(profile)
    total = float(profile.sum())
    if n == 0 or total <= 0 or frac >= 1.0:
        return None
    win = max(1, min(n, int(round(frac * n))))
    sums = np.convolve(profile, np.ones(win), mode="valid") / total
    centred = (n - win) // 2
    best = float(sums.max())
    if best - float(sums[centred]) < WIDE_ACTION_MIN_GAIN:
        return None
    near = np.flatnonzero(sums >= best - WIDE_ACTION_TIE)
    start = int(near[np.argmin(np.abs(near - centred))])
    return (start + win / 2.0) / n


def wide_action_centre(video_path, start_f, end_f, frac,
                       samples=WIDE_ACTION_SAMPLES):
    """Where the action is in a WIDE scene, for wide_crop's ``centre``."""
    import cv2

    cap = cv2.VideoCapture(video_path)
    try:
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        gap = max(1, int(round(fps * 0.1)))
        last = end_f - 1 - gap
        if total:
            last = min(last, total - 1 - gap)
        if last < start_f:
            return None
        profile = None
        for f_idx in np.linspace(start_f, last, samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(f_idx)))
            ok, a = cap.read()
            for _ in range(gap - 1):
                ok = ok and cap.grab()
            ok2, b = cap.read() if ok else (False, None)
            if not (ok and ok2):
                continue
            h = max(1, int(round(a.shape[0] * WIDE_ACTION_WIDTH / a.shape[1])))
            ga = cv2.cvtColor(cv2.resize(a, (WIDE_ACTION_WIDTH, h)), cv2.COLOR_BGR2GRAY)
            gb = cv2.cvtColor(cv2.resize(b, (WIDE_ACTION_WIDTH, h)), cv2.COLOR_BGR2GRAY)
            diff = cv2.absdiff(ga, gb) > WIDE_ACTION_NOISE
            col = diff.sum(axis=0).astype(np.float64)
            profile = col if profile is None else profile + col
        if profile is None:
            return None
        return action_window_centre(profile, frac)
    finally:
        cap.release()


def _filmed_person(cap, m, s_f, last_f, samples=3):
    """True when most samples show a person standing in the shot at full size."""
    import cv2

    frame_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0
    if not frame_h:
        return False
    hits = 0
    for f_idx in np.linspace(s_f, last_f, samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(f_idx)))
        ok, frame = cap.read()
        if not ok:
            continue
        person = m.detect_person_yolo(frame)
        if person and person[3] / PERSON_BOX_SHARE >= FILMED_PERSON_HEIGHT * frame_h:
            hits += 1
    return hits * 2 > samples


def detect_screencast_scenes(video_path, scenes, strategies, ranges, samples=6):
    """Route scenes that show wide on-screen content."""
    if not ENABLED or not ranges:
        return {}
    import cv2
    import main as m

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {}

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    found = {}

    try:
        for i, (start, end) in enumerate(scenes):
            s_f, e_f = start.get_frames(), end.get_frames()
            width, focus = overlapping_range(s_f / fps, e_f / fps, ranges)
            if not width:
                continue

            last_f = e_f - 1
            if total_frames:
                last_f = min(last_f, total_frames - 1)
            if last_f < s_f:
                continue

            if _filmed_person(cap, m, s_f, last_f):
                continue

            if width > STACK_MAX_WIDTH_FRACTION:
                found[i] = ('WIDE', focus)
                continue

            centres, extents = [], []
            for f_idx in np.linspace(s_f, last_f, samples):
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(f_idx)))
                ok, frame = cap.read()
                if not ok:
                    continue
                faces = detect_faces_full_res(frame)
                centre = _face_centre(faces, frame_w)
                person = m.detect_person_yolo(frame)
                if centre is None and person:
                    centre = (person[0] + person[2] / 2.0,
                              person[1] + person[3] / 2.0)
                if not centre:
                    continue
                centres.append(centre)
                if person:
                    extents.append((person[0], person[0] + person[2]))
                else:
                    face_w = max(c['box'][2] for c in faces)
                    half = face_w * BODY_FACE_WIDTHS / 2.0
                    extents.append((centre[0] - half, centre[0] + half))

            if len(centres) < samples / 2.0:
                found[i] = ('WIDE', focus)
                continue

            content = content_side(
                frame_w, float(np.median([e[0] for e in extents])),
                float(np.median([e[1] for e in extents])))
            if content is None:
                continue
            found[i] = ('SCREENCAST',
                        ((float(np.median([c[0] for c in centres])),
                          float(np.median([c[1] for c in centres]))), content))
    finally:
        cap.release()

    return found
