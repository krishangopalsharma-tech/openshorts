import layout_picker
import camera_inset
import reframe_v2
import screencast_layout
import split_layout
import active_speaker


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
