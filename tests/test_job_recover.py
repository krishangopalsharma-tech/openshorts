"""POST /api/job/{id}/recover: bring back a job that failed after rendering."""
import os
import uuid

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(app_module, "BILLING_ENABLED", False)
    return TestClient(app_module.app), app_module, tmp_path


def _job_dir(root, n_rendered, n_listed=3, emoji=True):
    import jsonfile

    job_id = str(uuid.uuid4())
    d = os.path.join(str(root), job_id)
    os.makedirs(d)
    base = "Film.2005"
    shorts = [{"start": i * 30, "end": i * 30 + 20,
               "pov_text": f"clip {i} 📜🔥" if emoji else f"clip {i}"} for i in range(n_listed)]
    jsonfile.dump_atomic(os.path.join(d, f"{base}_metadata.json"), {"shorts": shorts})
    for i in range(n_rendered):
        with open(os.path.join(d, f"{base}_clip_{i + 1}.mp4"), "wb") as f:
            f.write(b"\x00" * 64)
    return job_id


def test_recover_failed_job_with_emoji_metadata(client):
    c, app_module, root = client
    job_id = _job_dir(root, n_rendered=2)
    app_module.jobs[job_id] = {"status": "failed", "logs": ["Error: charmap"],
                               "output_dir": os.path.join(str(root), job_id), "user_id": None}

    r = c.post(f"/api/job/{job_id}/recover")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed" and body["recovered"] == 2 and body["missing"] == 1
    assert body["result"]["clips"][0]["pov_text"].endswith("📜🔥")
    assert body["result"]["clips"][0]["video_url"].startswith(f"/videos/{job_id}/")

    s = c.get(f"/api/status/{job_id}").json()
    assert s["status"] == "completed" and len(s["result"]["clips"]) == 2


def test_recover_refuses_running_empty_and_bad_ids(client):
    c, app_module, root = client
    running = _job_dir(root, n_rendered=1)
    app_module.jobs[running] = {"status": "processing", "logs": [],
                                "output_dir": os.path.join(str(root), running), "user_id": None}
    assert c.post(f"/api/job/{running}/recover").status_code == 409

    nothing = _job_dir(root, n_rendered=0)
    assert c.post(f"/api/job/{nothing}/recover").status_code == 404

    assert c.post("/api/job/..%2F..%2Fetc/recover").status_code in (400, 404)
    assert c.post("/api/job/not-a-uuid/recover").status_code == 400
    assert c.post(f"/api/job/{uuid.uuid4()}/recover").status_code == 404


def test_recover_is_off_in_cloud_mode(client, monkeypatch):
    c, app_module, root = client
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
    job_id = _job_dir(root, n_rendered=1)
    assert c.post(f"/api/job/{job_id}/recover").status_code == 404
