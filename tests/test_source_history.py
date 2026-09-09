"""The duplicate-submit warning: source_history's matching and index, and the
/api/source/check endpoint the dashboard asks before spending an hour of GPU
on a video it has already cut."""
import json
import time

import pytest
from fastapi.testclient import TestClient

import app as app_module
import source_history as sh


FP = dict(title="Judge Verdict Full Episode.mp4", size_bytes=626_000_000,
          duration_seconds=4320.77)


def _index(tmp_path):
    return str(tmp_path / ".sources.json")


class TestTitleMatching:
    def test_the_same_title_matches_however_it_was_punctuated(self, tmp_path):
        idx = _index(tmp_path)
        sh.record(idx, sh.fingerprint(title="My Video - Part 1.mp4"), "j1", 12)

        for variant in ("My Video - Part 1.mp4", "my video part 1",
                        "My  Video   Part 1.MP4", "My Video – Part 1"):
            found = sh.find(idx, sh.fingerprint(title=variant))
            assert [m["job_id"] for m in found] == ["j1"], variant

    def test_a_detail_that_distinguishes_two_uploads_is_not_normalised_away(self, tmp_path):
        """"(HD)" and "1080p" look like noise, but they are often the only
        difference between two real uploads. A false "you already did this"
        teaches the user to click through the warning, so it is the worse
        error to make."""
        idx = _index(tmp_path)
        sh.record(idx, sh.fingerprint(title="Episode 3.mp4"), "j1", 5)
        assert sh.find(idx, sh.fingerprint(title="Episode 3 (HD).mp4")) == []
        assert sh.find(idx, sh.fingerprint(title="Episode 30.mp4")) == []

    def test_an_empty_or_missing_title_never_matches_everything(self, tmp_path):
        idx = _index(tmp_path)
        sh.record(idx, sh.fingerprint(title="Real Video.mp4"), "j1", 3)
        assert sh.find(idx, sh.fingerprint(title="")) == []
        assert sh.find(idx, sh.fingerprint(title=None)) == []


class TestOtherSignals:
    def test_a_youtube_link_matches_on_its_video_id_not_its_url_text(self, tmp_path):
        idx = _index(tmp_path)
        sh.record(idx, sh.fingerprint(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
                  "j1", 8)
        for variant in ("https://youtu.be/dQw4w9WgXcQ",
                        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
                        "https://www.youtube.com/shorts/dQw4w9WgXcQ"):
            found = sh.find(idx, sh.fingerprint(url=variant))
            assert [m["match_reason"] for m in found] == ["youtube_id"], variant

    def test_the_same_file_renamed_is_still_caught_by_size_and_duration(self, tmp_path):
        idx = _index(tmp_path)
        sh.record(idx, sh.fingerprint(**FP), "j1", 12)
        renamed = sh.fingerprint(title="copy of something else.mp4",
                                 size_bytes=FP["size_bytes"],
                                 duration_seconds=FP["duration_seconds"])
        assert [m["match_reason"] for m in sh.find(idx, renamed)] == ["size_and_duration"]

    def test_same_size_but_a_different_length_is_a_different_video(self, tmp_path):
        idx = _index(tmp_path)
        sh.record(idx, sh.fingerprint(**FP), "j1", 12)
        assert sh.find(idx, sh.fingerprint(title="other.mp4",
                                           size_bytes=FP["size_bytes"],
                                           duration_seconds=120.0)) == []

    def test_a_container_duration_is_compared_with_a_tolerance(self, tmp_path):
        idx = _index(tmp_path)
        sh.record(idx, sh.fingerprint(**FP), "j1", 12)
        near = sh.fingerprint(title="x.mp4", size_bytes=FP["size_bytes"],
                              duration_seconds=FP["duration_seconds"] + 0.4)
        assert len(sh.find(idx, near)) == 1


class TestIndex:
    def test_rerunning_a_source_replaces_its_entry_so_the_warning_is_current(self, tmp_path):
        idx = _index(tmp_path)
        sh.record(idx, sh.fingerprint(**FP), "old", 5)
        sh.record(idx, sh.fingerprint(**FP), "new", 12)
        found = sh.find(idx, sh.fingerprint(**FP))
        assert [(m["job_id"], m["clip_count"]) for m in found] == [("new", 12)]

    def test_a_source_with_nothing_to_match_on_is_not_recorded(self, tmp_path):
        idx = _index(tmp_path)
        assert sh.record(idx, sh.fingerprint(), "j1", 3) is False
        assert sh.load(idx) == []

    def test_the_index_is_capped_and_keeps_the_newest(self, tmp_path):
        idx = _index(tmp_path)
        for i in range(sh.MAX_ENTRIES + 25):
            sh.record(idx, sh.fingerprint(title=f"video {i}.mp4"), f"j{i}", 1)
        entries = sh.load(idx)
        assert len(entries) == sh.MAX_ENTRIES
        assert entries[0]["job_id"] == f"j{sh.MAX_ENTRIES + 24}"

    def test_a_corrupt_or_missing_index_never_breaks_a_submit(self, tmp_path):
        missing = _index(tmp_path)
        assert sh.load(missing) == [] and sh.find(missing, sh.fingerprint(**FP)) == []

        corrupt = tmp_path / "bad.json"
        corrupt.write_text("{not json", encoding="utf-8")
        assert sh.load(str(corrupt)) == []

    def test_a_half_written_index_cannot_be_left_behind(self, tmp_path):
        """The write goes through a temp file and one os.replace, so a reader
        either sees the old index or the new one."""
        idx = _index(tmp_path)
        sh.record(idx, sh.fingerprint(**FP), "j1", 12)
        assert json.loads(open(idx, encoding="utf-8").read())["sources"]
        assert not (tmp_path / ".sources.json.tmp").exists()


class TestCheckEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path))
        return TestClient(app_module.app)

    def test_an_unseen_video_is_not_a_duplicate(self, client):
        r = client.post("/api/source/check", json={"title": "brand new.mp4"})
        assert r.status_code == 200
        assert r.json() == {"duplicate": False, "matches": []}

    def test_a_recorded_video_comes_back_with_what_it_produced(self, client, tmp_path):
        sh.record(_index(tmp_path), sh.fingerprint(**FP), "job-abc", 12)
        body = client.post("/api/source/check",
                           json={"title": FP["title"], "size_bytes": FP["size_bytes"]}).json()
        assert body["duplicate"] is True
        assert body["matches"][0]["job_id"] == "job-abc"
        assert body["matches"][0]["clip_count"] == 12
        assert body["matches"][0]["match_reason"] == "title"

    def test_the_check_needs_no_file_only_its_name_and_size(self, client, tmp_path):
        """The dashboard has to be able to ask about a 600 MB upload without
        sending it, or the check would cost as much as the job."""
        sh.record(_index(tmp_path), sh.fingerprint(**FP), "job-abc", 12)
        r = client.post("/api/source/check", json={"size_bytes": FP["size_bytes"],
                                                   "duration_seconds": FP["duration_seconds"]})
        assert r.json()["duplicate"] is True

    def test_an_empty_body_is_answered_not_rejected(self, client):
        r = client.post("/api/source/check")
        assert r.status_code == 200 and r.json()["duplicate"] is False


