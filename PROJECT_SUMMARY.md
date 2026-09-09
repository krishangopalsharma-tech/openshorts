# OpenShorts — project summary (for a fresh Claude Code session)

Paste this into a new Claude Code session (any account) pointed at `F:\openshorts` to get oriented fast. The repo's own `CLAUDE.md` auto-loads for any session in this directory and has the full architecture detail — this doc is the orientation layer on top: what the product is, what state the repo/git is actually in, and the local-machine specifics `CLAUDE.md` doesn't cover.

## What it is

OpenShorts — open-source AI vertical-video generator. Long YouTube video or local upload in, 3-15 viral 9:16/1:1/16:9 clips out. Gemini (`gemini-3.1-flash-lite` by default) finds the viral moments and picks layout; faster-whisper transcribes; ffmpeg does every cut/crop/effect. Self-hostable (BYOK Gemini key) or cloud (Stripe-billed, metered minutes).

Repo root: `F:\openshorts`. React/Vite dashboard in `dashboard/`. FastAPI backend at the repo root (`app.py`).

## Current architecture (this changed a lot recently — read this, not just memory of older sessions)

**Generation is now deliberately dumb.** `POST /api/process` always renders plain 9:16 clips — no captions, no hook, no cinematic look baked in. Everything else is a **post-generation, per-clip decision** made from the clip card in the dashboard, each hitting its own endpoint:

| Button | Endpoint | Does |
|---|---|---|
| format & look | `POST /api/clip/look` | Re-render 9:16/1:1/16:9 from source, and/or apply cinematic grade/glow/grain/vignette/scrims/letterbox (`cinematic.py`) |
| music | `POST /api/clip/music` | Background track (library in `assets/music/` + upload), dB level, sidechain ducking under speech (`music.py`) |
| logo & text | `POST /api/clip/overlays` | Logos + text, dragged/resized in the browser, stored as frame-fractions so it works on every format (`overlays.py`) |
| subtitles | `POST /api/subtitle` | ClipForge-ported caption studio: 19 presets, 5 themes, 30 fonts, highlight/reveal/one-word/karaoke, glow/shadow/box, drag-to-position (`caption_styles.py`) |

