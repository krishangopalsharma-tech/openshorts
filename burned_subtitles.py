"""Detect and crop off burned-in source subtitles at the bottom of a clip."""

import os
import shutil
import subprocess
import ffmpeg_utils

MIN_YMIN = 0.70
MAX_YMIN = 0.96
MIN_FRAME_FRACTION = 0.40


def clean_ymin(value):
    """Validate that a subtitle-band top edge sits in [0.70, 0.96]."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if MIN_YMIN <= v <= MAX_YMIN:
        return round(v, 3)
    return None


def decide(ymins, total_frames):
    """Return the safest (lowest) ymin when at least 40% of sampled frames show burned captions."""
    valid = [clean_ymin(y) for y in (ymins or [])]
    valid = [y for y in valid if y is not None]
    if not total_frames or len(valid) < max(2, MIN_FRAME_FRACTION * total_frames):
        return None
    return min(valid)


def crop_clip(video_path, ymin=None):
    """Crop the bottom (1 - ymin) strip off ``video_path`` in place.
    Preserves top-left (0, 0) so existing pixel face coordinates remain valid.
    """
    ymin = clean_ymin(ymin)
    if ymin is None or not os.path.exists(video_path):
        return False
    tmp_path = video_path + ".subcrop.mp4"
    vf = f"crop=iw:trunc(ih*{ymin:.4f}/2)*2:0:0"
    cmd = [
        "ffmpeg", "-y",
        *ffmpeg_utils.thread_args(),
        "-i", video_path,
        "-vf", vf,
        *ffmpeg_utils.video_encode_args(tier=ffmpeg_utils.INTERMEDIATE),
        "-c:a", "copy",
        tmp_path,
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if res.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
        shutil.move(tmp_path, video_path)
        return True
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    return False
