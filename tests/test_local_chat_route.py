"""The chat route driven from the dashboard instead of the command line.

Three things have to hold for the panel to be worth having:

  * `transcribe_only` stops at paste_into_chat.txt and is not mistaken for a
    job that failed to render anything,
  * the render half REUSES that transcript — without it, pasting the picks
    back costs a second full transcription of the same video (~25 min on an
    hour-long episode), which is the entire saving the route exists for,
  * the brief can be fetched, because the /videos mount refuses .txt.
"""
import asyncio
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

import app as app_module


JOB = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def local(tmp_path, monkeypatch):
    """A self-host app with its own OUTPUT_DIR, and nothing actually queued."""
    out = tmp_path / "output"
    ups = tmp_path / "uploads"
    out.mkdir()
    ups.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(ups))
    monkeypatch.setattr(app_module, "BILLING_ENABLED", False)
    monkeypatch.setattr(app_module, "jobs", {})
    monkeypatch.setattr(app_module, "_enqueue_job", lambda *a, **k: None)

    video = tmp_path / "episode.mp4"
    video.write_bytes(b"\x00" * 2048)
    return {"client": TestClient(app_module.app), "out": out,
            "video": str(video), "tmp": tmp_path}


def _start(local, **body):
    return local["client"].post("/api/process/local",
                                json={"video_path": local["video"], **body})


def _cmd(local, job_id):
    return app_module.jobs[job_id]["cmd"]


class TestTranscribeOnly:
    def test_it_runs_main_py_with_transcribe_only_and_no_picker(self, local):
        r = _start(local, transcribe_only=True)
        assert r.status_code == 200
        body = r.json()
        assert body["transcribe_only"] is True
        cmd = _cmd(local, body["job_id"])
        assert "--transcribe-only" in cmd
        assert "--clips" not in cmd

    def test_the_job_is_marked_so_an_empty_job_dir_is_not_read_as_a_failure(self, local):
        job_id = _start(local, transcribe_only=True).json()["job_id"]
        assert app_module.jobs[job_id]["transcribe_only"] is True

    def test_asking_for_a_transcript_and_clips_at_once_is_refused(self, local):
        """Not a silent precedence rule: the two halves of the route are
        deliberately separate runs, and quietly dropping one of them would
        transcribe when the user asked to render."""
        r = _start(local, transcribe_only=True, clips_json='[{"start":0,"end":10}]')
        assert r.status_code == 400
        assert "no clips" in r.json()["detail"]

    def test_an_ordinary_render_still_has_no_transcribe_only_flag(self, local):
        job_id = _start(local).json()["job_id"]
        assert "--transcribe-only" not in _cmd(local, job_id)
        assert app_module.jobs[job_id]["transcribe_only"] is False


class TestReusingTheTranscript:
    def _with_transcript(self, local, job_id=JOB):
        job_dir = local["out"] / job_id
        job_dir.mkdir()
        (job_dir / "transcript.json").write_text(
            json.dumps({"language": "hinglish",
                        "segments": [{"start": 0.0, "end": 4.0, "text": "namaste"}]}),
            encoding="utf-8")
        return job_dir

    def test_the_render_points_main_py_at_the_earlier_transcript(self, local):
        job_dir = self._with_transcript(local)
        r = _start(local, transcript_job=JOB, clips_json='[{"start":0,"end":10}]')
        assert r.status_code == 200
        cmd = _cmd(local, r.json()["job_id"])
        assert "--transcript" in cmd
        assert cmd[cmd.index("--transcript") + 1] == str(job_dir / "transcript.json")

    def test_it_renders_in_a_fresh_job_dir_not_the_transcribe_only_one(self, local):
        """The transcribe-only job id is already taken, and its dir holds no
        metadata.json — reusing it would collide with a live job entry."""
        job_dir = self._with_transcript(local)
        new_id = _start(local, transcript_job=JOB,
                        clips_json='[{"start":0,"end":10}]').json()["job_id"]
        assert new_id != JOB
        assert app_module.jobs[new_id]["output_dir"] != str(job_dir)

    def test_a_job_with_no_transcript_is_refused_before_anything_is_queued(self, local):
        (local["out"] / JOB).mkdir()
        r = _start(local, transcript_job=JOB, clips_json='[{"start":0,"end":10}]')
        assert r.status_code == 400
        assert "no transcript" in r.json()["detail"]
        assert app_module.jobs == {}

    @pytest.mark.parametrize("bad", ["../../etc", "..", "a/b", JOB + "/..",
                                     "not-a-uuid", ""])
    def test_a_job_id_that_is_not_a_job_id_never_reaches_the_filesystem(self, local, bad):
        """It is used to build a path, so it is matched against the uuid shape
        rather than merely cleaned up."""
        r = _start(local, transcript_job=bad, clips_json='[{"start":0,"end":10}]')
        if bad == "":
            assert r.status_code == 200          # absent, not malformed
        else:
            assert r.status_code == 400


