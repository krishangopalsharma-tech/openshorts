"""Saved Twitter/X channels: store, legacy migration, routes, render wiring."""
import json
import os

import pytest
from PIL import Image


def _png(path, color=(255, 0, 0, 255)):
    Image.new("RGBA", (8, 8), color).save(path, "PNG")


def test_channel_store_crud_and_handle_rules(tmp_path):
    import comedy_studio as cs

    root = str(tmp_path)
    assert cs.list_channels(root) == []

    a = cs.create_channel(root, {"channel_name": "Mockingbirds", "handle": "Mockingbirds",
                                 "categories": ["quote", "inspirational"]})
    assert a["handle"] == "@Mockingbirds"            # @ added
    assert a["categories"] == ["inspirational", "quote"]
    assert a["has_avatar"] is False

    with pytest.raises(ValueError):                  # same handle twice
        cs.create_channel(root, {"channel_name": "Other", "handle": "@mockingbirds"})
    with pytest.raises(ValueError):                  # name required
        cs.create_channel(root, {"channel_name": "", "handle": "@x"})
    with pytest.raises(ValueError):                  # unknown short type
        cs.create_channel(root, {"channel_name": "X", "handle": "@x", "categories": ["nope"]})

    b = cs.create_channel(root, {"channel_name": "Cheerful", "handle": "@Cheerfulvids", "verified": False})
    assert [c["id"] for c in cs.list_channels(root)] == [a["id"], b["id"]]

    upd = cs.update_channel(root, b["id"], {"channel_name": "Cheerful Videos"})
    assert upd["channel_name"] == "Cheerful Videos" and upd["verified"] is False
    with pytest.raises(ValueError):                  # cannot steal another handle
        cs.update_channel(root, b["id"], {"handle": "@Mockingbirds"})

    with pytest.raises(ValueError):                  # path traversal in an id
        cs.get_channel(root, "../etc")


def test_avatars_are_per_channel_and_survive_delete(tmp_path):
    import comedy_studio as cs

    root = str(tmp_path)
    a = cs.create_channel(root, {"channel_name": "A", "handle": "@a"})
    b = cs.create_channel(root, {"channel_name": "B", "handle": "@b"})
    _png(cs.channel_avatar_path(root, a["id"]), (255, 0, 0, 255))
    _png(cs.channel_avatar_path(root, b["id"]), (0, 0, 255, 255))

    cfg_a = cs.channel_card_config(root, a["id"])
    cfg_b = cs.channel_card_config(root, b["id"])
    assert cfg_a["avatar_path"] != cfg_b["avatar_path"]
    assert cfg_a["channel_id"] == a["id"] and cfg_a["handle"] == "@a"

    cs.delete_channel(root, a["id"])
    assert [c["id"] for c in cs.list_channels(root)] == [b["id"]]
    # A clip rendered for A still has its face to re-render with.
    assert os.path.isfile(cfg_a["avatar_path"])
    # And A's id is not handed to the next channel with the same handle.
    again = cs.create_channel(root, {"channel_name": "A2", "handle": "@a"})
    assert again["id"] != a["id"]


def test_legacy_profile_becomes_first_channel(tmp_path):
    import comedy_studio as cs

    root = str(tmp_path)
    with open(os.path.join(root, cs.COMEDY_PROFILE_FILE), "w", encoding="utf-8") as f:
        json.dump({"channel_name": "Mockingbirds", "handle": "@Mockingbirds",
                   "verified": True, "burn_subtitles": False}, f)
    _png(os.path.join(root, cs.COMEDY_AVATAR_FILE))

    chans = cs.list_channels(root)
    assert len(chans) == 1
    assert chans[0]["channel_name"] == "Mockingbirds" and chans[0]["has_avatar"]
    # Migration runs once: deleting the channel does not resurrect it.
    cs.delete_channel(root, chans[0]["id"])
    assert cs.list_channels(root) == []


