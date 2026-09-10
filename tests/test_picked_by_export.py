"""`picked_by` has to survive the two exports that project fields explicitly.

metadata.json is dumped wholesale so it rides along on its own, but the ZIP's
CSV and the webhook payload both name their fields, so each needs the key added
— and the CSV's DictWriter has an explicit `fieldnames`, which raises
ValueError on an unknown row key rather than silently dropping it.
"""
import asyncio
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import app as app_module


JOB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _job_on_disk(tmp_path, picked_by="gemini:gemini-3.7-flash"):
    out = tmp_path / JOB
    out.mkdir(parents=True)
    base = "My Video"
    (out / f"{base}_clip_1.mp4").write_bytes(b"\x00" * 2048)
    (out / f"{base}_metadata.json").write_text(json.dumps({"shorts": [{
        "start": 1.0, "end": 20.0,
        "video_title_for_youtube_short": "A title",
        "video_description_for_instagram": "A description",
        "video_tags": "one, two",
        "picked_by": picked_by,
    }]}), encoding="utf-8")
    return out


class TestZipExport:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path))
        monkeypatch.setattr(app_module, "jobs", {})
        return TestClient(app_module.app)

    def test_the_csv_names_who_picked_each_clip(self, client, tmp_path):
        _job_on_disk(tmp_path)
        r = client.get(f"/api/jobs/{JOB}/download-all")
        assert r.status_code == 200, r.text
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            csv_name = next(n for n in z.namelist() if n.endswith(".csv"))
            text = z.read(csv_name).decode("utf-8-sig")
        header, row = text.splitlines()[0], text.splitlines()[1]
        assert "picked_by" in header
        assert "gemini:gemini-3.7-flash" in row

    def test_a_clip_with_no_picker_recorded_still_exports(self, client, tmp_path):
        """Clips generated before this existed have no picked_by. The CSV must
        still build — an unknown row key would raise, and a missing one must
        not."""
        out = _job_on_disk(tmp_path)
        meta = next(out.glob("*_metadata.json"))
        data = json.loads(meta.read_text(encoding="utf-8"))
        del data["shorts"][0]["picked_by"]
        meta.write_text(json.dumps(data), encoding="utf-8")

        r = client.get(f"/api/jobs/{JOB}/download-all")
        assert r.status_code == 200, r.text


class TestWebhookPayload:
    def test_the_clip_entries_carry_who_picked_them(self):
        job = {"base_url": "https://api.example.com", "result": {"clips": [
            {"video_url": "/videos/x/clip_1.mp4", "title": "A",
             "picked_by": "claude"},
        ]}}
        entries = asyncio.run(app_module._webhook_clip_entries(JOB, job))
        assert entries[0]["picked_by"] == "claude"
        assert entries[0]["video_url"] == "https://api.example.com/videos/x/clip_1.mp4"

    def test_an_older_clip_reports_no_picker_rather_than_failing(self):
        job = {"result": {"clips": [{"video_url": "/videos/x/clip_1.mp4"}]}}
        entries = asyncio.run(app_module._webhook_clip_entries(JOB, job))
        assert entries[0]["picked_by"] is None