class TestBriefEndpoint:
    def _write_brief(self, local, job_id=JOB, text="PROMPT\n\n[0.0-4.0] namaste\n"):
        job_dir = local["out"] / job_id
        job_dir.mkdir(exist_ok=True)
        (job_dir / "paste_into_chat.txt").write_text(text, encoding="utf-8")
        return job_dir

    def test_it_hands_back_the_file_the_chat_route_produces(self, local):
        self._write_brief(local)
        r = local["client"].get(f"/api/local/brief/{JOB}")
        assert r.status_code == 200
        body = r.json()
        assert body["text"] == "PROMPT\n\n[0.0-4.0] namaste\n"
        assert body["filename"] == "paste_into_chat.txt"

    def test_it_reads_from_disk_so_it_survives_a_restart(self, local):
        """A transcribe-only job writes no *_metadata.json, so it is never
        recovered into the job table — the file on disk is all there is."""
        self._write_brief(local)
        assert app_module.jobs == {}
        assert local["client"].get(f"/api/local/brief/{JOB}").status_code == 200

    def test_it_reports_what_it_is_holding_so_the_panel_can_say_so(self, local):
        job_dir = self._write_brief(local)
        (job_dir / "transcript.json").write_text(
            json.dumps({"language": "hinglish", "segments": [
                {"start": float(i * 10), "end": float(i * 10 + 8), "text": "line"}
                for i in range(40)]}),
            encoding="utf-8")
        body = local["client"].get(f"/api/local/brief/{JOB}").json()
        assert body["segments"] == 40
        assert body["language"] == "hinglish"
        assert body["min_clips"] and body["max_clips"] >= body["min_clips"]

    def test_a_broken_transcript_never_withholds_the_brief(self, local):
        """Every stat beside the text is decoration; the text is the point."""
        job_dir = self._write_brief(local)
        (job_dir / "transcript.json").write_text("{not json", encoding="utf-8")
        body = local["client"].get(f"/api/local/brief/{JOB}").json()
        assert body["text"].startswith("PROMPT")
        assert body["segments"] is None

    def test_a_job_without_one_is_a_404_not_an_error(self, local):
        assert local["client"].get(f"/api/local/brief/{JOB}").status_code == 404

    @pytest.mark.parametrize("bad", ["..%2F..%2Fetc", "abc", "0" * 40])
    def test_it_will_not_read_a_path_it_was_handed(self, local, bad):
        assert local["client"].get(f"/api/local/brief/{bad}").status_code in (400, 404)

    def test_neither_local_endpoint_exists_on_a_hosted_instance(self, local, monkeypatch):
        """Both read the server's own filesystem, so on a multi-tenant install
        they would let a visitor enumerate and name paths on the box."""
        self._write_brief(local)
        monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
        assert local["client"].get(f"/api/local/brief/{JOB}").status_code == 404
        assert local["client"].get("/api/local/browse").status_code == 404
        assert _start(local, transcribe_only=True).status_code == 404


class TestCompletionIsNotAFailure:
    """run_job fails a job that produced no *_metadata.json. That is right for
    a render and wrong for a transcription, which is the whole point of this
    job — so the flag has to be honoured before that check."""

    def _run(self, local, transcribe_only, write_brief):
        job_id = "run-" + ("t" if transcribe_only else "r")
        out = local["out"] / job_id
        out.mkdir()
        if write_brief:
            (out / "paste_into_chat.txt").write_text("PROMPT", encoding="utf-8")
        job = {
            "status": "queued", "logs": [], "output_dir": str(out),
            "cmd": [sys.executable, "-c", "pass"], "env": os.environ.copy(),
            "transcribe_only": transcribe_only, "user_id": None,
            "reservation_id": None,
        }
        app_module.jobs[job_id] = job
        asyncio.run(app_module.run_job(job_id, job))
        return app_module.jobs[job_id]

    def test_a_transcription_that_wrote_its_brief_completed(self, local):
        job = self._run(local, transcribe_only=True, write_brief=True)
        assert job["status"] == "completed"
        assert job["result"] == {"clips": [], "transcribe_only": True}

    def test_a_transcription_with_no_brief_did_fail(self, local):
        job = self._run(local, transcribe_only=True, write_brief=False)
        assert job["status"] == "failed"

    def test_a_render_with_no_metadata_still_fails(self, local):
        job = self._run(local, transcribe_only=False, write_brief=False)
        assert job["status"] == "failed"


