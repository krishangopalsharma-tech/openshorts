"""NVDEC hardware decoding with free-VRAM guard and byte-identical yuv420p repack."""

import json
import os
import subprocess
import threading
import time

ENABLED = os.environ.get("GPU_DECODE", "1").strip() != "0"
MIN_FREE_MB = int(os.environ.get("GPU_DECODE_MIN_FREE_MB", "2500") or 0)
HW_ARGS = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
DOWNLOAD = "hwdownload,format=nv12,format=yuv420p"
_CODECS = {"h264", "hevc"}
_lock = threading.Lock()
_probe_cache = {}


def _supported(info):
    if not info:
        return False
    if info.get("codec_name") not in _CODECS or info.get("pix_fmt") != "yuv420p":
        return False
    for sd in info.get("side_data_list") or []:
        if sd.get("rotation") not in (None, 0, "0"):
            return False
    return True


def _vram_ok():
    if MIN_FREE_MB <= 0:
        return True
    try:
        import torch
        if not torch.cuda.is_available():
            return False
        free_bytes, _ = torch.cuda.mem_get_info()
        return (free_bytes // (1024 * 1024)) >= MIN_FREE_MB
    except Exception:
        return False


def probe_stream(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.realpath(path), st.st_size, st.st_mtime_ns)
    with _lock:
        if key in _probe_cache:
            return _probe_cache[key]
    info = None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt:stream_side_data=rotation",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        streams = json.loads(out.stdout or "{}").get("streams") or []
        if streams:
            info = streams[0]
    except Exception:
        info = None
    with _lock:
        _probe_cache[key] = info
    return info


def can_decode_on_gpu(path):
    return ENABLED and _supported(probe_stream(path)) and _vram_ok()
