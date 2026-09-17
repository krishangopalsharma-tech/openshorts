"""HTTP layer for Movie Recap: ``/api/film/*`` (sessions, voices) and
``/api/movierecap/*``. Thin: every rule lives in movierecap / film_prep /
film_render / film_voice; this file moves JSON.

Self-host only. Like ``/api/process/local``, a session names two paths on
the server's own disk (a 4 GB film should not be uploaded to the machine it
already sits on), so on a hosted instance every route here is a 404.

Sessions live under ``output/film/<uuid>/session.json`` and are NOT swept by
the hourly job cleanup: a recap is built over several sittings. Rendered
parts are served at ``/film/<session>/<file>`` through the same media guard
as ``/videos``.

Render work runs in a thread per request and reports through the session
file (``renders[key]``: status, logs, output), so a poll landing on either
side of a restart reads the same thing.

The Movie Shorts routes lived here until 17-sep-2026; see
docs/film-modules-plan.md for why they went.
"""

import json
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import film_prep as fp
import film_render
import film_voice
import movierecap as mr

router = APIRouter()

FILM_SUBDIR = "film"
_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
_LOCK = threading.Lock()
_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".ts"}

RECAP_DEFAULTS = {
    "target_seconds": mr.PART_TARGET_SECONDS, "speed": mr.SPEED, "ratio": "9:16",
    "fps": film_render.DEFAULT_FPS, "source_license": "unlicensed",
    # Generated voiceover (film_voice): a Kokoro voice id and its reading speed.
    "voice": film_voice.DEFAULT_VOICE, "tts_speed": film_voice.DEFAULT_SPEED,
    # Narrator register for Pass C: "story" (from inside the moment, in the
    # part's mood) or "essay" (the original video-essay voice).
    "narration_style": "story",
}
_RATIO_TO_FORMAT = {"9:16": "vertical", "1:1": "square", "vertical": "vertical", "square": "square"}


# --- plumbing ---------------------------------------------------------------

def _app():
    import app as _app_module  # lazy: app imports this module
    return _app_module


def _guard():
    if _app().BILLING_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")


def film_root():
    root = os.path.join(_app().OUTPUT_DIR, FILM_SUBDIR)
    os.makedirs(root, exist_ok=True)
    return root


def _session_dir(sid):
    if not _SESSION_RE.match(sid or ""):
        raise HTTPException(status_code=404, detail="Unknown session")
    return os.path.join(film_root(), sid)


def _load(sid):
    path = os.path.join(_session_dir(sid), "session.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        raise HTTPException(status_code=404, detail="Unknown session")
    except ValueError:
        raise HTTPException(status_code=500, detail="Session file is corrupt")


def _save(sess):
    d = _session_dir(sess["id"])
    os.makedirs(d, exist_ok=True)
    sess["updated"] = time.time()
    tmp = os.path.join(d, "session.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sess, fh, indent=1)
    os.replace(tmp, os.path.join(d, "session.json"))
    return sess


def _update(sid, fn):
    """Read-modify-write under one lock so a render thread's log line and a
    user's edit never overwrite each other."""
    with _LOCK:
        sess = _load(sid)
        fn(sess)
        _save(sess)
        return sess


def _cues(sess):
    try:
        cues = fp.parse_subtitles(sess["subtitle_path"])
    except (fp.SubtitleError, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"subtitles unreadable: {exc}")
    return fp.shift_cues(cues, float(sess.get("offset") or 0.0))


def _probe_duration(path):
    import music
    return music.probe_duration(path)


def _public(sess):
    return dict(sess)


async def _body(request):
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON")


def _pasted(body):
    """Accept ``{"json": {...}}``, ``{"text": "..."}`` or the object itself.
    A ``force`` key beside ``text``/``json`` is stripped first; read it with
    ``_force(body)`` before calling this."""
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k != "force"}
    if isinstance(body, dict) and "text" in body and len(body) == 1:
        try:
            return mr.parse_json(body["text"])
        except mr.PlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(body, dict) and "json" in body and len(body) == 1:
        return body["json"]
    return body


def _force(body):
    """The user chose to keep a plan the validator rejected. Schema errors
    can never be forced (there is nothing to render), rule violations can."""
    return isinstance(body, dict) and bool(body.get("force"))


def _check_local_file(path, kind, exts):
    path = str(path or "").strip().strip('"')
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"{kind} not found: {path or '(empty)'}")
    if os.path.splitext(path)[1].lower() not in exts:
        raise HTTPException(status_code=400, detail=f"{kind} must be one of {sorted(exts)}")
    return os.path.abspath(path)