class TestImportedTranscript:
    """A transcript the user already had (YouTube's, or an earlier run).

    The saving this exists for is the whole transcription — ~25 min of GPU on
    an hour-long source — so the only thing that really has to hold is that
    main.py is told to reuse the file instead of listening to the audio.
    """

    def test_it_is_written_into_the_job_and_passed_as_transcript(self, local):
        r = _start(local, transcript_text="0:14 so I said no\n0:20 and he left",
                   transcript_name="yt.txt")
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        cmd = _cmd(local, job_id)
        assert "--transcript" in cmd
        written = cmd[cmd.index("--transcript") + 1]
        assert os.path.isfile(written)
        data = json.loads(open(written, encoding="utf-8").read())
        assert [s["text"] for s in data["segments"]] == ["so I said no", "and he left"]

    def test_whisper_json_keeps_its_word_timings(self, local):
        doc = json.dumps({"language": "en", "segments": [
            {"start": 0.0, "end": 2.0, "text": "hello there",
             "words": [{"word": " hello", "start": 0.0, "end": 1.0},
                       {"word": " there", "start": 1.0, "end": 2.0}]}]})
        job_id = _start(local, transcript_text=doc,
                        transcript_name="t.json").json()["job_id"]
        cmd = _cmd(local, job_id)
        data = json.loads(open(cmd[cmd.index("--transcript") + 1], encoding="utf-8").read())
        assert data["segments"][0]["words"][1]["start"] == 1.0

    def test_it_lands_as_transcript_json_so_a_later_job_can_point_at_it(self, local):
        """Same name and shape a transcribed job writes, which is what makes
        `transcript_job` work against an imported one too."""
        job_id = _start(local, transcript_text="0:01 hi").json()["job_id"]
        assert os.path.isfile(os.path.join(local["out"], job_id, "transcript.json"))

    def test_a_broken_transcript_is_refused_before_a_job_exists(self, local):
        """Finding out after the job dir and manifest are on disk leaves a
        dead job behind for the user to notice and clean up."""
        before = set(os.listdir(local["out"]))
        r = _start(local, transcript_text="no timestamps anywhere in here",
                   transcript_name="yt.txt")
        assert r.status_code == 400
        assert "timestamp" in r.json()["detail"].lower()
        assert set(os.listdir(local["out"])) == before

    def test_an_imported_transcript_and_an_earlier_job_at_once_is_refused(self, local):
        r = _start(local, transcript_text="0:01 hi", transcript_job=JOB)
        assert r.status_code == 400
        assert "not both" in r.json()["detail"]

    def test_transcribe_only_with_an_import_skips_the_audio_entirely(self, local):
        """The best case of the feature: a brief for the chat with no GPU at
        all, because the transcript was handed to us."""
        job_id = _start(local, transcribe_only=True,
                        transcript_text="0:01 hi there").json()["job_id"]
        cmd = _cmd(local, job_id)
        assert "--transcribe-only" in cmd and "--transcript" in cmd