def test_channel_routes_and_render_uses_saved_channel(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path))
    client = TestClient(app_module.app)

    r = client.post("/api/comedy/channels", json={"channel_name": "Mockingbirds", "handle": "@Mockingbirds"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert client.post("/api/comedy/channels", json={"channel_name": "X", "handle": "@Mockingbirds"}).status_code == 400

    img = tmp_path / "a.png"
    _png(str(img))
    with open(img, "rb") as f:
        r = client.post(f"/api/comedy/channels/{cid}/avatar", files={"file": ("a.png", f, "image/png")})
    assert r.status_code == 200 and r.json()["avatar_url"]
    assert client.get(f"/api/comedy/channels/{cid}/avatar").status_code == 200

    r = client.patch(f"/api/comedy/channels/{cid}", json={"categories": ["quote"]})
    assert r.json()["categories"] == ["quote"]
    listed = client.get("/api/comedy/channels").json()["channels"]
    assert [c["id"] for c in listed] == [cid]

    assert client.get(f"/api/comedy/preview-card?channel_id={cid}").status_code == 200
    assert client.get("/api/comedy/preview-card?channel_id=nope").status_code == 404

    assert client.delete(f"/api/comedy/channels/{cid}").status_code == 200
    assert client.get("/api/comedy/channels").json()["channels"] == []


def test_sweeps_skip_the_channels_dir():
    import app as app_module
    import comedy_studio

    assert app_module.COMEDY_CHANNELS_SUBDIR == comedy_studio.CHANNELS_SUBDIR


def test_logo_drawn_where_asked_and_clamped(tmp_path):
    import comedy_card

    logo = tmp_path / "logo.png"
    Image.new("RGBA", (200, 100), (255, 0, 0, 255)).save(logo)

    plain = tmp_path / "plain.png"
    box = comedy_card.render_card_overlay(str(plain), pov_text="hi")
    out = tmp_path / "br.png"
    comedy_card.render_card_overlay(str(out), pov_text="hi", logo_path=str(logo),
                                    logo_position="video_br", logo_size=0.2, logo_opacity=1.0)
    im = Image.open(out).convert("RGBA")
    # Inside the video window's bottom-right corner: opaque red over the hole.
    x = box["x"] + box["w"] - comedy_card.LOGO_MARGIN - 10
    y = box["y"] + box["h"] - comedy_card.LOGO_MARGIN - 10
    assert im.getpixel((x, y)) == (255, 0, 0, 255)
    # Without a logo that pixel is the transparent video hole.
    assert Image.open(plain).convert("RGBA").getpixel((x, y))[3] == 0
    # Top-left of the window stays clear.
    assert im.getpixel((box["x"] + 40, box["y"] + 40))[3] == 0

    foot = tmp_path / "footer.png"
    comedy_card.render_card_overlay(str(foot), pov_text="hi", logo_path=str(logo),
                                    logo_position="footer", logo_opacity=0.5)
    fim = Image.open(foot).convert("RGBA")
    fy = box["y"] + box["h"] + (comedy_card.CANVAS_H - box["y"] - box["h"]) // 2
    r, g, b, a = fim.getpixel((comedy_card.CANVAS_W // 2, fy))
    assert r > 200 and g < 200 and a == 255     # half-transparent red over white

    assert comedy_card.normalize_logo_settings(
        {"logo_position": "moon", "logo_size": 9, "logo_opacity": -1}) == {
        "logo_position": "video_br", "logo_size": 0.4, "logo_opacity": 0.2}
    kw = comedy_card.card_kwargs({"handle": "@a", "logo_path": str(logo), "logo_position": "footer"})
    assert kw["logo_path"] == str(logo) and kw["logo_position"] == "footer"


def test_channel_logo_routes_and_card_config(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module
    import comedy_studio as cs

    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path))
    client = TestClient(app_module.app)
    cid = client.post("/api/comedy/channels", json={
        "channel_name": "Logo Co", "handle": "@logoco",
        "logo_position": "header_right", "logo_size": 0.2}).json()["id"]

    img = tmp_path / "l.png"
    Image.new("RGBA", (3000, 600), (0, 128, 255, 200)).save(img)
    with open(img, "rb") as f:
        r = client.post(f"/api/comedy/channels/{cid}/logo", files={"file": ("l.png", f, "image/png")})
    body = r.json()
    assert r.status_code == 200 and body["has_logo"] and body["logo_position"] == "header_right"
    stored = cs.channel_logo_path(str(tmp_path), cid)
    assert max(Image.open(stored).size) <= 1024          # downscaled on upload
    assert client.get(f"/api/comedy/channels/{cid}/logo").status_code == 200

    cfg = cs.channel_card_config(str(tmp_path), cid)
    assert cfg["logo_path"] == stored and cfg["logo_size"] == 0.2
    assert client.get(f"/api/comedy/preview-card?channel_id={cid}").status_code == 200

    r = client.patch(f"/api/comedy/channels/{cid}", json={"logo_opacity": 0.5})
    assert r.json()["logo_opacity"] == 0.5 and r.json()["logo_position"] == "header_right"

    r = client.delete(f"/api/comedy/channels/{cid}/logo")
    assert r.json()["has_logo"] is False and not os.path.exists(stored)