def _log(sid, key, line):
    def fn(s):
        r = s.setdefault("renders", {}).setdefault(key, {})
        r.setdefault("logs", []).append(f"[{time.strftime('%H:%M:%S')}] {line}")
        r["logs"] = r["logs"][-200:]
    _update(sid, fn)


def _set_render(sid, key, **fields):
    def fn(s):
        s.setdefault("renders", {}).setdefault(key, {}).update(fields)
    _update(sid, fn)


def _start_thread(target, *args):
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    return t


# --- sessions ---------------------------------------------------------------

class SessionRequest(BaseModel):
    kind: str = "recap"
    video_path: str
    subtitle_path: str
    settings: Dict[str, Any] = {}
    probe_offset: bool = True
    title: Optional[str] = None


@router.post("/api/film/session")
async def create_session(req: SessionRequest):
    _guard()
    if req.kind != "recap":
        raise HTTPException(status_code=400, detail="kind must be recap (movie shorts go through the clip maker)")
    video = _check_local_file(req.video_path, "video", _VIDEO_EXTS)
    subs = _check_local_file(req.subtitle_path, "subtitle file", fp.SUBTITLE_EXTENSIONS)
    try:
        cues = fp.parse_subtitles(subs)
    except fp.SubtitleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    duration = _probe_duration(video) or fp.transcript_duration(fp.cues_to_transcript(cues))
    settings = {**RECAP_DEFAULTS, **{k: v for k, v in (req.settings or {}).items() if k in RECAP_DEFAULTS}}
    sess = {
        "id": uuid.uuid4().hex, "kind": "recap", "created": time.time(),
        "title": req.title or os.path.splitext(os.path.basename(video))[0],
        "video_path": video, "subtitle_path": subs,
        "duration": round(float(duration), 2), "cue_count": len(cues),
        "offset": 0.0, "offset_probe": None, "settings": settings, "renders": {},
        "recap": {"spoilers": None, "manual_excluded": [], "protected": None,
                  "structure": None, "plans": {}, "budget": None},
    }
    _save(sess)
    if req.probe_offset:
        _start_thread(_probe_offset_job, sess["id"])
    return _public(sess)


def _probe_offset_job(sid):
    sess = _load(sid)
    cues = fp.parse_subtitles(sess["subtitle_path"])
    result = fp.probe_offset(sess["video_path"], cues, sess["duration"])

    def fn(s):
        s["offset_probe"] = result
        if result.get("confidence", 0) >= 0.5 and result.get("matches", 0) >= 8 and not s.get("offset_set_by_user"):
            s["offset"] = result["offset"]
    _update(sid, fn)


@router.get("/api/film/sessions")
async def list_sessions():
    _guard()
    out = []
    root = film_root()
    for name in sorted(os.listdir(root)):
        if not _SESSION_RE.match(name):
            continue
        try:
            with open(os.path.join(root, name, "session.json"), encoding="utf-8") as fh:
                s = json.load(fh)
        except (OSError, ValueError):
            continue
        if s.get("kind") != "recap":
            continue
        out.append({"id": s["id"], "kind": s["kind"], "title": s.get("title"),
                    "duration": s.get("duration"), "created": s.get("created"),
                    "updated": s.get("updated"),
                    "parts_planned": len((s.get("recap") or {}).get("plans") or {}),
                    "renders": len(s.get("renders") or {})})
    out.sort(key=lambda s: s.get("updated") or 0, reverse=True)
    return {"sessions": out}


@router.get("/api/film/session/{sid}")
async def get_session(sid: str):
    _guard()
    return _public(_load(sid))


@router.delete("/api/film/session/{sid}")
async def delete_session(sid: str):
    _guard()
    d = _session_dir(sid)
    if not os.path.isdir(d):
        raise HTTPException(status_code=404, detail="Unknown session")
    shutil.rmtree(d, ignore_errors=True)
    return {"deleted": sid}


