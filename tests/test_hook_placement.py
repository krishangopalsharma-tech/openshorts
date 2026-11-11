import hook_placement
import hooks


def test_head_box_expands_forehead_upward():
    # Normalized face box (xmin, ymin, w, h)
    hx, hy, hw, hh = hook_placement.head_box((0.30, 0.20, 0.20, 0.20))
    assert hy < 0.20
    assert abs(hy - 0.08) < 1e-4  # 60% of 0.20 = 0.12 upward


def test_best_y_avoids_top_face_and_bottom_captions():
    faces = [(0.30, 0.12, 0.35, 0.22)]
    y = hook_placement.best_y(faces, band_height=0.08, forbidden=[(0.70, 0.88)])
    # Should land below the top face+headroom (~0.34) and above captions (0.70)
    assert 0.35 <= y <= 0.62


def test_hooks_pill_style_renders_rgba_banner():
    img = hooks._render_banner_image("THIS CHANGED EVERYTHING", 1080, 1920, position="top", style="pill")
    assert img.mode == "RGBA"
    assert img.size == (1080, 1920)
