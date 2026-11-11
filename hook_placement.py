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


def forbidden_bands(ranges=None, duration=5.0, band_height=0.08):
    """Caption and WIDE content bands to avoid during the hook window [0, duration]."""
    window_end = float(duration or 5.0)
    bands = set()
    covered = 0.0
    content = []
    for r in ranges or []:
        try:
            s = max(0.0, float(r["start"]))
            e = min(window_end, float(r["end"]))
            layout = str(r.get("layout", "")).lower()
        except (KeyError, TypeError, ValueError):
            continue
        if e <= s:
            continue
        covered += e - s
        bands.add(SEAM_BAND if layout == "split" else CAPTION_BAND)
        if "content" in r and isinstance(r["content"], (list, tuple)) and len(r["content"]) == 2:
            try:
                ct, cb = float(r["content"][0]), float(r["content"][1])
                if 0.0 <= ct < cb <= 1.0:
                    content.append((ct, cb))
            except (TypeError, ValueError):
                pass
    if covered < window_end - 1e-3 or not bands:
        bands.add(CAPTION_BAND)
    base = sorted(bands)
    if content:
        with_content = sorted(set(base) | set(content))
        # Only enforce WIDE content bands if at least one candidate spot still fits
        half = band_height / 2.0
        y = MIN_Y + half
        fits = False
        while y <= MAX_Y - half + 1e-6:
            if not any(_overlap_1d(y - half, y + half, f0, f1) > 0 for f0, f1 in with_content):
                fits = True
                break
            y += STEP
        if fits:
            return with_content
    return base


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
        import frame_sampler
        import main as m
    except Exception:
        return []
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    end_f = min(total, max(1, int(float(duration or 5.0) * fps)))
    indices = sorted({int(i * end_f / max(1, samples)) for i in range(samples)})
    faces = []
    try:
        for frame in frame_sampler.read_at(cap, indices):
            if frame is None:
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