@router.post("/api/film/session/{sid}/offset")
async def set_offset(sid: str, request: Request):
    _guard()
    body = await _body(request)
    try:
        offset = float(body.get("offset", 0.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="offset must be a number of seconds")
    if abs(offset) > 600:
        raise HTTPException(status_code=400, detail="offset must be within ±600 s")

    def fn(s):
        s["offset"] = round(offset, 3)
        s["offset_set_by_user"] = True
    return _public(_update(sid, fn))


@router.post("/api/film/session/{sid}/settings")
async def set_settings(sid: str, request: Request):
    _guard()
    body = await _body(request)
    clean = {k: v for k, v in body.items() if k in RECAP_DEFAULTS}
    if "ratio" in clean and clean["ratio"] not in _RATIO_TO_FORMAT:
        raise HTTPException(status_code=400, detail="ratio must be 9:16 or 1:1")
    return _public(_update(sid, lambda s: s["settings"].update(clean)))


@router.get("/api/film/digest/{sid}")
async def get_digest(sid: str, start: Optional[float] = None, end: Optional[float] = None):
    _guard()
    sess = _load(sid)
    text = fp.digest(_cues(sess), start=start, end=end)
    return {"digest": text, "words": fp.digest_word_count(text)}


@router.get("/api/film/status/{sid}")
async def status(sid: str):
    _guard()
    sess = _load(sid)
    return {"id": sid, "renders": sess.get("renders", {}), "offset": sess.get("offset"),
            "offset_probe": sess.get("offset_probe")}


# --- movie recap ------------------------------------------------------------

def _recap(sess):
    return sess["recap"]


def _protected(sess):
    rc = _recap(sess)
    return mr.union_protected(rc.get("spoilers") or {}, sess["duration"], manual=rc.get("manual_excluded") or [])


def _part(sess, index):
    rc = _recap(sess)
    st = rc.get("structure")
    if not st:
        raise HTTPException(status_code=400, detail="paste the pass-B structure first")
    for p in st["parts"]:
        if int(p["index"]) == int(index):
            return mr.Part.model_validate(p)
    raise HTTPException(status_code=404, detail=f"part {index} is not in the structure")


@router.get("/api/movierecap/prompt/{sid}")
async def recap_prompt(sid: str, request: Request, part: Optional[int] = None):
    _guard()
    which = request.query_params.get("pass", "a")
    sess = _load(sid)
    rc = _recap(sess)
    cues = _cues(sess)
    protected = _protected(sess)
    if which == "a":
        text = mr.build_prompt_a(cues, sess["duration"])
    elif which == "b":
        if not rc.get("spoilers"):
            raise HTTPException(status_code=400, detail="paste the pass-A spoiler map first")
        text = mr.build_prompt_b(cues, sess["duration"], rc["spoilers"], protected,
                                 target=float(sess["settings"]["target_seconds"]))
    elif which == "c":
        if part is None:
            raise HTTPException(status_code=400, detail="pass=c needs ?part=N")
        text = mr.build_prompt_c(cues, sess["duration"], _part(sess, part), protected,
                                 target=float(sess["settings"]["target_seconds"]), speed=float(sess["settings"]["speed"]),
                                 style=sess["settings"].get("narration_style", "story"))
    else:
        raise HTTPException(status_code=400, detail="pass must be a, b or c")
    if request.query_params.get("format") == "text":
        return PlainTextResponse(text)
    return {"pass": which, "part": part, "prompt": text, "words": len(text.split()),
            "protected": protected, "wall": mr.wall_seconds(sess["duration"])}


@router.post("/api/movierecap/spoilers/{sid}")
async def recap_spoilers(sid: str, request: Request):
    _guard()
    data = _pasted(await _body(request))
    sess = _load(sid)
    sm, errors = mr.validate_spoilers(data, sess["duration"])
    if sm is None:
        return {"ok": False, "errors": errors, "errors_text": mr.format_errors(errors)}
    if not errors:
        def fn(s):
            s["recap"]["spoilers"] = sm.model_dump()
            s["recap"]["protected"] = mr.union_protected(sm.model_dump(), s["duration"], s["recap"].get("manual_excluded"))
            s["recap"]["structure"] = None
            s["recap"]["plans"] = {}
        sess = _update(sid, fn)
    return {"ok": not errors, "errors": errors, "errors_text": mr.format_errors(errors),
            "protected": sess["recap"].get("protected"), "wall": mr.wall_seconds(sess["duration"])}


@router.post("/api/movierecap/exclusions/{sid}")
async def recap_exclusions(sid: str, request: Request):
    _guard()
    body = await _body(request)
    ranges = body.get("ranges")
    if not isinstance(ranges, list):
        raise HTTPException(status_code=400, detail="ranges must be a list of {start, end}")
    sess = _load(sid)
    clean = mr.normalize_ranges(ranges, sess["duration"])

    def fn(s):
        s["recap"]["manual_excluded"] = clean
        s["recap"]["protected"] = mr.union_protected(s["recap"].get("spoilers") or {}, s["duration"], clean)
    sess = _update(sid, fn)
    return {"manual_excluded": clean, "protected": sess["recap"]["protected"]}


@router.post("/api/movierecap/structure/{sid}")
async def recap_structure(sid: str, request: Request):
    _guard()
    data = _pasted(await _body(request))
    sess = _load(sid)
    st, errors = mr.validate_structure(data, sess["duration"], _protected(sess))
    if st is None:
        return {"ok": False, "errors": errors, "errors_text": mr.format_errors(errors)}
    if not errors:
        def fn(s):
            s["recap"]["structure"] = st.model_dump()
            s["recap"]["plans"] = {}
        _update(sid, fn)
    return {"ok": not errors, "errors": errors, "errors_text": mr.format_errors(errors),
            "structure": st.model_dump()}


@router.post("/api/movierecap/plan/{sid}/{part}")
async def recap_plan(sid: str, part: int, request: Request):
    _guard()
    body = await _body(request)
    force = _force(body)
    data = _pasted(body)
    sess = _load(sid)
    p = _part(sess, part)
    protected = _protected(sess)
    plan, errors, warnings = mr.validate_plan(data, p, sess["duration"], protected,
                                              target=float(sess["settings"]["target_seconds"]),
                                              speed=float(sess["settings"]["speed"]),
                                              style=sess["settings"].get("narration_style", "story"))
    if plan is None:
        return {"ok": False, "errors": errors, "warnings": [], "errors_text": mr.format_errors(errors),
                "forceable": False}
    forced = bool(errors) and force
    if forced:
        # Forcing never overrides the spoiler wall: a chunk inside a protected
        # range or past 75% is dropped, not rendered. That rule is the module.
        wall = mr.wall_seconds(sess["duration"])
        kept = [c for c in plan.chunks
                if c.end > c.start and c.end <= wall + 1e-6
                and not any(r["tier"] != "wall" and c.start < r["end"] and c.end > r["start"] for r in protected)]
        if not kept:
            raise HTTPException(status_code=400, detail="every chunk is protected or past the wall; nothing to render")
        plan = mr.PartPlan(index=plan.index, target_duration=plan.target_duration,
                           chunks=sorted(kept, key=lambda c: c.start),
                           closing_lines=plan.closing_lines, spoiler_self_check=plan.spoiler_self_check)
    if not errors or forced:
        def fn(s):
            entry = plan.model_dump()
            if forced:
                entry["forced"] = True
                entry["ignored_errors"] = errors
            s["recap"]["plans"][str(part)] = entry
        _update(sid, fn)
    return {"ok": not errors or forced, "forced": forced, "errors": errors, "warnings": warnings,
            "errors_text": mr.format_errors(errors), "warnings_text": mr.format_errors(warnings),
            "forceable": True, "kept_chunks": len(plan.chunks)}


@router.get("/api/movierecap/budget/{sid}")
async def recap_budget(sid: str):
    _guard()
    sess = _load(sid)
    rc = _recap(sess)
    plans = {int(k): mr.PartPlan.model_validate(v) for k, v in (rc.get("plans") or {}).items()}
    structure = mr.Structure.model_validate(rc["structure"]) if rc.get("structure") else None
    return mr.budget(plans, structure, sess["duration"], _protected(sess), speed=float(sess["settings"]["speed"]))


def _render_recap_silent_job(sid, part):
    key = f"recap-{part}-silent"
    try:
        sess = _load(sid)
        plan = mr.PartPlan.model_validate(sess["recap"]["plans"][str(part)])
        st = sess["settings"]
        out_dir = _session_dir(sid)
        work = os.path.join(out_dir, f"film_work_{key}")
        out_name = f"recap_part{part}_silent_{int(time.time())}.mp4"
        out_path = os.path.join(out_dir, out_name)
        shots = [{"start": c.start, "end": c.end, "hflip": c.flip} for c in plan.chunks]
        report = film_render.render_silent(
            sess["video_path"], shots, out_path, work, speed=float(st["speed"]),
            output_format=_RATIO_TO_FORMAT.get(st.get("ratio"), "vertical"),
            fps=int(st.get("fps") or 60), mute=True, log=lambda line: _log(sid, key, line))
        shutil.rmtree(work, ignore_errors=True)
        _set_render(sid, key, status="completed", output=f"/film/{sid}/{out_name}", file=out_name,
                    report=report, finished=time.time())
    except Exception as exc:  # noqa: BLE001
        _log(sid, key, f"failed: {exc}")
        _set_render(sid, key, status="failed", error=str(exc)[:500], finished=time.time())


@router.post("/api/movierecap/render/{sid}")
async def recap_render(sid: str, request: Request):
    """Silent parts (no voiceover yet), to check the cut before narrating."""
    _guard()
    body = await _body(request)
    sess = _load(sid)
    rc = _recap(sess)
    parts = body.get("parts") or sorted(int(k) for k in (rc.get("plans") or {}))
    if not parts:
        raise HTTPException(status_code=400, detail="no validated part plans to render")
    started = []
    for part in parts:
        if str(part) not in (rc.get("plans") or {}):
            raise HTTPException(status_code=400, detail=f"part {part} has no validated plan")
        key = f"recap-{part}-silent"
        if (sess.get("renders") or {}).get(key, {}).get("status") == "running":
            continue
        _set_render(sid, key, status="running", logs=[], started=time.time(), output=None, error=None)
        _start_thread(_render_recap_silent_job, sid, int(part))
        started.append(key)
    return {"renders": started}


def _render_recap_vo_job(sid, part, vo_path):
    key = f"recap-{part}"
    try:
        import compilation
        sess = _load(sid)
        plan = mr.PartPlan.model_validate(sess["recap"]["plans"][str(part)])
        st = sess["settings"]
        speed = float(st["speed"])
        protected = _protected(sess)
        out_dir = _session_dir(sid)
        work = os.path.join(out_dir, f"film_work_{key}")
        os.makedirs(work, exist_ok=True)
        _log(sid, key, "🎙️  Transcribing the voiceover")
        words = compilation.transcribe_vo(vo_path)
        if not words:
            raise film_render.RenderError("whisper found no words in the voiceover")
        measured = words[-1]["e"]
        check = mr.vo_duration_check(measured, target=float(st["target_seconds"]))
        _set_render(sid, key, vo_check=check)
        if not check["ok"]:
            raise film_render.RenderError(check["message"])
        cplan = mr.compilation_plan(plan, protected, sess["duration"], speed=speed)
        _log(sid, key, f"🧭 Aligning {len(cplan['vo']['lines'])} lines to {len(words)} words")
        aligned = compilation.align_lines_to_words(cplan, words)
        chunks = compilation.pad_and_split(vo_path, aligned, cplan["vo"]["edge_guard"], work)
        fitted, total = compilation.fit_shots(cplan, chunks)
        segments = mr.fitted_to_source(fitted, speed=speed)
        out_name = f"recap_part{part}_{int(time.time())}.mp4"
        out_path = os.path.join(out_dir, out_name)
        report = film_render.render_narrated(
            sess["video_path"], segments, out_path, work, speed=speed,
            output_format=_RATIO_TO_FORMAT.get(st.get("ratio"), "vertical"),
            fps=int(st.get("fps") or 60), log=lambda line: _log(sid, key, line))
        shutil.rmtree(work, ignore_errors=True)
        _set_render(sid, key, status="completed", output=f"/film/{sid}/{out_name}", file=out_name,
                    base_file=out_name, overlays=[], caption_style=None,
                    narration_cues=_narration_cues(plan, segments),
                    report=report, finished=time.time(),
                    segments=[{k: v for k, v in s.items() if k != "chunk"} for s in segments])
    except Exception as exc:  # noqa: BLE001
        _log(sid, key, f"failed: {exc}")
        _set_render(sid, key, status="failed", error=str(exc)[:500], finished=time.time())


def _narration_cues(plan, segments):
    """The spoken lines on the FINISHED timeline, as cues: what the caption
    layer renders. One cue per narrated chunk, from its placement."""
    cues = []
    for chunk, seg in zip(plan.chunks, segments):
        text = " ".join(chunk.narration.split())
        if not text or seg.get("voice_start") is None:
            continue
        dur = float((seg.get("chunk") or {}).get("duration") or 0.0)
        if dur <= 0:
            dur = max(0.5, float(seg.get("shot_length", 0.0)) - 0.4)
        cues.append({"start": round(float(seg["voice_start"]), 3),
                     "end": round(float(seg["voice_start"]) + dur, 3), "text": text})
    return cues


@router.post("/api/movierecap/voiceover/{sid}/{part}")
async def recap_voiceover(sid: str, part: int, file: UploadFile = File(...)):
    _guard()
    sess = _load(sid)
    rc = _recap(sess)
    if str(part) not in (rc.get("plans") or {}):
        raise HTTPException(status_code=400, detail=f"part {part} has no validated plan")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"):
        raise HTTPException(status_code=400, detail="voiceover must be an audio file")
    key = f"recap-{part}"
    if (sess.get("renders") or {}).get(key, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="this part is already rendering")
    vo_path = os.path.join(_session_dir(sid), f"vo_part{part}{ext}")
    with open(vo_path, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    _set_render(sid, key, status="running", logs=[], started=time.time(), output=None, error=None,
                voiceover=os.path.basename(vo_path))
    _start_thread(_render_recap_vo_job, sid, int(part), vo_path)
    return {"render": key, "status": "running"}


# --- generated voiceover (Kokoro, film_voice) --------------------------------

@router.get("/api/film/voices")
async def film_voices():
    """Kokoro health plus the voice list grouped by language. Offline is a
    state with the start command, never a 5xx."""
    _guard()
    import asyncio
    loop = asyncio.get_running_loop()
    status = await loop.run_in_executor(None, film_voice.health)
    groups = {}
    if status["online"]:
        for v in film_voice.voices():
            groups.setdefault(v["language"], []).append(v)
    return {**status, "groups": groups, "default": film_voice.DEFAULT_VOICE,
            "speed_range": list(film_voice.SPEED_RANGE)}


def _voice_settings(sess, body):
    st = sess["settings"]
    voice = str(body.get("voice") or st.get("voice") or film_voice.DEFAULT_VOICE)
    if not re.match(r"^[a-z]{2}_[a-z0-9_]+$", voice):
        raise HTTPException(status_code=400, detail="voice must be a Kokoro voice id like am_michael")
    try:
        speed = float(body.get("speed", st.get("tts_speed", film_voice.DEFAULT_SPEED)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="speed must be a number")
    lo, hi = film_voice.SPEED_RANGE
    return voice, min(hi, max(lo, speed))


@router.post("/api/movierecap/narrate/{sid}/{part}/preview")
async def recap_narrate_preview(sid: str, part: int, request: Request):
    """The part's first line in the chosen voice, to pick a narrator by ear."""
    _guard()
    body = await _body(request)
    sess = _load(sid)
    rc = _recap(sess)
    plan = (rc.get("plans") or {}).get(str(part))
    if not plan:
        raise HTTPException(status_code=400, detail=f"part {part} has no validated plan")
    voice, speed = _voice_settings(sess, body)
    line = next((c["narration"] for c in plan["chunks"] if str(c.get("narration", "")).strip()), None)
    if not line:
        raise HTTPException(status_code=400, detail="the plan has no narration to preview")
    name = f"preview_part{part}_{voice}_{int(speed * 100)}.wav"
    out = os.path.join(_session_dir(sid), name)
    import asyncio
    loop = asyncio.get_running_loop()
    try:
        got = await loop.run_in_executor(None, lambda: film_voice.synthesize(line, voice, speed, out))
    except film_voice.VoiceOffline as exc:
        raise HTTPException(status_code=503, detail=f"Kokoro is offline ({exc}). Start it: {film_voice.START_COMMAND}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"url": f"/film/{sid}/{name}", "duration": got["duration"], "voice": voice,
            "speed": got["speed"], "text": line}


def _render_recap_tts_job(sid, part, voice, speed):
    key = f"recap-{part}"
    try:
        sess = _load(sid)
        plan = mr.PartPlan.model_validate(sess["recap"]["plans"][str(part)])
        st = sess["settings"]
        speed_video = float(st["speed"])
        protected = _protected(sess)
        out_dir = _session_dir(sid)
        vo_dir = os.path.join(out_dir, f"vo_part{part}_{voice}")
        work = os.path.join(out_dir, f"film_work_{key}")
        os.makedirs(work, exist_ok=True)
        # Synthesis first, on the CPU, before any ffmpeg or GPU work starts.
        _log(sid, key, f"🗣️  Kokoro voice {voice} at {speed}x")
        narrated = film_voice.narrate_plan(plan, part, protected, sess["duration"], voice, speed, vo_dir,
                                           speed_video=speed_video, log=lambda line: _log(sid, key, line))
        check = mr.vo_duration_check(narrated["total_finished"], target=float(st["target_seconds"]))
        _set_render(sid, key, fit=narrated["report"], overruns=narrated["overruns"],
                    vo_check={**check, "ok": True}, voice=voice, tts_speed=speed,
                    words=narrated["words"])
        if narrated["overruns"]:
            _log(sid, key, f"⚠️  {narrated['overruns']} line(s) overrun their footage; see the fit table")
        report = film_render.render_narrated(
            sess["video_path"], narrated["segments"], os.path.join(out_dir, f"recap_part{part}_{int(time.time())}.mp4"),
            work, speed=speed_video, output_format=_RATIO_TO_FORMAT.get(st.get("ratio"), "vertical"),
            fps=int(st.get("fps") or 60), log=lambda line: _log(sid, key, line))
        shutil.rmtree(work, ignore_errors=True)
        out_name = os.path.basename(report["output"])
        _set_render(sid, key, status="completed", output=f"/film/{sid}/{out_name}", file=out_name,
                    base_file=out_name, overlays=[], caption_style=None,
                    narration_cues=_narration_cues(plan, narrated["segments"]),
                    report=report, finished=time.time(), generated=True,
                    segments=[{k: v for k, v in s.items() if k != "chunk"} for s in narrated["segments"]])
    except film_voice.VoiceOffline as exc:
        _log(sid, key, f"Kokoro offline: {exc}")
        _set_render(sid, key, status="failed", error=f"Kokoro is offline ({exc}). Start it: {film_voice.START_COMMAND}",
                    finished=time.time())
    except Exception as exc:  # noqa: BLE001
        _log(sid, key, f"failed: {exc}")
        _set_render(sid, key, status="failed", error=str(exc)[:500], finished=time.time())


@router.post("/api/movierecap/narrate/{sid}/{part}")
async def recap_narrate(sid: str, part: int, request: Request):
    """Generate the voiceover from the plan's narration and render the part."""
    _guard()
    body = await _body(request)
    sess = _load(sid)
    rc = _recap(sess)
    if str(part) not in (rc.get("plans") or {}):
        raise HTTPException(status_code=400, detail=f"part {part} has no validated plan")
    voice, speed = _voice_settings(sess, body)
    key = f"recap-{part}"
    if (sess.get("renders") or {}).get(key, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="this part is already rendering")
    status = film_voice.health()
    if not status["online"]:
        raise HTTPException(status_code=503, detail=f"Kokoro is offline. Start it: {status['start_command']}")

    def fn(s):
        s["settings"]["voice"] = voice
        s["settings"]["tts_speed"] = speed
        s.setdefault("renders", {})[key] = {"status": "running", "logs": [], "started": time.time(),
                                            "output": None, "error": None, "voice": voice, "tts_speed": speed}
    _update(sid, fn)
    _start_thread(_render_recap_tts_job, sid, int(part), voice, speed)
    return {"render": key, "status": "running", "voice": voice, "speed": speed}


@router.post("/api/film/voices/start")
async def film_voices_start():
    """Start the Kokoro server from KOKORO_HOME (CPU) and wait for it. The
    same happens by itself on the first narrate/preview when it is down."""
    _guard()
    import asyncio
    loop = asyncio.get_running_loop()
    status = await loop.run_in_executor(None, film_voice.ensure_server)
    groups = {}
    if status.get("online"):
        for v in film_voice.voices():
            groups.setdefault(v["language"], []).append(v)
    return {**status, "groups": groups, "default": film_voice.DEFAULT_VOICE,
            "speed_range": list(film_voice.SPEED_RANGE)}


# --- captions and overlays on a narrated part --------------------------------
#
# Same tools as a clip (SubtitleModal -> caption_styles, OverlayEditor ->
# overlays.py), same layer order (overlays under, captions on top), same
# fail-open contract. The narrated render is the base and is never touched;
# every change strips back to it and re-derives, so a caption restyle never
# re-composites logos twice.

def _part_render(sess, part):
    r = (sess.get("renders") or {}).get(f"recap-{part}")
    if not r or r.get("status") != "completed" or not r.get("base_file"):
        raise HTTPException(status_code=400, detail=f"part {part} has no narrated render yet")
    return r


def _part_transcript(render):
    cues = render.get("narration_cues") or []
    return fp.cues_to_transcript(cues), (cues[-1]["end"] + 1.0 if cues else 0.0)


def _words_transcript(words):
    """Edited caption words from the modal (clip-relative ms) -> transcript."""
    edited = []
    for w in words or []:
        try:
            text, s, e = str(w["text"]).strip(), float(w["startMs"]) / 1000.0, float(w["endMs"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        if text and e > s >= 0:
            edited.append({"word": " " + text, "start": s, "end": e})
    if not edited:
        return None, 0.0
    edited.sort(key=lambda w: w["start"])
    return ({"language": "en", "segments": [{"start": edited[0]["start"], "end": edited[-1]["end"],
                                             "text": " ".join(w["word"] for w in edited), "words": edited}]},
            edited[-1]["end"] + 1.0)


def _relayer_part(sid, part):
    """Re-derive overlays then captions over the narrated base; returns the
    served filename. Old derivatives of this part are removed."""
    import overlays as _ov
    sess = _load(sid)
    r = _part_render(sess, part)
    out_dir = _session_dir(sid)
    base = os.path.join(out_dir, r["base_file"])
    if not os.path.exists(base):
        raise HTTPException(status_code=409, detail="the narrated render is gone; generate it again")
    stale = [f for f in os.listdir(out_dir)
             if f.endswith(r["base_file"]) and f != r["base_file"] and (f.startswith("ov_") or f.startswith("subtitled_"))]
    current = base
    items = _ov.normalize(r.get("overlays") or [])
    if items:
        ov_path = os.path.join(out_dir, f"ov_{uuid.uuid4().hex[:6]}_{r['base_file']}")
        if _ov.apply_overlays(current, items, ov_path):
            current = ov_path
    style = r.get("caption_style")
    if style:
        if style.get("words"):
            transcript, end = _words_transcript(style["words"])
        else:
            transcript, end = _part_transcript(r)
        if transcript and transcript.get("segments"):
            burned = _app()._burn_styled_captions(current, transcript, 0.0, end,
                                                   {"preset": style.get("preset"), "overrides": style.get("overrides") or {}},
                                                   split_ranges=[])
            if burned:
                current = burned
    for f in stale:
        if os.path.join(out_dir, f) != current:
            try:
                os.remove(os.path.join(out_dir, f))
            except OSError:
                pass
    name = os.path.basename(current)
    _set_render(sid, f"recap-{part}", file=name, output=f"/film/{sid}/{name}")
    return name


@router.get("/api/movierecap/part/{sid}/{part}/transcript")
async def recap_part_transcript(sid: str, part: int):
    """The narration as caption words, in the shape SubtitleModal reads for
    a clip (``captions`` with ms, ``durationSec``)."""
    _guard()
    r = _part_render(_load(sid), part)
    transcript, _end = _part_transcript(r)
    captions = [{"text": w["word"].strip(), "startMs": int(w["start"] * 1000), "endMs": int(w["end"] * 1000)}
                for seg in transcript["segments"] for w in seg["words"]]
    duration = float((r.get("report") or {}).get("duration") or (captions[-1]["endMs"] / 1000.0 if captions else 0.0))
    return {"captions": captions, "durationSec": duration, "language": "en"}


@router.post("/api/movierecap/part/{sid}/{part}/captions")
async def recap_part_captions(sid: str, part: int, request: Request):
    """Burn a caption look onto the narrated part (``preset`` + ``overrides``,
    optional edited ``words``); ``{"preset": null}`` removes the layer."""
    _guard()
    body = await _body(request)
    sess = _load(sid)
    _part_render(sess, part)
    import caption_styles
    preset = body.get("preset")
    style = None
    if preset:
        if preset not in caption_styles.STYLE_PRESETS:
            raise HTTPException(status_code=400, detail=f"unknown caption preset {preset}")
        words = body.get("words")
        if words is not None and (not isinstance(words, list) or len(words) > 2000):
            raise HTTPException(status_code=400, detail="words must be a list of at most 2000 items")
        style = {"preset": preset, "overrides": caption_styles.normalize_overrides(body.get("overrides") or {}),
                 "words": words}
    _update(sid, lambda s: s["renders"][f"recap-{part}"].update(caption_style=style))
    import asyncio
    loop = asyncio.get_running_loop()
    name = await loop.run_in_executor(None, _relayer_part, sid, part)
    return {"file": name, "output": f"/film/{sid}/{name}", "caption_style": style}


@router.post("/api/movierecap/part/{sid}/{part}/overlays")
async def recap_part_overlays(sid: str, part: int, request: Request):
    """Logos and text blocks on the narrated part, in fractions of the frame
    (overlays.normalize schema); ``[]`` removes the layer."""
    _guard()
    body = await _body(request)
    sess = _load(sid)
    _part_render(sess, part)
    import overlays as _ov
    items = _ov.normalize(body.get("overlays") or [])
    for item in items:
        if item["type"] == "image" and not _ov.resolve_asset(item["asset"]):
            raise HTTPException(status_code=400, detail=f"unknown overlay asset {item['asset']}")
    _update(sid, lambda s: s["renders"][f"recap-{part}"].update(overlays=items))
    import asyncio
    loop = asyncio.get_running_loop()
    name = await loop.run_in_executor(None, _relayer_part, sid, part)
    return {"file": name, "output": f"/film/{sid}/{name}", "overlays": items}