Layers are **filename prefixes**, innermost first: `<base>_clip_<n>.mp4` → `recut_` (format/reframe) → `fx_` (cinematic) → `mu_` (music) → `ov_` (overlays) → `hooked_` (legacy) → `subtitled_` (always outermost). Changing one layer re-derives only it and the layers above it (`_relayer`), never the ones below — a caption restyle never re-does the music mix. Every clip stores its own recipe (`cinematic`, `music`, `overlays`, `caption_style`, `output_format`) so a trim/reframe/format-change re-applies everything automatically. This is all documented in depth in `CLAUDE.md` under "Format, look and captions are chosen AFTER generation" — read that section before touching any of these endpoints, it explains *why* (ext4's 255-byte filename cap, why re-render strips back to the layer below instead of burning in place, etc).

The pre-generation knobs (`cinematic_effects`, `auto_hook`, `captions`, `output_format` on `POST /api/process`) still work for API/MCP callers — only the dashboard UI stopped sending them.

## Other recent additions worth knowing about

- **MCP server** at `/mcp` (`mcp_server.py`) — the whole pipeline as agent tools, stateless JSON-RPC, no SDK dependency. `osk_...` API keys (`cloud/api_keys.py`) work as agent auth everywhere.
- **Spoken-language control**: `POST /api/process` takes `language` (whisper code, `auto`, or `hinglish`). `hinglish` transcribes Devanagari then romanises it (`translit.py`) so all 19 Latin-only caption presets still work on Hindi content. Forcing `hi` explicitly matters — auto-detect on Hindi/Urdu sources has been seen sliding into English translation mid-file.
- **Compilation feature** (`compilation.py`) — narrated short built from a Gemini edit plan + ElevenLabs voiceover, separate from the clipping pipeline.
- **Voice dubbing** via ElevenLabs (`translate.py`), 30+ languages.
- **GDPR account erasure** (`cloud/account.py`, `DELETE /api/account`) — immediate, irreversible, ordered Stripe→R2→DB, closed-list deletion reason.
- **Deploy/drain mechanics** for zero-downtime rolling deploys on Coolify (`CLAUDE.md` has the full sequence: instance-id handover, resume manifests, SIGTERM drain timing). Not relevant to local dev, very relevant if touching `app.py`'s startup/shutdown code.

## Git / remotes — this is a fork, and it's diverged

```
mine     https://github.com/krishangopalsharma-tech/openshorts.git   (push target — this fork)
origin   https://github.com/mutonby/openshorts.git                   (upstream project, NOT this account's)
upstream https://github.com/kamilstanuch/Autocrop-vertical.git       (the original project this was forked from)
```

`main` tracks `origin/main` and is currently **16 ahead, 40 behind** — i.e. upstream (`mutonby/openshorts`) has moved a lot since this fork's `main` branched, and this fork has real local work on top. **Do not naively merge/pull `origin/main`** without expecting conflicts, especially in `app.py`. Push finished work to `mine`, not `origin` (no write access there anyway).

There are several other local/remote branches (`claude/hindi-subtitle-hinglish-e99d1e`, `feat/cinematic-export-compilation`, a few `claude/*` housekeeping branches, and a live git worktree at `.claude/worktrees/claude-rc-0c8075`). If you're picking up mid-task, check `git branch -vv` and `git status` before assuming `main` is the active line of work.

Latest commit as of this summary: `b451b7b fix(dev): proxy /overlays, /music and /fonts to the backend`.

## Local dev, this machine, no Docker

Docker Desktop was broken on this machine (dead containers, hung daemon) during earlier work and the whole stack was moved to running natively. As of the last working session:

- **Python venv** at `F:\openshorts\venv`, **Python 3.11.9 specifically** (`mediapipe==0.10.14` pinned in `requirements.txt` requires ≤3.11; this machine's default Pythons were 3.12/3.13/3.14rc, so 3.11 was installed separately via `winget install --id Python.Python.3.11 --source winget`).
- **`pip install torch==2.11.0`** resolves to a **CPU-only** wheel on Windows by default. This machine has an RTX 2070 SUPER — the CUDA build had to be installed explicitly:
  ```
  pip install torch==2.11.0+cu128 torchvision==0.26.0+cu128 --index-url https://download.pytorch.org/whl/cu128
  ```
  Check `torch.cuda.is_available()` if whisper transcription looks slow (~300s+ instead of ~10-15s) — that's the CPU-fallback symptom. Note also: `transcribe_backends.py`'s `_whisper_force_cpu` flag is a **process-lifetime latch** — once one GPU failure trips it, that flag stays true until the server process restarts, even after fixing the underlying CUDA issue.
- **SSL/TLS**: this machine's HTTPS traffic goes through something (AV or corporate proxy) that a fresh Python install's `certifi` bundle doesn't trust, breaking `pip install` and `huggingface_hub`'s runtime model downloads with `CERTIFICATE_VERIFY_FAILED`. Fixed with the `truststore` package + a `sitecustomize.py` dropped into `venv/Lib/site-packages/` (auto-loaded by Python on every interpreter start in this venv):
  ```python
  import truststore
  truststore.inject_into_ssl()
  ```
  This is a machine-local fix, deliberately not part of the git repo.
- **Dev launcher**: `start-dev.bat` / `stop-dev.bat` (untracked, at repo root) start/stop backend (uvicorn, port 8000) and frontend (Vite, port 5173) each in their own console window. **Access the dashboard at `http://127.0.0.1:5173`, not `localhost`** — Windows/browsers often resolve `localhost` to `::1` first, and if anything else (e.g. a leftover Docker port-forwarder) is squatting on the IPv6 side of a port, `localhost` silently routes to the wrong backend while `127.0.0.1` doesn't.
- **Backend has no `--reload`.** Restart it after every Python-side edit — this has caused confusing "why isn't my fix showing up" moments more than once.
- **rtk (Rust Token Killer)**: a Claude Code hook installed globally (`~/.claude/RTK.md`) that transparently rewrites Bash calls (`git status` → `rtk git status`) to cut token usage on command output. Nothing to do here, just don't be confused if `git status` output looks pre-filtered.

## If you're about to touch `cinematic.py`

It's been edited since the version described in older session notes — it now takes an optional `output_path` param on `apply_cinematic_effects()` so `/api/clip/look` can write the graded copy to a new `fx_` file without touching the clean render (in-place is still the default, used by the legacy generation-time `CINEMATIC_EFFECTS` env path for API/MCP callers). Read the current file before assuming its shape from memory — it's under active development across sessions.

## Quick verification checklist for a fresh pickup

1. `git status` / `git branch -vv` — confirm which branch/worktree you're actually in.
2. Is the backend running? `curl http://127.0.0.1:8000/health` — if not, `venv\Scripts\uvicorn app:app --host 0.0.0.0 --port 8000` (or `start-dev.bat`).
3. Is the frontend running? `curl http://127.0.0.1:5173` — if not, `npm run dev -- --port 5173 --host` in `dashboard/` with `VITE_PROXY_TARGET=http://127.0.0.1:8000` set.
4. `npm run build && npm run lint` in `dashboard/` before calling any frontend work done — lint is strict (`--max-warnings 0`).
5. A Gemini API key is needed for clip picking, titles, layout picker, thumbnails — entered per-browser-session in Settings, not in `.env`. Not needed for transcription, captions, look, music, or overlays (those are pure ffmpeg/local-model work).
