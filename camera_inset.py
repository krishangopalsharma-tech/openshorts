"""Find the webcam inset in a screen recording, and frame the two apart.

The case: one source, the whole screen (a game, a desktop, an editor) with the
person composited into a corner. It is how OBS records and how every stream VOD
looks, and it is the case screencast_layout gets wrong. There the speaker band
is a large crop taken AROUND the face, which on a full-screen source means the
band is mostly more screen: measured on an Excel walkthrough, the output showed
the same spreadsheet twice, once whole and once enlarged.

What this needs instead is the inset's own rectangle, so the two bands can hold
genuinely different things — screen above, person below.
"""
import os

from ffmpeg_utils import blurred_backdrop

CORNER_MARGIN = 0.20   # a subject this far from an edge (as a fraction of the
                       # frame) still counts as anchored to it

MAX_SUBJECT_HEIGHT = 0.35   # fraction of frame height
MIN_OFFSET = 0.18
INSET_PADDING = float(os.environ.get("INSET_PADDING", "1.45"))

MIN_INSET_HEIGHT = 0.10
MAX_INSET_HEIGHT = 0.38
MAX_CENTRE_SPREAD = 0.05

FACE_HEADROOM = 0.35
BODY_HEADROOM = 0.10
OVERLAY_FACE_WIDTHS = 3.0
INSET_ASPECT = 16 / 9.0


def nearest_corner(box, frame_w, frame_h):
    """Which corner the subject sits in: (horizontal, vertical) as strings."""
    cx = box[0] + box[2] / 2.0
    cy = box[1] + box[3] / 2.0
    return ("left" if cx < frame_w / 2 else "right",
            "top" if cy < frame_h / 2 else "bottom")


def is_cornered(box, frame_w, frame_h, margin=CORNER_MARGIN):
    """True when the subject looks like a webcam inset rather than the shot."""
    x, y, w, h = box
    if h > frame_h * MAX_SUBJECT_HEIGHT:
        return False

    cx = (x + w / 2.0) / frame_w
    off_centre = abs(cx - 0.5) >= MIN_OFFSET

    near_edge = (x <= frame_w * margin or (x + w) >= frame_w * (1 - margin)
                 or y <= frame_h * margin or (y + h) >= frame_h * (1 - margin))
    return off_centre and near_edge


def _place(x, y, w, h, new_w, new_h, frame_w, frame_h, horizontal, vertical):
    """Top-left corner of a new_w x new_h box grown around the detection."""
    if horizontal == "left":
        new_x = max(0, min(x - (new_w - w) / 2.0, frame_w - new_w))
        if x <= frame_w * CORNER_MARGIN:
            new_x = 0
    else:
        new_x = max(0, min(x + w + (new_w - w) / 2.0 - new_w, frame_w - new_w))
        if (x + w) >= frame_w * (1 - CORNER_MARGIN):
            new_x = frame_w - new_w

    grow = new_h - h
    if vertical == "top":
        new_y = max(0, min(y - grow * 0.6, frame_h - new_h))
        if y <= frame_h * CORNER_MARGIN:
            new_y = 0
    else:
        new_y = frame_h - new_h
        if (y + h) < frame_h * (1 - CORNER_MARGIN):
            new_y = max(0, min(y - grow * 0.6, frame_h - new_h))
    return new_x, new_y


def inset_box(box, frame_w, frame_h, padding=None, is_face=False, face=False):
    """Estimated inset rectangle (x, y, w, h) around a detected subject."""
    is_face = bool(is_face or face)
    if padding is None:
        padding = OVERLAY_FACE_WIDTHS if is_face else INSET_PADDING
    x, y, w, h = box
    horizontal, vertical = nearest_corner(box, frame_w, frame_h)

    new_w = min(frame_w, w * padding)
    new_h = new_w / INSET_ASPECT if is_face else max(h * padding, new_w / INSET_ASPECT)
    new_h = min(frame_h, new_h)
    top_limit = max(0.0, y - h * (FACE_HEADROOM if is_face else BODY_HEADROOM))

    new_x, new_y = _place(x, y, w, h, new_w, new_h, frame_w, frame_h,
                          horizontal, vertical)
    if new_y > top_limit:
        if new_y + new_h >= frame_h - 1:
            new_h = frame_h - top_limit
            new_w = min(frame_w, max(new_w, new_h * INSET_ASPECT))
            new_x, _ = _place(x, y, w, h, new_w, new_h, frame_w, frame_h,
                              horizontal, vertical)
        new_y = top_limit

    return (int(round(new_x)), int(round(new_y)),
            int(round(new_w)), int(round(new_h)))


def usable(box, frame_h, min_ratio=MIN_INSET_HEIGHT, max_ratio=MAX_INSET_HEIGHT):
    """Whether an inset of this size is worth building a layout around."""
    return min_ratio * frame_h <= box[3] <= max_ratio * frame_h


def stable_box(boxes, frame_w, frame_h, min_agree=3):
    """Median of the inset boxes that agree with each other, or None."""
    import numpy as np

    arr = np.array(boxes, dtype=float)
    centres_x = arr[:, 0] + arr[:, 2] / 2.0
    centres_y = arr[:, 1] + arr[:, 3] / 2.0
    mid_x, mid_y = np.median(centres_x), np.median(centres_y)
    tolerance = frame_w * MAX_CENTRE_SPREAD
    close = ((np.abs(centres_x - mid_x) <= tolerance)
             & (np.abs(centres_y - mid_y) <= tolerance))
    if close.sum() < max(min_agree, 0.6 * len(boxes)):
        return None

    arr = arr[close]
    median = tuple(int(v) for v in np.median(arr, axis=0))
    if not usable(median, frame_h):
        return None
    return median


