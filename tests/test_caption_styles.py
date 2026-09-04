"""The ClipForge caption port: preset/override merging in caption_styles and
the ASS the styled generator writes (subtitles.generate_ass_styled)."""
import os

import caption_styles as cs
import subtitles


def _w(text, start, end):
    return {"word": text, "start": start, "end": end}


TRANSCRIPT = {
    "language": "en",
    "segments": [{
        "start": 10.0, "end": 13.0, "text": "hello world this is it",
        "words": [_w(" Hello", 10.2, 10.5), _w(" world", 10.6, 11.0),
                  _w(" this", 11.1, 11.3), _w(" is", 11.3, 11.5), _w(" it", 11.6, 12.2)],
    }],
}


def _ass(tmp_path, preset, overrides=None, **kw):
    out = tmp_path / "t.ass"
    ok = subtitles.generate_ass_styled(TRANSCRIPT, 10.0, 12.5, str(out), preset=preset,
                                       overrides=overrides, **kw)
    text = out.read_text(encoding="utf-8-sig") if ok else ""
    style = next((l for l in text.splitlines() if l.startswith("Style:")), "")
    events = [l for l in text.splitlines() if l.startswith("Dialogue:")]
    return ok, style, events


class TestOverrides:
    def test_unknown_keys_and_bad_values_are_dropped(self):
        ov = cs.normalize_overrides({"pos_x": 150, "glow_color": "zzz", "font_scale": "1.2",
                                     "evil": 1, "animation": "spin", "max_lines": 7})
        assert ov == {"pos_x": 100.0, "font_scale": 1.2, "max_lines": 2}

    def test_colors_are_normalised_to_hash_upper(self):
        assert cs.normalize_overrides({"primary_color": "ffd400"})["primary_color"] == "#FFD400"

    def test_font_family_cannot_break_the_style_line(self):
        ov = cs.normalize_overrides({"font_family": "Ant,on{}\\x"})
        assert ov["font_family"] == "Antonx"

    def test_karaoke_animation_folds_into_the_flag(self):
        cfg = cs.merge("hormozi_green", {"animation": "karaoke"})
        assert cfg["karaoke"] is True and cfg["animation"] == "none"

    def test_timed_animation_turns_karaoke_off(self):
        cfg = cs.merge("karaoke_yellow", {"animation": "highlight"})
        assert cfg["karaoke"] is False and cfg["animation"] == "highlight"

    def test_unknown_preset_falls_back_to_default(self):
        assert cs.merge("nope")["font_size"] == cs.STYLE_PRESETS[cs.DEFAULT_PRESET]["font_size"]
        assert not cs.is_known_preset("nope") and cs.is_known_preset("beast_red")

    def test_api_projection_carries_ids(self):
        presets = cs.presets_for_api()
        assert len(presets) == 19
        assert {p["id"] for p in presets} == set(cs.STYLE_PRESETS)
        assert all("label" in p for p in presets)


class TestFonts:
    def test_bundled_fonts_are_listed_by_embedded_family(self):
        families = {f["family"] for f in cs.list_fonts()}
        # ClipForge's trending set plus the fonts OpenShorts already shipped.
        for name in ("Anton", "Montserrat", "Poppins", "Bebas Neue", "Oswald",
                     "Permanent Marker", "Noto Sans Devanagari"):
            assert name in families, name
        assert cs.font_file_for("montserrat") == "Montserrat.ttf"
        assert all(os.path.exists(os.path.join(cs.FONTS_DIR, f["file"])) for f in cs.list_fonts())

    def test_one_entry_per_family(self):
        # Roboto-Regular + Roboto-Bold are one choice in the UI.
        assert sum(1 for f in cs.list_fonts() if f["family"] == "Roboto") == 1


