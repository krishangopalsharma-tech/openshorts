"""HTTP tests for the spoken-language option on /api/process.

Whisper auto-detect on Hindustani slides into English TRANSLATION mid-file, so
a Hindi upload came back with English captions. Naming the language fixes that,
and the extra value "hinglish" transcribes as Hindi and romanises the result
(translit.py) so the Latin caption presets still apply.

Same conventions as test_process_handover.py: a real ASGI round-trip against
the imported app; the job is only ENQUEUED, so what these tests assert is the
env handed to the subprocess and the resume manifest written beside it.
"""

import asyncio
import json
import os

import httpx
import pytest

app_module = pytest.importorskip("app")


def _post_process(json_body):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            return await client.post("/api/process", json=json_body,
                                     headers={"X-Gemini-Key": "test-key"})
    return asyncio.run(_do())


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    up_root = tmp_path / "uploads"
    out_root.mkdir()
    up_root.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(up_root))
    monkeypatch.setattr(app_module, "MIN_SOURCE_SECONDS", 0)

    async def _probe(url):
        return {"max_height": 1080, "duration": 600}
    monkeypatch.setattr(app_module, "_probe_youtube_quality", _probe)
    return out_root, up_root


def _submit(language=None):
    body = {"url": "https://www.youtube.com/watch?v=ok", "acknowledged": True}
    if language is not None:
        body["language"] = language
    resp = _post_process(body)
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    return job_id, app_module.jobs[job_id]


def test_no_language_leaves_detection_alone(dirs, monkeypatch):
    monkeypatch.delenv("TRANSCRIBE_LANGUAGE", raising=False)
    _, job = _submit()
    assert "TRANSCRIBE_LANGUAGE" not in job["env"]


def test_auto_is_the_same_as_saying_nothing(dirs, monkeypatch):
    monkeypatch.delenv("TRANSCRIBE_LANGUAGE", raising=False)
    _, job = _submit("auto")
    assert "TRANSCRIBE_LANGUAGE" not in job["env"]


def test_hinglish_reaches_the_subprocess(dirs):
    _, job = _submit("Hinglish")
    assert job["env"]["TRANSCRIBE_LANGUAGE"] == "hinglish"


def test_language_code_reaches_the_subprocess(dirs):
    _, job = _submit("hi")
    assert job["env"]["TRANSCRIBE_LANGUAGE"] == "hi"


def test_garbage_language_is_rejected(dirs):
    resp = _post_process({"url": "https://www.youtube.com/watch?v=ok",
                          "acknowledged": True,
                          "language": "; rm -rf /"})
    assert resp.status_code == 400


def test_choice_survives_a_redeploy(dirs, monkeypatch):
    """A resumed job rebuilds env from os.environ — the request's choices have
    to come back with it, or a Hinglish job finishes in Devanagari."""
    out_root, _ = dirs
    monkeypatch.delenv("TRANSCRIBE_LANGUAGE", raising=False)
    job_id, _ = _submit("hinglish")

    manifest = json.load(open(os.path.join(out_root, job_id, ".resume.json")))
    assert manifest["job_env"]["TRANSCRIBE_LANGUAGE"] == "hinglish"
    # The manifest sits next to the user's video: no server credentials in it.
    assert "GEMINI_API_KEY" not in manifest["job_env"]

    app_module.jobs.pop(job_id)
    app_module._resume_interrupted_jobs()
    assert app_module.jobs[job_id]["env"]["TRANSCRIBE_LANGUAGE"] == "hinglish"


def test_resume_ignores_env_keys_outside_the_allowlist(dirs, monkeypatch):
    out_root, _ = dirs
    job_id, _ = _submit("hi")
    path = os.path.join(out_root, job_id, ".resume.json")
    manifest = json.load(open(path))
    manifest["job_env"]["GEMINI_API_KEY"] = "stolen"
    json.dump(manifest, open(path, "w"))

    app_module.jobs.pop(job_id)
    app_module._resume_interrupted_jobs()
    assert app_module.jobs[job_id]["env"].get("GEMINI_API_KEY") != "stolen"