def detect(video_path, samples=10):
    """Median inset rectangle across sampled frames, or None."""
    import cv2
    import main as m
    import screencast_layout

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if total <= 0 or not frame_w:
        cap.release()
        return None

    boxes = []
    try:
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i * total / samples))
            ok, frame = cap.read()
            if not ok:
                continue
            faces = screencast_layout.detect_faces_full_res(frame)
            if faces:
                box = max(faces, key=lambda c: c['score'])['box']
            else:
                box = m.detect_person_yolo(frame)
            if not box:
                continue
            if not is_cornered(box, frame_w, frame_h):
                continue
            boxes.append(inset_box(box, frame_w, frame_h, face=bool(faces)))
    finally:
        cap.release()

    if len(boxes) < max(3, samples // 3):
        return None

    return stable_box(boxes, frame_w, frame_h)


def overlay_box(box, frame_w, frame_h, widen=OVERLAY_FACE_WIDTHS):
    """A 16:9 box centred on a detected face (or upper body), clamped."""
    x, y, w, h = box
    new_w = min(frame_w, w * widen)
    new_h = min(frame_h, new_w / INSET_ASPECT)
    cx, cy = x + w / 2.0, y + h / 2.0
    new_x = max(0, min(cx - new_w / 2.0, frame_w - new_w))
    new_y = max(0, min(cy - new_h * 0.45, frame_h - new_h))
    return (int(round(new_x)), int(round(new_y)),
            int(round(new_w)), int(round(new_h)))


def detect_in_scene(video_path, start_f, end_f, samples=5):
    """A presenter window laid over the screen in this one scene, or None."""
    import cv2
    import numpy as np
    import main as m
    import screencast_layout

    if end_f - 1 < start_f:
        return None
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    boxes = []
    try:
        for f_idx in np.linspace(start_f, end_f - 1, samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(f_idx)))
            ok, frame = cap.read()
            if not ok:
                continue
            faces = screencast_layout.detect_faces_full_res(frame)
            if faces:
                box = max(faces, key=lambda c: c['score'])['box']
                widen = OVERLAY_FACE_WIDTHS
            else:
                box = m.detect_person_yolo(frame)
                widen = INSET_PADDING
            if not box or box[3] > frame_h * MAX_SUBJECT_HEIGHT:
                continue
            boxes.append(overlay_box(box, frame_w, frame_h, widen))
    finally:
        cap.release()
    need = samples // 2 + 1
    if len(boxes) < need:
        return None
    return stable_box(boxes, frame_w, frame_h, min_agree=need)


def _centre_inside(det, box, slack):
    cx = det[0] + det[2] / 2.0
    cy = det[1] + det[3] / 2.0
    x, y, w, h = box
    return (x - slack <= cx <= x + w + slack) and (y - slack <= cy <= y + h + slack)


def present_in_scene(video_path, box, start_f, end_f, samples=3):
    """Whether the webcam really sits in ``box`` during this scene."""
    import cv2
    import numpy as np
    import main as m
    import screencast_layout

    if end_f - 1 < start_f:
        return False
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return False
    slack = max(box[2], box[3]) * 0.1
    hits = 0
    try:
        for f_idx in np.linspace(start_f, end_f - 1, samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(f_idx)))
            ok, frame = cap.read()
            if not ok:
                continue
            dets = [c['box'] for c in screencast_layout.detect_faces_full_res(frame)]
            if not any(_centre_inside(d, box, slack) for d in dets):
                person = m.detect_person_yolo(frame)
                dets = [person] if person else []
            if any(_centre_inside(d, box, slack) for d in dets):
                hits += 1
    finally:
        cap.release()
    return hits * 2 > samples


MAX_CAMERA_RATIO = 0.40


def inset_filtergraph(orig_w, orig_h, out_w, out_h, box, camera_ratio=None):
    """Screen on top at full width, the webcam inset below."""
    box_w = max(2, box[2])
    box_h = max(2, box[3])

    if camera_ratio is not None:
        cam_h = int(out_h * camera_ratio)
    else:
        cam_h = int(round(out_w * box_h / float(box_w)))
    cam_h = min(cam_h, int(out_h * MAX_CAMERA_RATIO))
    cam_h -= cam_h % 2

    screen_h = int(round(out_w * orig_h / float(orig_w)))
    screen_h -= screen_h % 2
    screen_h = max(2, min(screen_h, out_h - cam_h - 2))

    target_aspect = out_w / float(cam_h)
    x, y, w, h = box
    if w / float(h) < target_aspect:
        want_w = min(orig_w, int(round(h * target_aspect)))
        x = int(round(x + w / 2.0 - want_w / 2.0))
        w = want_w
    else:
        want_h = min(orig_h, int(round(w / target_aspect)))
        y = int(round(y + h / 2.0 - want_h / 2.0))
        h = want_h
    x = max(0, min(x, orig_w - w))
    y = max(0, min(y, orig_h - h))

    w -= w % 2
    h -= h % 2
    x -= x % 2
    y -= y % 2

    filler_h = out_h - screen_h - cam_h

    return (
        f"[0:v]split=3[bga][sa][ca];"
        f"[bga]{blurred_backdrop(out_w, out_h, 14)}[bg];"
        f"[sa]scale={out_w}:{screen_h}[screen];"
        f"[ca]crop=w={w}:h={h}:x={x}:y={y},scale={out_w}:{cam_h}[cam];"
        f"[bg][screen]overlay=x=0:y={filler_h // 2}[withscreen];"
        f"[withscreen][cam]overlay=x=0:y={filler_h // 2 + screen_h},"
        f"setsar=1[v]"
    )
