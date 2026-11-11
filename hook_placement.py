"""Choose a hook banner Y-fraction (0..1) that avoids faces and captions."""

MIN_Y = 0.06
MAX_Y = 0.80
STEP = 0.02
FOREHEAD_EXTRA = 0.60
CAPTION_BAND = (0.70, 0.88)
SEAM_BAND = (0.42, 0.58)
DEFAULT_TOP_Y = 0.16


def head_box(face):
    """Expand a (xmin, ymin, w, h) face box 60% upward to cover forehead and hair."""
    x, y, w, h = face
    top = max(0.0, y - h * FOREHEAD_EXTRA)
    return (x, top, w, (y + h) - top)


def _overlap_1d(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def best_y(faces, band_height, forbidden=None):
    """Pick the Y-centre in [MIN_Y, MAX_Y] minimizing face overlap while avoiding forbidden bands."""
    forbidden = list(forbidden or [CAPTION_BAND])
    heads = [head_box(f) for f in (faces or [])]
    half = band_height / 2.0

    best_pos = DEFAULT_TOP_Y
    best_cost = float("inf")
    y = MIN_Y + half
    while y <= MAX_Y - half + 1e-6:
        b0, b1 = y - half, y + half
        if any(_overlap_1d(b0, b1, f0, f1) > 0 for f0, f1 in forbidden):
            y += STEP
            continue
        face_cost = sum(_overlap_1d(b0, b1, hy, hy + hh) * hw for _, hy, hw, hh in heads)
        # Mild preference for upper-third placement when face overlap is tied (0)
        drift_cost = abs(y - DEFAULT_TOP_Y) * 0.01
        total = face_cost + drift_cost
        if total < best_cost:
            best_cost = total
            best_pos = round(y, 3)
        y += STEP
    return best_pos


def sample_faces(video_path, duration=5.0, samples=6):
    """Sample normalized (xmin, ymin, w, h) face boxes over the hook window."""
    try:
        import cv2
        import main as m
    except Exception:
        return []
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    end_f = min(total, max(1, int(float(duration or 5.0) * fps)))
    faces = []
    try:
        for i in range(samples):
            f_idx = int(i * end_f / max(1, samples))
            cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
            ok, frame = cap.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            if not w or not h:
                continue
            for cand in m.detect_face_candidates(frame):
                bx, by, bw, bh = cand["box"]
                faces.append((bx / float(w), by / float(h), bw / float(w), bh / float(h)))
    except Exception:
        pass
    finally:
        cap.release()
    return faces
