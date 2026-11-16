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
