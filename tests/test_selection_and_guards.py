import clip_selection
import jsonfile
import yt_clients


def test_dedupe_overlapping_drops_clips_sharing_25_percent():
    clips = [
        {"start": 10.0, "end": 50.0, "score": 92},
        {"start": 20.0, "end": 55.0, "score": 85},  # overlaps 30s of 40s (75%)
        {"start": 100.0, "end": 140.0, "score": 88},
    ]
    kept = clip_selection.dedupe_overlapping(clips)
    assert len(kept) == 2
    assert kept[0]["start"] == 10.0
    assert kept[1]["start"] == 100.0


def test_vision_segments_splits_long_silent_video():
    segs = clip_selection.vision_segments(7200.0, max_segment_seconds=2400.0)
    assert len(segs) == 3
    assert segs[0] == (0.0, 2400.0)
    assert segs[-1][1] == 7200.0


def test_youtube_non_video_reason_rejects_playlists_and_channels():
    assert yt_clients.youtube_non_video_reason("https://www.youtube.com/results?search_query=podcast") is not None
    assert yt_clients.youtube_non_video_reason("https://www.youtube.com/@SomeChannel") is not None
    assert yt_clients.youtube_non_video_reason("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None


def test_jsonfile_atomic_write_and_read(tmp_path):
    target = tmp_path / "clip_metadata.json"
    jsonfile.dump_atomic({"clips": [1, 2, 3]}, str(target))
    assert target.exists()
