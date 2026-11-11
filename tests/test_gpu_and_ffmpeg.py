import ffmpeg_utils
import gpu_decode


def test_nvenc_uses_p3_and_supports_intermediate_tier(monkeypatch):
    monkeypatch.setattr(ffmpeg_utils, "_nvenc_available", True)
    args = ffmpeg_utils.video_encode_args()
    assert "p3" in args
    inter = ffmpeg_utils.video_encode_args(tier=ffmpeg_utils.INTERMEDIATE)
    assert "-cq" in inter


def test_thread_args_returns_bounded_threads():
    t_args = ffmpeg_utils.thread_args()
    assert "-threads" in t_args
    assert "-filter_threads" in t_args


def test_gpu_decode_rejects_10bit_or_rotated_streams():
    assert gpu_decode._supported({"codec_name": "h264", "pix_fmt": "yuv420p"}) is True
    assert gpu_decode._supported({"codec_name": "hevc", "pix_fmt": "yuv420p10le"}) is False
    assert gpu_decode._supported({"codec_name": "h264", "pix_fmt": "yuv420p", "side_data_list": [{"rotation": -90}]}) is False
