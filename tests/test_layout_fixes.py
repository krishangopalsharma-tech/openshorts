import active_speaker
import camera_inset
import frame_sampler
import hook_placement
import layout_picker
import layout_ranges
import reframe_v2
import screencast_layout
import split_layout


def test_layout_picker_none_still_enables_per_scene_split(monkeypatch):
    monkeypatch.setattr(split_layout, "ENABLED", False)
    monkeypatch.setattr(active_speaker, "ENABLED", False)
    touched = layout_picker.apply("none")
    assert "split_layout" in touched
    assert split_layout.ENABLED is True
    assert active_speaker.ENABLED is True


def test_camera_inset_face_box_preserves_forehead_headroom():
    # Small face near bottom-right corner on 1920x1080
    face_box = [1620, 820, 90, 90]
    ix, iy, iw, ih = camera_inset.inset_box(face_box, 1920, 1080, is_face=True)
    assert iw >= 270  # 3x face width
    assert iy < 820 - 25  # headroom above top of face


def test_screencast_fallback_ranges_converts_general_scenes():
    class FakePt:
        def __init__(self, sec):
            self._s = sec
        def get_seconds(self):
            return self._s

    scenes = [(FakePt(0.0), FakePt(5.0)), (FakePt(5.0), FakePt(12.0))]
    strategies = ["TRACK", "GENERAL"]
    ranges = screencast_layout.fallback_ranges(scenes, strategies)
    assert ranges == [(5.0, 12.0, "general", 0.75)]


def test_general_filtergraph_uses_quarter_res_blur():
    fg = reframe_v2.general_filtergraph(1080, 1920)
    assert "scale=270:480" in fg
    assert "gblur=sigma=3" in fg


def test_frame_sampler_reads_forward_without_redundant_seeks():
    class FakeCap:
        def __init__(self, n=20):
            self.n = n
            self.pos = 0
            self.seeks = 0
        def set(self, _prop, idx):
            self.seeks += 1
            self.pos = int(idx)
        def grab(self):
            if self.pos >= self.n:
                return False
            self.pos += 1
            return True
        def read(self):
            if self.pos >= self.n:
                return False, None
            val = f"frame_{self.pos}"
            self.pos += 1
            return True, val

    cap = FakeCap(20)
    got = list(frame_sampler.read_at(cap, [0, 3, 3, 7, 12]))
    assert got == ["frame_0", "frame_3", "frame_3", "frame_7", "frame_12"]
    assert cap.seeks == 0


def test_screencast_content_side_leaves_out_presenter():
    # Presenter on the right (1400..1850 on 1920 wide): content is on the left
    side = screencast_layout.content_side(1920, 1400, 1850)
    assert side is not None
    cx, cw = side
    assert cx == 0
    assert 1300 <= cw <= 1365
    assert cw % 2 == 0
    fg = screencast_layout.screencast_filtergraph(1920, 1080, 1080, 1920, (1625, 540), side)
    assert f"crop=w={cw}:h=1080:x=0:y=0" in fg


def test_wide_crop_and_action_window_centre():
    crop = screencast_layout.wide_crop(1920, 1080, 1080, 1920, centre=0.75)
    assert crop is not None
    x, w = crop
    assert w < 1920
    assert x > 200
    # Motion concentrated on the right side
    profile = [0.0] * 120 + [10.0] * 40
    centre = screencast_layout.action_window_centre(profile, 0.5)
    assert centre is not None and centre > 0.65


def test_layout_ranges_preserves_wide_content_band(tmp_path):
    clip = str(tmp_path / "clip.mp4")
    layout_ranges.write(clip, [(0.0, 5.0, "WIDE", (0.295, 0.705)), (5.0, 10.0, "SPLIT")])
    got = layout_ranges.read(clip)
    assert got[0]["content"] == [0.295, 0.705]
    remapped = layout_ranges.remap(got, [{"start": 1.0, "end": 4.0}])
    assert remapped[0]["content"] == [0.295, 0.705]


def test_hook_placement_avoids_split_seam_captions():
    ranges = [{"start": 0.0, "end": 8.0, "layout": "split"}]
    forbidden = hook_placement.forbidden_bands(ranges, duration=5.0)
    assert (0.42, 0.58) in forbidden