class TestRecordingOnCompletion:
    def test_only_a_completed_job_that_made_clips_is_remembered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path))
        fp = sh.fingerprint(**FP)
        cases = [
            ("failed-job", {"status": "failed", "source_fp": fp, "result": {"clips": [1]}}),
            ("no-clips", {"status": "completed", "source_fp": fp, "result": {"clips": []}}),
        ]
        for job_id, job in cases:
            monkeypatch.setitem(app_module.jobs, job_id, job)
            app_module._record_source_history(job_id)
        assert sh.load(_index(tmp_path)) == []

        monkeypatch.setitem(app_module.jobs, "good", {
            "status": "completed", "source_fp": fp, "result": {"clips": [1, 2, 3]}})
        app_module._record_source_history("good")
        assert [e["clip_count"] for e in sh.load(_index(tmp_path))] == [3]

    def test_a_bookkeeping_failure_does_not_fail_the_job(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path))
        monkeypatch.setitem(app_module.jobs, "boom", {
            "status": "completed", "source_fp": sh.fingerprint(**FP),
            "result": {"clips": [1]}})

        def explode(*a, **k):
            raise OSError("disk gone")
        monkeypatch.setattr(sh, "record", explode)
        app_module._record_source_history("boom")   # must not raise


class TestSeedingFromDisk:
    def test_record_many_is_idempotent_and_writes_once(self, tmp_path):
        idx = _index(tmp_path)
        items = [(sh.fingerprint(title=f"vid {i}.mp4"), f"j{i}", i + 1, 1000 + i)
                 for i in range(3)]
        assert sh.record_many(idx, items) == 3
        assert sh.record_many(idx, items) == 0          # nothing new the 2nd time
        assert len(sh.load(idx)) == 3

    def test_seeding_never_overwrites_a_real_run_with_an_older_one(self, tmp_path):
        idx = _index(tmp_path)
        sh.record(idx, sh.fingerprint(**FP), "the-real-run", 12)
        seeded = sh.record_many(idx, [(sh.fingerprint(**FP), "older", 3, 1)])
        assert seeded == 0
        assert [m["job_id"] for m in sh.find(idx, sh.fingerprint(**FP))] == ["the-real-run"]

    def test_recovery_seeds_the_history_from_a_job_dir_on_disk(self, tmp_path, monkeypatch):
        """A restart is where this has to happen: the index would otherwise be
        empty and the first re-upload of an already-cut video would pass."""
        job_id = "11111111-2222-3333-4444-555555555555"
        job_dir = tmp_path / "output" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / f"{job_id}_My Show Episode 4_metadata.json").write_text(
            json.dumps({"source_video": f"{job_id}_My Show Episode 4.mp4",
                        "shorts": [{"start": 0, "end": 12}, {"start": 20, "end": 32}]}),
            encoding="utf-8")

        monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path / "output"))
        monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
        monkeypatch.setattr(app_module, "jobs", {})
        app_module._recover_jobs_from_disk()

        found = sh.find(str(tmp_path / "output" / ".sources.json"),
                        sh.fingerprint(title="My Show Episode 4.mp4"))
        assert [(m["job_id"], m["clip_count"]) for m in found] == [(job_id, 2)]