class TestStyledAss:
    def test_playres_is_the_real_frame_and_sizes_scale_with_height(self, tmp_path):
        _, tall, _ = _ass(tmp_path, "bold_white", video_w=1080, video_h=1920)
        _, short, _ = _ass(tmp_path, "bold_white", video_w=1920, video_h=1080)
        assert int(tall.split(",")[2]) == 96
        assert int(short.split(",")[2]) == 54  # 96 * 1080/1920
        text = (tmp_path / "t.ass").read_text(encoding="utf-8-sig")
        assert "PlayResX: 1920" in text and "PlayResY: 1080" in text

    def test_karaoke_swaps_primary_and_secondary(self, tmp_path):
        _, style, events = _ass(tmp_path, "karaoke_yellow")
        fields = style.split(",")
        assert fields[3] == "&H0000E6FF"   # highlight in PrimaryColour
        assert fields[4] == "&H00FFFFFF"   # base in SecondaryColour
        assert "\\k" in events[0]

    def test_highlight_recolours_the_spoken_word(self, tmp_path):
        _, _, events = _ass(tmp_path, "hormozi_green")
        assert "\\t(0,0,\\1c&H006BE327)" in events[0]
        assert "HELLO" in events[0]  # preset is uppercase

    def test_one_word_makes_one_event_per_word(self, tmp_path):
        _, _, events = _ass(tmp_path, "one_word_punch")
        assert len(events) == 5
        assert all("\\fad(60,0)" in e for e in events)

    def test_word_reveal_and_glow_layer(self, tmp_path):
        _, _, events = _ass(tmp_path, "word_reveal", {"glow_enabled": True, "glow_color": "#22D3EE"})
        # Glow copy on layer 0 behind the sharp text on layer 1.
        assert events[0].startswith("Dialogue: 0,") and "\\blur" in events[0]
        assert events[1].startswith("Dialogue: 1,") and "\\alpha&HFF&" in events[1]

    def test_box_mode_uses_border_style_3(self, tmp_path):
        _, style, _ = _ass(tmp_path, "boxed_tiktok", {"background_opacity": 50})
        fields = style.split(",")
        assert fields[15] == "3"
        assert fields[5].startswith("&H80")  # 50% -> alpha 0x80 in the box slot

    def test_seam_anchor_applies_unless_pinned(self, tmp_path):
        _, _, events = _ass(tmp_path, "bold_white", split_ranges=[(0.0, 1.0)])
        assert "{\\an5}" in events[0]
        _, _, pinned = _ass(tmp_path, "bold_white", {"pos_x": 50, "pos_y": 30, "rotation": -4},
                            split_ranges=[(0.0, 1.0)])
        assert "\\an5\\pos(540,576)" in pinned[0] and "\\frz-4" in pinned[0]
        assert pinned[0].count("\\an5") == 1

    def test_offsets_nudge_the_anchor_and_keep_its_alignment(self, tmp_path):
        # bottom caption, 10% higher and 5% right: an2 anchor stays, position moves.
        _, _, ev = _ass(tmp_path, "bold_white", {"offset_y": -10, "offset_x": 5})
        # bottom margin is 8% of 1920 = 154 -> y = 1920-154-192 = 1574; x = 540+54
        assert "\\an2\\pos(594,1574)" in ev[0]
        _, _, centred = _ass(tmp_path, "hormozi_green", {"offset_y": 12})
        assert "\\an5\\pos(540,1190)" in centred[0]          # 960 + 230
        _, _, top = _ass(tmp_path, "bold_white", {"position": "top", "offset_y": 4})
        assert "\\an8\\pos(540,231)" in top[0]               # 154 + 77

    def test_offsets_apply_on_top_of_a_pin_and_beat_the_seam(self, tmp_path):
        _, _, ev = _ass(tmp_path, "bold_white", {"pos_x": 50, "pos_y": 30, "offset_x": -10},
                        split_ranges=[(0.0, 1.0)])
        assert "\\an5\\pos(432,576)" in ev[0]
        assert ev[0].count("\\an5") == 1                        # no extra seam anchor
        assert cs.normalize_overrides({"offset_y": 80})["offset_y"] == 50

    def test_no_words_in_window_returns_false(self, tmp_path):
        out = tmp_path / "empty.ass"
        assert subtitles.generate_ass_styled(TRANSCRIPT, 100.0, 110.0, str(out), preset="minimal") is False
        assert not out.exists()

    def test_lines_wrap_by_max_chars_and_max_lines(self, tmp_path):
        _, _, events = _ass(tmp_path, "bold_white", {"max_chars": 8, "max_lines": 1, "uppercase": False})
        # "Hello world this is it" cannot fit 8 chars a line -> several events,
        # none of them with a line break.
        assert len(events) >= 3 and all("\\N" not in e for e in events)
        _, _, two = _ass(tmp_path, "bold_white", {"max_chars": 8, "max_lines": 2})
        assert any("\\N" in e for e in two)
