"""cinematic.py grades, sharpening and LUTs: what normalize() accepts, the
order the filters land in, the LUT allowlist, and (when ffmpeg is on PATH)
that every preset chain actually parses and renders."""

import os
import shutil
import subprocess

import pytest

import cinematic


@pytest.fixture()
def lut_dir(tmp_path, monkeypatch):
    # A space and (on Windows) a drive colon in the path: both have to
    # survive filtergraph escaping.
    d = tmp_path / "lut dir"
    d.mkdir()
    with open(d / "Warm Test.cube", "w") as f:
        f.write("LUT_3D_SIZE 2\n")
        for b in (0, 1):
            for g in (0, 1):
                for r in (0, 1):
                    f.write(f"{min(1.0, r + 0.1)} {g} {b * 0.8}\n")
    (d / "notes.txt").write_text("not a lut")
    monkeypatch.setattr(cinematic, "LUT_DIR", str(d))
    return d


def test_every_grade_has_a_label():
    assert set(cinematic.GRADE_LABELS) == set(cinematic.COLOR_GRADES)
    assert set(cinematic.SHARPEN_LABELS) == set(cinematic.SHARPEN_PRESETS)


def test_normalize_rejects_unknown_names():
    e = cinematic.normalize({"color_grade": "nope", "sharpen": "max", "lut": "x"})
    assert (e["color_grade"], e["sharpen"], e["lut"]) == ("none", "none", "none")
    assert cinematic.is_noop(e)


def test_sharpen_alone_is_not_a_noop():
    e = cinematic.normalize({"sharpen": "crisp"})
    assert not cinematic.is_noop(e)
    graph = cinematic.build_filter_complex(e, 1080, 1920)
    assert "cas=strength=0.45" in graph


def test_filter_order_denoise_lut_grade_sharpen_then_grain(lut_dir):
    e = {"sharpen": "tv_remaster", "lut": "Warm Test", "color_grade": "film_print", "grain": True}
    graph = cinematic.build_filter_complex(e, 1080, 1920)
    order = [graph.index(s) for s in ("hqdn3d", "lut3d", "curves", "cas=", "noise=")]
    assert order == sorted(order)


def test_lut_listing_is_the_allowlist(lut_dir):
    assert cinematic.list_luts() == ["Warm Test"]
    assert cinematic.lut_path("notes") is None
    assert cinematic.lut_path("../Warm Test") is None
    assert cinematic.normalize({"lut": "../../etc/passwd"})["lut"] == "none"
    assert cinematic.normalize({"lut": "Warm Test"})["lut"] == "Warm Test"


def test_lut_path_is_escaped_for_the_filtergraph(lut_dir):
    graph = cinematic.build_filter_complex({"lut": "Warm Test"}, 1080, 1920)
    value = cinematic._filter_path(str(lut_dir / "Warm Test.cube"))
    assert f"lut3d=file={value}" in graph
    # Forward slashes only, and every colon (the Windows drive) escaped.
    assert value.replace("\\:", "").count(":") == 0
    assert "\\\\" not in value
    assert cinematic._filter_path("C:\\a b\\it's.cube") == "'C\\:/a b/it\\'s.cube'"


def test_options_for_api(lut_dir):
    opts = cinematic.options_for_api()
    assert opts["grades"][0] == {"value": "none", "label": "No grade"}
    assert {"value": "crisp", "label": "Crisp"} in opts["sharpen"]
    assert opts["luts"] == ["Warm Test"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_every_preset_renders(tmp_path, lut_dir):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=320x568:r=10:d=1", "-pix_fmt", "yuv420p", str(src)], check=True)
    looks = [{"color_grade": g} for g in cinematic.COLOR_GRADES if g != "none"]
    looks += [{"sharpen": s} for s in cinematic.SHARPEN_PRESETS if s != "none"]
    looks.append({"lut": "Warm Test", "color_grade": "moody_cinema", "sharpen": "studio_hd",
                  "glow": True, "vignette": True})
    for i, look in enumerate(looks):
        out = tmp_path / f"o{i}.mp4"
        assert cinematic.apply_cinematic_effects(str(src), look, output_path=str(out)), look
        assert out.stat().st_size > 0


def test_look_options_endpoint(lut_dir):
    import asyncio
    import httpx
    app_module = pytest.importorskip("app")

    async def _get():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.get("/api/look-options")
    res = asyncio.run(_get())
    assert res.status_code == 200
    body = res.json()
    assert "film_print" in [g["value"] for g in body["grades"]]
    assert body["luts"] == ["Warm Test"]