class TestBrowseListing:
    """The picker's listing. It is capped at 500 entries, so filtering and
    sorting have to happen here rather than in the browser — a client-side
    filter would only ever search the part that survived the cap."""

    def _folder(self, local):
        import time
        root = local["tmp"] / "media"
        root.mkdir()
        for i, name in enumerate(["alpha.mp4", "beta.mkv", "gamma.mp4"]):
            f = root / name
            f.write_bytes(b"\x00" * 1024)
            os.utime(f, (time.time() - i * 3600, time.time() - i * 3600))
        (root / "nested").mkdir()
        (root / "notes.pdf").write_bytes(b"x")
        return root

    def test_it_lists_videos_with_size_and_mtime(self, local):
        root = self._folder(local)
        body = local["client"].get("/api/local/browse",
                                   params={"path": str(root)}).json()
        assert [f["name"] for f in body["files"]] == ["alpha.mp4", "beta.mkv", "gamma.mp4"]
        assert all(f["mtime"] and f["size"] for f in body["files"])
        assert body["dirs"] == ["nested"]

    def test_the_filter_runs_on_the_server(self, local):
        root = self._folder(local)
        body = local["client"].get("/api/local/browse",
                                   params={"path": str(root), "q": "mm"}).json()
        assert [f["name"] for f in body["files"]] == ["gamma.mp4"]

    def test_newest_first_is_available(self, local):
        root = self._folder(local)
        body = local["client"].get("/api/local/browse",
                                   params={"path": str(root), "sort": "modified"}).json()
        assert [f["name"] for f in body["files"]] == ["alpha.mp4", "beta.mkv", "gamma.mp4"]

    def test_dirs_stay_a_list_of_names_for_an_older_bundle(self, local):
        """A cached dashboard build reads `dirs`; the richer shape rides
        alongside it rather than replacing it."""
        root = self._folder(local)
        body = local["client"].get("/api/local/browse",
                                   params={"path": str(root)}).json()
        assert body["dirs"] == ["nested"]
        assert body["dir_entries"][0]["name"] == "nested"

    def test_the_thumbnail_route_refuses_a_non_video(self, local):
        root = self._folder(local)
        r = local["client"].get("/api/local/thumb",
                                params={"path": str(root / "notes.pdf")})
        assert r.status_code == 400


class TestPipeDrain:
    """app.enqueue_output is the ONLY thing draining the child's pipe.

    The OS buffer is 64 KB; a child that fills it blocks inside print(),
    alive and silent, while run_job heartbeats forever on process.poll().
    A transcribe-only run wedged for 40 minutes that way, after its
    transcription had already finished — so the reader must survive
    anything one line can throw at it.
    """

    class _Pipe:
        """A pipe that yields the given lines, then EOF."""

        def __init__(self, lines):
            self._lines = list(lines)
            self.closed = False

        def readline(self):
            return self._lines.pop(0) if self._lines else b''

        def close(self):
            self.closed = True

    def _drain(self, lines, job_id="job", jobs=None):
        import app as m
        if jobs is not None:
            m.jobs.clear()
            m.jobs.update(jobs)
        pipe = self._Pipe(lines)
        m.enqueue_output(pipe, job_id)
        return pipe

    def test_a_non_utf8_byte_does_not_kill_the_reader(self, local, monkeypatch):
        """stderr is merged into this pipe and native libs write raw bytes to
        fd 2 outside Python's encoder. A strict decode used to raise here,
        break the loop and close the pipe on a child still writing."""
        monkeypatch.setattr(app_module, "jobs", {"job": {"logs": []}})
        self._drain([b"before\n", b"native \x92quote\x92\n", b"after\n"])
        logs = app_module.jobs["job"]["logs"]
        assert logs[0] == "before"
        assert logs[-1] == "after", "the reader stopped at the bad byte"
        assert len(logs) == 3

    def test_it_reads_to_eof_even_when_a_line_explodes(self, local, monkeypatch):
        monkeypatch.setattr(app_module, "jobs", {"job": {"logs": []}})
        monkeypatch.setattr(app_module, "_scrub_secrets",
                            lambda s: (_ for _ in ()).throw(RuntimeError("boom"))
                            if "bad" in s else s)
        self._drain([b"one\n", b"bad\n", b"two\n"])
        assert app_module.jobs["job"]["logs"] == ["one", "two"]

    def test_the_log_is_capped(self, local, monkeypatch):
        """Unbounded it grows for the life of the job and is returned whole
        on every /api/status poll."""
        monkeypatch.setattr(app_module, "jobs", {"job": {"logs": []}})
        monkeypatch.setattr(app_module, "MAX_JOB_LOG_LINES", 10)
        self._drain([f"line {i}\n".encode() for i in range(50)])
        logs = app_module.jobs["job"]["logs"]
        assert len(logs) == 10
        assert logs[-1] == "line 49", "the newest lines are the ones kept"

    def test_a_blocked_operator_console_does_not_stop_the_drain(self, local, monkeypatch):
        """A Windows console with a QuickEdit selection blocks every write."""
        monkeypatch.setattr(app_module, "jobs", {"job": {"logs": []}})
        def explode(*a, **k):
            raise OSError("console is blocked")
        monkeypatch.setattr("builtins.print", explode)
        self._drain([b"one\n", b"two\n"])
        assert app_module.jobs["job"]["logs"] == ["one", "two"]


