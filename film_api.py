"""HTTP layer for the film modules: ``/api/film/*`` (sessions, library),
``/api/movieshorts/*`` and ``/api/movierecap/*``. Thin: every rule lives in
movieshorts / movierecap / film_prep / film_render; this file moves JSON.

Self-host only. Like ``/api/process/local``, a session names two paths on
the server's own disk (a 4 GB film should not be uploaded to the machine it
already sits on), so on a hosted instance every route here is a 404.

Sessions live under ``output/film/<uuid>/session.json`` and are NOT swept by
the hourly job cleanup: a film's hook list is the reusable asset and gets
built over several sittings. Rendered parts are served at
``/film/<session>/<file>`` through the same media guard as ``/videos``.

Render work runs in a thread per request and reports through the session
file (``renders[key]``: status, logs, output), so a poll landing on either
side of a restart reads the same thing.
"""

import json
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import beat_grid
import film_prep as fp
import film_render
import movierecap as mr
import movieshorts as ms
import music_library

router = APIRouter()

FILM_SUBDIR = "film"
_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
_LOCK = threading.Lock()
_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".ts"}

SHORTS_DEFAULTS = {
    "target_seconds": ms.DEFAULT_TARGET_SECONDS, "speed": ms.DEFAULT_SPEED,
    "hook_count": ms.DEFAULT_HOOK_COUNT, "ratio": "1:1", "fps": film_render.DEFAULT_FPS,
    "grade": "washed", "caption_style": "film_pop", "beat_sync": "beat",
    "strictness": "dialogue_first", "source_license": "unlicensed",
}
RECAP_DEFAULTS = {
    "target_seconds": mr.PART_TARGET_SECONDS, "speed": mr.SPEED, "ratio": "9:16",
    "fps": film_render.DEFAULT_FPS, "source_license": "unlicensed",
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
    """The session without the bulky per-hook shot lists."""
    out = {k: v for k, v in sess.items() if k != "shots"}
    out["shot_counts"] = {k: len(v) for k, v in (sess.get("shots") or {}).items()}
    return out


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
            return ms.parse_json(body["text"])
        except ms.PlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(body, dict) and "json" in body and len(body) == 1:
        return body["json"]
    return body


def _force(body):
    """The user chose to keep a plan the validator rejected. Schema errors
    can never be forced (there is nothing to render), rule violations can:
    captions are burned from the real cues regardless, and a short with 9
    beats is a shorter short, not a broken one."""
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
    kind: str = "shorts"                # shorts | recap
    video_path: str
    subtitle_path: str
    settings: Dict[str, Any] = {}
    probe_offset: bool = True
    title: Optional[str] = None


@router.post("/api/film/session")
async def create_session(req: SessionRequest):
    _guard()
    if req.kind not in ("shorts", "recap"):
        raise HTTPException(status_code=400, detail="kind must be shorts or recap")
    video = _check_local_file(req.video_path, "video", _VIDEO_EXTS)
    subs = _check_local_file(req.subtitle_path, "subtitle file", fp.SUBTITLE_EXTENSIONS)
    try:
        cues = fp.parse_subtitles(subs)
    except fp.SubtitleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    duration = _probe_duration(video) or fp.transcript_duration(fp.cues_to_transcript(cues))
    defaults = SHORTS_DEFAULTS if req.kind == "shorts" else RECAP_DEFAULTS
    settings = {**defaults, **{k: v for k, v in (req.settings or {}).items() if k in defaults}}
    sess = {
        "id": uuid.uuid4().hex, "kind": req.kind, "created": time.time(),
        "title": req.title or os.path.splitext(os.path.basename(video))[0],
        "video_path": video, "subtitle_path": subs,
        "duration": round(float(duration), 2), "cue_count": len(cues),
        "offset": 0.0, "offset_probe": None, "settings": settings,
        "hooks": None, "beats": {}, "shots": {}, "renders": {},
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
        out.append({"id": s["id"], "kind": s["kind"], "title": s.get("title"),
                    "duration": s.get("duration"), "created": s.get("created"),
                    "updated": s.get("updated"), "hooks": len((s.get("hooks") or {}).get("hooks", []) or []),
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
    sess = _load(sid)
    defaults = SHORTS_DEFAULTS if sess["kind"] == "shorts" else RECAP_DEFAULTS
    clean = {k: v for k, v in body.items() if k in defaults}
    if "ratio" in clean and clean["ratio"] not in _RATIO_TO_FORMAT:
        raise HTTPException(status_code=400, detail="ratio must be 9:16 or 1:1")
    if "grade" in clean and film_render.grade_chain(clean["grade"]) is None and clean["grade"] not in ("none", "neutral", None):
        raise HTTPException(status_code=400, detail=f"unknown grade {clean['grade']}")
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


# --- music library ----------------------------------------------------------

@router.get("/api/film/library")
async def library():
    _guard()
    return {**music_library.library_summary(), "recipes": music_library.MOOD_RECIPES,
            "tracks": [{k: v for k, v in t.items() if k not in ("beats", "energy")}
                       for t in music_library.load_manifest()["tracks"]]}


@router.post("/api/film/library/scan")
async def library_scan(request: Request):
    _guard()
    body = await _body(request)
    import asyncio
    loop = asyncio.get_running_loop()
    lines = []
    await loop.run_in_executor(None, lambda: music_library.scan(
        force=bool(body.get("force")), normalise=body.get("normalise", True), log=lines.append))
    return {**music_library.library_summary(), "log": lines[-100:]}


@router.get("/api/film/music-hint")
async def music_hint(mood: str = Query("")):
    _guard()
    return music_library.music_hint(mood)


@router.post("/api/film/library/upload")
async def library_upload(file: UploadFile = File(...), mood: str = Form("any"),
                         source: str = Form(""), licence: str = Form(""), url: str = Form(""),
                         attribution: str = Form(""), attribution_required: bool = Form(False)):
    """Drop one track into ``assets/music/<mood>/`` from the tab, with its
    licence recorded beside it, and analyse it into the manifest."""
    _guard()
    import asyncio
    meta = {"source": source, "licence": licence, "url": url, "attribution": attribution,
            "attribution_required": bool(attribution_required)}
    loop = asyncio.get_running_loop()
    try:
        entry = await loop.run_in_executor(
            None, lambda: music_library.upload_track(file.filename, file.file, mood, meta=meta))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"track": entry, **music_library.library_summary()}


# --- movie shorts -----------------------------------------------------------

def _hook(sess, hook_index):
    hooks = (sess.get("hooks") or {}).get("hooks") or []
    for h in hooks:
        if int(h.get("index", -1)) == int(hook_index):
            return ms.Hook.model_validate(h)
    raise HTTPException(status_code=404, detail=f"hook {hook_index} is not in this session")


@router.get("/api/movieshorts/prompt/{sid}")
async def shorts_prompt(sid: str, request: Request, hook: Optional[int] = None):
    _guard()
    which = request.query_params.get("pass", "hooks")
    sess = _load(sid)
    if sess["kind"] != "shorts":
        raise HTTPException(status_code=400, detail="not a movie-shorts session")
    cues = _cues(sess)
    st = sess["settings"]
    if which == "hooks":
        text = ms.build_hooks_prompt(cues, sess["duration"], hook_count=int(st["hook_count"]),
                                     target_seconds=float(st["target_seconds"]))
    elif which == "beats":
        if hook is None:
            raise HTTPException(status_code=400, detail="pass=beats needs ?hook=N")
        text = ms.build_beats_prompt(cues, _hook(sess, hook), target_seconds=float(st["target_seconds"]),
                                     speed=float(st["speed"]))
    else:
        raise HTTPException(status_code=400, detail="pass must be hooks or beats")
    if request.query_params.get("format") == "text":
        return PlainTextResponse(text)
    return {"pass": which, "hook": hook, "prompt": text, "words": len(text.split())}


@router.post("/api/movieshorts/hooks/{sid}")
async def shorts_hooks(sid: str, request: Request):
    _guard()
    data = _pasted(await _body(request))
    sess = _load(sid)
    plan, errors = ms.validate_hooks(data, sess["duration"])
    if plan is None:
        return {"ok": False, "errors": errors, "errors_text": ms.format_errors(errors)}
    _update(sid, lambda s: s.update(hooks=plan.model_dump(), beats={}, shots={}))
    return {"ok": not errors, "errors": errors, "errors_text": ms.format_errors(errors),
            "hooks": plan.model_dump()}


@router.post("/api/movieshorts/beats/{sid}/{hook}")
async def shorts_beats(sid: str, hook: int, request: Request):
    _guard()
    body = await _body(request)
    force = _force(body)
    data = _pasted(body)
    sess = _load(sid)
    h = _hook(sess, hook)
    st = sess["settings"]
    plan, errors = ms.validate_beats(data, h, _cues(sess), sess["duration"],
                                     target_seconds=float(st["target_seconds"]), speed=float(st["speed"]))
    if plan is None:
        return {"ok": False, "errors": errors, "errors_text": ms.format_errors(errors), "forceable": False}
    forced = bool(errors) and force
    if forced:
        # Keep what can be cut: a beat with no footage or outside the film
        # cannot render; everything else is the user's call.
        kept = [b for b in plan.beats if b.end > b.start and b.start >= 0 and b.end <= sess["duration"] + 0.5]
        if not kept:
            raise HTTPException(status_code=400, detail="no beat in this plan can be cut")
        plan = ms.BeatPlan(hook_index=plan.hook_index, beats=sorted(kept, key=lambda b: b.start))
    if not errors or forced:
        def fn(s):
            entry = plan.model_dump()
            if forced:
                entry["forced"] = True
                entry["ignored_errors"] = errors
            s.setdefault("beats", {})[str(hook)] = entry
            s.setdefault("shots", {}).pop(str(hook), None)
        _update(sid, fn)
    return {"ok": not errors or forced, "forced": forced, "errors": errors,
            "errors_text": ms.format_errors(errors), "forceable": True,
            "summary": ms.summarize(plan)}


def _pick_bed(sess, hook_obj, beats_plan, beat_sync):
    st = sess["settings"]
    duration = float(st["target_seconds"])
    return music_library.pick(hook_obj.mood, duration, beat_sync=(beat_sync == "beat"))


def _build_shots(sess, hook):
    h = _hook(sess, hook)
    plan = (sess.get("beats") or {}).get(str(hook))
    if not plan:
        raise HTTPException(status_code=400, detail=f"hook {hook} has no validated beat plan yet")
    st = sess["settings"]
    speed = float(st["speed"])
    beats = plan["beats"]
    bed = _pick_bed(sess, h, plan, st.get("beat_sync"))
    sched = None
    if bed and st.get("beat_sync") == "beat":
        words = fp.cues_to_transcript(_cues(sess))
        flat = [w for seg in words["segments"] for w in seg["words"]]
        sched = beat_grid.schedule(beats, bed["track"], speed=speed, words=flat,
                                   strictness=st.get("strictness", "dialogue_first"))
    schedule = sched["schedule"] if sched and sched["tier"] in ("beat", "energy") else None
    shots = fp.expand_plan(beats, speed=speed, schedule=schedule, seed=int(hook))
    return {
        "hook": hook, "shots": shots, "summary": fp.shots_summary(shots, speed),
        "music": (None if not bed else {**{k: v for k, v in bed.items() if k not in ("track", "reasons")},
                                        "track": {k: v for k, v in bed["track"].items() if k not in ("beats", "energy")}}),
        "rhythm": (sched["report"] if sched else {"tier": "free" if not bed else bed["tier"]}),
        "generated": time.time(),
    }


@router.get("/api/movieshorts/shots/{sid}/{hook}")
async def shorts_shots(sid: str, hook: int, rebuild: bool = False):
    _guard()
    sess = _load(sid)
    existing = (sess.get("shots") or {}).get(str(hook))
    if existing and not rebuild:
        return existing
    built = _build_shots(sess, hook)
    _update(sid, lambda s: s.setdefault("shots", {}).__setitem__(str(hook), built))
    return built


@router.post("/api/movieshorts/shots/{sid}/{hook}")
async def shorts_shots_save(sid: str, hook: int, request: Request):
    """Hand-edited shots from the preview: scale, anchor, blur, composite."""
    _guard()
    body = await _body(request)
    shots = body.get("shots")
    if not isinstance(shots, list) or not shots:
        raise HTTPException(status_code=400, detail="shots must be a non-empty list")
    sess = _load(sid)
    current = (sess.get("shots") or {}).get(str(hook))
    if not current:
        raise HTTPException(status_code=400, detail="build the shot plan first")
    by_id = {s["id"]: s for s in current["shots"]}
    for edit in shots:
        s = by_id.get(edit.get("id"))
        if not s:
            continue
        if "scale" in edit:
            s["scale"] = max(1.0, min(fp.SCALE_MAX, float(edit["scale"])))
        if "anchor" in edit and isinstance(edit["anchor"], (list, tuple)) and len(edit["anchor"]) == 2:
            s["anchor"] = [max(0.0, min(1.0, float(edit["anchor"][0]))), max(0.0, min(1.0, float(edit["anchor"][1])))]
        if "blur" in edit:
            s["blur"] = edit["blur"] if isinstance(edit["blur"], dict) else None
        if "composite" in edit:
            s["composite"] = bool(edit["composite"])
    current["summary"] = fp.shots_summary(current["shots"], float(sess["settings"]["speed"]))
    current["edited"] = time.time()
    _update(sid, lambda s: s["shots"].__setitem__(str(hook), current))
    return current


def _render_short_job(sid, hook, overrides):
    key = f"short-{hook}"
    try:
        sess = _load(sid)
        st = {**sess["settings"], **overrides}
        built = (sess.get("shots") or {}).get(str(hook)) or _build_shots(sess, hook)
        h = _hook(sess, hook)
        out_dir = _session_dir(sid)
        work = os.path.join(out_dir, f"film_work_{key}")
        safe_title = re.sub(r"[^\w-]+", "_", h.title)[:60].strip("_") or f"hook{hook}"
        out_name = f"short_{hook:02d}_{safe_title}_{int(time.time())}.mp4"
        out_path = os.path.join(out_dir, out_name)
        music_path = (built.get("music") or {}).get("path")
        report = film_render.render_short(
            sess["video_path"], built["shots"], out_path, work,
            speed=float(st["speed"]), output_format=_RATIO_TO_FORMAT.get(st.get("ratio"), "square"),
            grade=st.get("grade"), fps=int(st.get("fps") or 60), cues=_cues(sess),
            caption_preset=st.get("caption_style") or "film_pop",
            music_track=music_path if music_path and os.path.exists(music_path) else None,
            log=lambda line: _log(sid, key, line))
        if music_path:
            try:
                music_library.record_use(built["music"]["track"]["path"])
            except Exception:  # noqa: BLE001
                pass
        rhythm = None
        try:
            rhythm = film_render.measure_cut_rhythm(out_path)
            rhythm.pop("times", None)
            bed = built.get("music") or {}
            if bed.get("track") and bed["track"].get("bpm"):
                grid = music_library.load_manifest()["tracks"]
                entry = next((t for t in grid if t["path"] == bed["track"]["path"]), None)
                if entry and entry.get("beats"):
                    import numpy as np
                    cuts = np.array(film_render.measure_cut_rhythm(out_path)["times"])
                    rhythm["phase_lock"] = round(beat_grid.phase_lock(cuts, np.array(entry["beats"]),
                                                                      60.0 / entry["bpm"]), 3)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(work, ignore_errors=True)
        _set_render(sid, key, status="completed", output=f"/film/{sid}/{out_name}", file=out_name,
                    report=report, rhythm=rhythm, music=built.get("music"),
                    finished=time.time(), title=h.title,
                    attribution=((built.get("music") or {}).get("track") or {}).get("attribution"))
    except Exception as exc:  # noqa: BLE001
        _log(sid, key, f"failed: {exc}")
        _set_render(sid, key, status="failed", error=str(exc)[:500], finished=time.time())


@router.post("/api/movieshorts/render/{sid}/{hook}")
async def shorts_render(sid: str, hook: int, request: Request):
    _guard()
    body = await _body(request)
    sess = _load(sid)
    if not (sess.get("beats") or {}).get(str(hook)):
        raise HTTPException(status_code=400, detail="validate a beat plan for this hook first")
    key = f"short-{hook}"
    if (sess.get("renders") or {}).get(key, {}).get("status") == "running":
        raise HTTPException(status_code=409, detail="this short is already rendering")
    overrides = {k: v for k, v in body.items() if k in SHORTS_DEFAULTS}
    _set_render(sid, key, status="running", logs=[], started=time.time(), output=None, error=None)
    _start_thread(_render_short_job, sid, hook, overrides)
    return {"render": key, "status": "running"}


# --- movie recap ------------------------------------------------------------

def _recap(sess):
    if sess["kind"] != "recap":
        raise HTTPException(status_code=400, detail="not a movie-recap session")
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
                                 target=float(sess["settings"]["target_seconds"]), speed=float(sess["settings"]["speed"]))
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
    _recap(sess)
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
    _recap(sess)
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
    _recap(sess)
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
                                              speed=float(sess["settings"]["speed"]))
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
        shots = [{"start": c.start, "end": c.end, "scale": 1.0, "hflip": c.flip} for c in plan.chunks]
        report = film_render.render_short(
            sess["video_path"], shots, out_path, work, speed=float(st["speed"]),
            output_format=_RATIO_TO_FORMAT.get(st.get("ratio"), "vertical"), grade=None,
            fps=int(st.get("fps") or 60), cues=None, music_track=None,
            log=lambda line: _log(sid, key, line))
        shutil.rmtree(work, ignore_errors=True)
        _set_render(sid, key, status="completed", output=f"/film/{sid}/{out_name}", file=out_name,
                    report=report, finished=time.time())
    except Exception as exc:  # noqa: BLE001
        _log(sid, key, f"failed: {exc}")
        _set_render(sid, key, status="failed", error=str(exc)[:500], finished=time.time())


@router.post("/api/movierecap/render/{sid}")
async def recap_render(sid: str, request: Request):
    """Silent parts (no voiceover yet), to check the cut before recording."""
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
                    report=report, finished=time.time(),
                    segments=[{k: v for k, v in s.items() if k != "chunk"} for s in segments])
    except Exception as exc:  # noqa: BLE001
        _log(sid, key, f"failed: {exc}")
        _set_render(sid, key, status="failed", error=str(exc)[:500], finished=time.time())


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
