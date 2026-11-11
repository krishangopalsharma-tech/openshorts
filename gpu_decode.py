"""Decode on the card (NVDEC), keep every pixel the CPU decoder gives.

A job decodes each clip several times: scene detection, the face classifier,
the split detector, the camera trajectory, every reframe segment and the
caption/hook burn. On the CPU those decodes were the largest share of a job's
CPU time while the card's decoder sat idle.

H.264/HEVC decoding is exact, so NVDEC returns the same frames as libavcodec.
What changes is the pixel format they arrive in (nv12 instead of yuv420p),
and swscale converts nv12 by another path: an nv12 frame turned straight
into BGR differs from the CPU decode. ``DOWNLOAD`` therefore brings each frame
back as nv12 and repacks it to yuv420p (a lossless copy of the same samples)
before any other filter, and from there the graph is the one the CPU path
runs: frames, samples and encodes come out byte-identical.
"""
import json
import os
import subprocess
import threading
import time

ENABLED = os.environ.get("GPU_DECODE", "1").strip() != "0"
RENDER = ENABLED and os.environ.get("GPU_DECODE_RENDER", "1").strip() != "0"
MIN_FREE_MB = int(os.environ.get("GPU_DECODE_MIN_FREE_MB", "2500") or 0)

HW_ARGS = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
DOWNLOAD = "hwdownload,format=nv12,format=yuv420p"
_CODECS = {"h264", "hevc"}

_lock = threading.Lock()
_probe_cache = {}
_free = {"t": 0.0, "mb": None}
STATS = {"gpu": 0, "fallback": 0, "skipped": 0}


def _count(key):
    with _lock:
        STATS[key] += 1


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
            capture_output=True, text=True, timeout=30)
        streams = json.loads(out.stdout or "{}").get("streams") or []
        if streams:
            info = streams[0]
    except Exception:
        info = None
    with _lock:
        _probe_cache[key] = info
    return info


_probe = probe_stream


def _supported(info):
    if not info:
        return False
    if info.get("codec_name") not in _CODECS or info.get("pix_fmt") != "yuv420p":
        return False
    for side in info.get("side_data_list") or []:
        if int(float(side.get("rotation") or 0)) % 360:
            return False
    return True


def _free_mb():
    """Free card memory in MB, refreshed at most every 2 s (None: unknown)."""
    now = time.time()
    with _lock:
        if now - _free["t"] < 2:
            return _free["mb"]
    mb = None
    try:
        import torch
        if torch.cuda.is_available():
            free_bytes, _ = torch.cuda.mem_get_info()
            mb = int(free_bytes // (1024 * 1024))
    except Exception:
        mb = None
    if mb is None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                mb = int(out.stdout.split()[0])
        except Exception:
            mb = None
    with _lock:
        _free.update(t=now, mb=mb)
    return mb


def _vram_ok():
    if MIN_FREE_MB <= 0:
        return True
    free = _free_mb()
    return free is not None and free >= MIN_FREE_MB


def can_decode_on_gpu(path):
    return ENABLED and _supported(probe_stream(path)) and _vram_ok()


def usable(path, render=False):
    """True when ``path`` should be decoded on the card right now."""
    if not (RENDER if render else ENABLED):
        return False
    if not _supported(probe_stream(path)):
        return False
    if not _vram_ok():
        _count("skipped")
        return False
    return True


def input_args(on_gpu):
    """Options to put right before the video's ``-i``."""
    return list(HW_ARGS) if on_gpu else []


def vf(chain, on_gpu):
    """A -vf chain for the video input, preceded by the download when on the card."""
    if not on_gpu:
        return chain
    return f"{DOWNLOAD},{chain}" if chain else DOWNLOAD


def graph(filter_complex, on_gpu, label="[0:v]"):
    """A -filter_complex graph whose video input ``label`` is read from the card."""
    if not on_gpu:
        return filter_complex
    return f"{label}{DOWNLOAD}[gpudec];" + filter_complex.replace(label, "[gpudec]", 1)


def graph_ok(filter_complex, label="[0:v]"):
    return filter_complex.count(label) == 1


def note(on_gpu, failed=False):
    if failed:
        _count("fallback")
    elif on_gpu:
        _count("gpu")


def run(build, runner, ok=lambda result: result.returncode == 0, path=None, render=True):
    """Run ``runner(build(on_gpu))``; if the card was used and the result is not
    ``ok``, run it again on the CPU. Returns the last result."""
    on_gpu = usable(path, render=render) if path else False
    cmd = build(on_gpu)
    if on_gpu and cmd == build(False):
        on_gpu = False
    result = runner(cmd)
    if on_gpu and not ok(result):
        note(True, failed=True)
        print("   ⚠️ GPU decode failed, decoding on the CPU")
        return runner(build(False))
    note(on_gpu)
    return result


def on_card(cmd, video_path):
    """``cmd`` with ``video_path`` decoded on the card."""
    try:
        first = cmd.index("-i")
    except ValueError:
        return cmd
    if cmd[first + 1] != video_path:
        return cmd
    out = list(cmd)
    if "-filter_complex" in out:
        k = out.index("-filter_complex") + 1
        if not graph_ok(out[k]):
            return cmd
        out[k] = graph(out[k], True)
    elif "-vf" in out and out.count("-vf") == 1:
        k = out.index("-vf") + 1
        out[k] = vf(out[k], True)
    else:
        return cmd
    return out[:first] + HW_ARGS + out[first:]


_decoded = {}


def _file_key(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.realpath(path), st.st_size, st.st_mtime_ns)


def remember_count(path, n):
    key = _file_key(path)
    if key:
        with _lock:
            _decoded[key] = n


def any_of(terms):
    """ffmpeg expression true when any of ``terms`` is, as a balanced tree of sums."""
    terms = list(terms)
    if not terms:
        return "0"
    while len(terms) > 1:
        terms = [f"({terms[k]}+{terms[k + 1]})" if k + 1 < len(terms) else terms[k]
                 for k in range(0, len(terms), 2)]
    return terms[0]


def one_frame_per_decoded_frame(stderr):
    text = (stderr or b"").decode(errors="replace")
    return "frame=" in text and "dup=" not in text and "drop=" not in text


def decoded_frames(path):
    key = _file_key(path)
    if not key:
        return None
    with _lock:
        return _decoded.get(key)