class TestLocalJobEnvironment:
    def test_the_child_gets_utf8_stdio(self, local):
        """main.py prints an emoji on its first line; on a cp1252 console the
        child dies before rendering anything. /api/process has set this for
        years and this endpoint never did."""
        job_id = _start(local).json()["job_id"]
        assert app_module.jobs[job_id]["env"]["PYTHONIOENCODING"] == "utf-8"

    def test_an_explicit_setting_still_wins(self, local, monkeypatch):
        monkeypatch.setenv("PYTHONIOENCODING", "utf-16")
        job_id = _start(local).json()["job_id"]
        assert app_module.jobs[job_id]["env"]["PYTHONIOENCODING"] == "utf-16"


class TestDriveNavigation:
    def test_a_drive_root_has_no_parent(self, local):
        """On Windows dirname("C:\\\\") is "C:" — a RELATIVE path meaning "the
        current directory on C", not the drive root. Offered as "up" it walks
        the user somewhere arbitrary instead of out of the drive."""
        root = os.path.abspath(os.sep) if os.name != "nt" else "C:\\"
        body = local["client"].get("/api/local/browse", params={"path": root}).json()
        assert body["parent"] is None

    def test_a_folder_inside_a_drive_still_has_one(self, local):
        body = local["client"].get(
            "/api/local/browse", params={"path": str(local["tmp"])}).json()
        assert body["parent"] and os.path.isabs(body["parent"])

    def test_every_listing_carries_the_drive_list(self, local):
        """The picker opens inside the remembered folder, so the drive list
        has to be reachable from a listing rather than only from the empty
        'this computer' screen."""
        body = local["client"].get(
            "/api/local/browse", params={"path": str(local["tmp"])}).json()
        assert body["drives"], "no drives offered from inside a folder"


class TestResumedTranscribeOnly:
    """A transcribe-only job that survives a restart is still transcribe-only.

    run_job fails any job that produced no *_metadata.json — which is exactly
    what a successful transcription produces — so the flag is what tells it
    the empty job dir is the expected outcome. It lived only in the in-memory
    job record, and a resume rebuilds that from the manifest, so a restart
    turned a finished brief into "Error: No metadata file generated" with
    paste_into_chat.txt sitting complete on disk.
    """

    def _manifest(self, local, job_id, **extra):
        job_dir = os.path.join(local["out"], job_id)
        os.makedirs(job_dir, exist_ok=True)
        payload = {
            "cmd": [sys.executable, "-u", "main.py", "-i", local["video"],
                    "-o", job_dir, "--transcribe-only"],
            "priority": 2, "user_id": None, "reservation_id": None,
            "watermark": False, "attempts": 0, "job_env": {},
        }
        payload.update(extra)
        with open(os.path.join(job_dir, ".resume.json"), "w") as f:
            json.dump(payload, f)
        return job_dir

    def test_the_flag_is_written_into_the_manifest(self, local):
        job_id = _start(local, transcribe_only=True).json()["job_id"]
        with open(os.path.join(local["out"], job_id, ".resume.json")) as f:
            assert json.load(f)["transcribe_only"] is True

    def test_an_ordinary_render_is_not_marked(self, local):
        job_id = _start(local).json()["job_id"]
        with open(os.path.join(local["out"], job_id, ".resume.json")) as f:
            assert json.load(f)["transcribe_only"] is False

    def test_a_resumed_job_keeps_the_flag(self, local):
        self._manifest(local, JOB, transcribe_only=True)
        app_module._resume_interrupted_jobs()
        assert app_module.jobs[JOB]["transcribe_only"] is True

    def test_an_old_manifest_recovers_from_the_argv(self, local):
        """Manifests already on disk predate the field; the command line has
        always carried the truth, so they must not resume as broken."""
        self._manifest(local, JOB)                      # no transcribe_only key
        app_module._resume_interrupted_jobs()
        assert app_module.jobs[JOB]["transcribe_only"] is True

    def test_a_resumed_render_is_still_not_transcribe_only(self, local):
        job_dir = os.path.join(local["out"], JOB)
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, ".resume.json"), "w") as f:
            json.dump({"cmd": [sys.executable, "main.py", "-i", local["video"],
                               "-o", job_dir],
                       "priority": 2, "attempts": 0}, f)
        app_module._resume_interrupted_jobs()
        assert app_module.jobs[JOB]["transcribe_only"] is False
