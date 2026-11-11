import burned_subtitles


def test_clean_ymin_validates_bounds():
    assert burned_subtitles.clean_ymin(None) is None
    assert burned_subtitles.clean_ymin(0.50) is None  # too high, would cut faces
    assert burned_subtitles.clean_ymin(0.98) is None  # too thin to matter
    assert burned_subtitles.clean_ymin(0.82) == 0.82


def test_decide_requires_quorum_of_frames():
    assert burned_subtitles.decide([0.82, 0.84, 0.83, 0.85, 0.81], total_frames=10) == 0.81
    assert burned_subtitles.decide([0.82, 0.84], total_frames=10) is None
