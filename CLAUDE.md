# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenShorts is an AI-powered vertical video generator that transforms long YouTube videos or local uploads into viral-ready short clips (9:16 format) for TikTok, Instagram Reels, and YouTube Shorts. Uses Google Gemini 3.1 Flash-Lite (`gemini-3.1-flash-lite`, overridable with `GEMINI_MODEL`) for viral moment detection and title generation.

## Development Commands

### Local Development (Docker)
```bash
docker compose up --build   # Build and run full stack
```
- Backend: http://localhost:8000 (FastAPI/Uvicorn)
- Frontend: http://localhost:5175 (Vite proxies API calls to backend)

### Frontend Only (Dashboard)
```bash
cd dashboard
npm install
npm run dev       # Dev server with HMR (port 5173)
npm run build     # Production build
npm run lint      # ESLint (strict, --max-warnings 0)
```

### Backend Only
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Architecture

### Core Processing Pipeline
1. **Ingest** - YouTube download (yt-dlp) or local upload
2. **Transcription** - faster-whisper with word-level timestamps
3. **Scene Detection** - PySceneDetect for segment boundaries
4. **AI Analysis** - Gemini identifies 3-15 viral moments (15-60 sec each)
5. **FFmpeg Extraction** - Precise clip cutting
6. **AI Cropping** - Vertical reframing with subject tracking
7. **Effects/Subtitles** - Optional AI-generated FFmpeg filters
8. **Hook Overlay** - Text overlays with styled fonts
9. **Voice Dubbing** - Optional ElevenLabs AI translation (30+ languages)
10. **S3 Backup** - Silent background upload
11. **Social Distribution** - Upload-Post API (async upload)

### Key Files
| File | Purpose |
|------|---------|
| `main.py` | Core video processing: transcription, scene detection, clip extraction, vertical reframing |
| `app.py` | FastAPI server with async job queue and REST endpoints |
| `editor.py` | Gemini AI integration for dynamic video effects (FFmpeg filter generation) |
| `cinematic.py` | Cinematic look (grade/glow/grain/vignette/gradients/letterbox): one ffmpeg pass, applied per clip after generation (`/api/clip/look`) or at render for API callers |
| `caption_styles.py` | Caption looks: 22 presets (19 ported from ClipForge), 5 theme bundles, the override schema and the bundled font registry (`fonts/`) |
| `hooks.py` | Hook text overlay generation with font rendering |
| `s3_uploader.py` | AWS S3 upload with caching |
| `subtitles.py` | SRT/ASS generation (legacy karaoke + `generate_ass_styled` for the ClipForge presets), FFmpeg subtitle burning, dubbed video transcription |
| `translate.py` | ElevenLabs dubbing API for AI voice translation |
| `translit.py` | Devanagari → Hinglish romanisation for captions (`TRANSCRIBE_LANGUAGE=hinglish`) |
| `dashboard/src/App.jsx` | Main React component with state management |
| `dashboard/src/components/TranslateModal.jsx` | Voice dubbing UI with language selection |
| `dashboard/vite-plugin-seo.js` | Build-time SEO surface: injects crawler-visible homepage content, emits static pages, sitemap.xml and llms.txt |
| `dashboard/seo/data.js` | Single source of truth for pricing, pipeline and competitor facts used by every generated page |

### SEO / AI-crawler surface

The dashboard is a client-rendered SPA with hash routing, so the HTML served for
`/` used to contain an empty `<div id="root">`. Googlebot renders JavaScript and
saw the real page; GPTBot, ClaudeBot and PerplexityBot do not and measured the
homepage as zero characters of text. `vite-plugin-seo.js` fixes that at build time:

- Injects the content of `seo/landing-fallback.js` into `#root`. React's
  `createRoot().render()` replaces it on mount, so users get the app and
  non-executing clients get the copy. **Keep it in sync with `Landing.jsx`.**
- Emits the standalone pages (the `/alternatives` cluster, the clip-generator,
  open-source, use-case and automation pages, and `/mcp`; the full list is
  `buildPages()` in `seo/pages.js`) as flat `.html` files.
  nginx resolves the clean URL through `try_files $uri $uri.html`; serving them as
  directories instead makes nginx 301 to a trailing slash and every canonical
  would then point at a redirect.
- Generates `sitemap.xml` and `llms.txt` from the same page list, so they cannot
  drift. Do not add a static `public/sitemap.xml` back.

When editing pricing anywhere, edit `seo/data.js` too. Nothing on the site should
say "OpenShorts is free" without naming the Cloud price in the same breath: both
are true of different editions and quoting only the first one is what makes AI
answers describe the paid product as free.

### Cómo se elige el layout

`POST /api/process` acepta `layouts`: una lista (JSON) o cadena separada por
comas con `auto`, `split`, `screencast`, `speaker_cut`, `punch_in` y `none`.
Cada nombre enciende su variable de entorno para **ese** trabajo
(`app.py:layout_env`); `none` apaga el picker aunque prod corra con
`AUTO_LAYOUT=1` (recorte simple y nada más). Sin `layouts` manda el env del
despliegue, que desde el 25-ago-2026 es `AUTO_LAYOUT=1`. El dashboard lo expone
en opciones avanzadas ("vertical layout": auto / split / screencast / none,
`MediaInput.jsx`, recordado en `localStorage.os_layout`).

`auto` activa `layout_picker.py`: **una** llamada a Gemini por vídeo de origen
(no por clip) que elige entre `none` / `screencast` / `split`. Medido sobre el
corpus de 48 contra etiquetas revisadas a mano: 94% / 92% / 96% en tres pasadas,
con 0-1 falsos positivos sobre los 28 clips que no deben tocarse, y solo 2 clips
que cambian de respuesta entre pasadas.

**Manda 12 fotogramas a 1024px, no el vídeo.** Gemini factura vídeo a ~300
tokens por segundo: una hora de fuente son ~1,08M de tokens (no cabe en una
ventana de 1M) y una subida de 1-2 GB para recibir una palabra. Doce fotogramas
cuestan ~3k tokens **dure lo que dure la fuente**, que es lo que hace viable
esto con los podcasts de una hora que entran de verdad. La resolución importa y
el número de fotogramas no: a 640px detecta 15 de 20 (una hoja de cálculo es
ilegible), a 1024px sube a 17, y pasar a 24 fotogramas lo empeora. A 1024px la
diferencia con mandar el vídeo entero cae dentro de la varianza que ya tiene el
propio modo vídeo, a 2,2 s por clip en vez de ~15 s.

Lo que hace que funcione, y que conviene no deshacer: se le pide una **decisión
entre opciones cerradas**, no una medida. Los cuatro intentos anteriores (Canny,
MSER, cobertura temporal, anchura) le pedían un número y ninguno separó una hoja
de cálculo de un marcador de esquina. La varianza que este repo atribuía a
Gemini era de las medidas continuas, no del modelo.

`layout_picker.apply()` sólo **añade**: una elección explícita del usuario nunca
se desactiva porque el modelo diga `none`.

### Hook grounding for on-screen clips (`hook_grounding.py`)

The hook and title come from the detail pass, which only reads the
transcript, so on a clip whose meaning is on the screen (a settings dialog,
a spreadsheet) they summarise the video's topic instead of naming what is
shown. After the render, if the `<clip>.layout.json` sidecar says at least
25% of the clip is `screencast` / `wide` / `inset` (plus `general` when the
layout picker called the video a screencast: a face-less scene there is a
slide or a dialog, not a group shot), three frames from those
stretches at 1024px plus the clip's own words go to Gemini
(`GroundedHook`) and `viral_hook_text` / `video_title_for_youtube_short`
are rewritten in place before `auto_hook_clip` burns them; the originals
stay under `hook_grounding.before`. Gemini-only (frames): with just a local
LLM it logs one line and keeps the transcript hook. `HOOK_GROUNDING=0`
disables it. The detail prompt itself now carries the rule "about this
moment, not the video", which is the cheap half of the same fix.

### Local LLM for the moment picker (`llm_backend.py`)

`LLM_BASE_URL` (+ `LLM_MODEL`, `LLM_API_KEY`) routes the two transcript
passes of `get_viral_clips` to any OpenAI-compatible `/chat/completions`
instead of Gemini; the response is validated with the same pydantic schemas
Gemini enforces server-side, so `main.py` sees one shape. `main.score_batch_size`
drops to 3 windows per call there (local contexts are 4-8k; a truncated
prompt scores garbage silently). Self-host `/api/process` then accepts a
request without `X-Gemini-Key` and `/api/config.localLlm` tells the dashboard
not to demand one. Frame-based stages (`layout_picker`, `screencast_layout`,
`get_visual_clips`) stay on Gemini and degrade as they always did without a
key. Never wired in cloud mode: `BILLING_ENABLED` ignores it.

### Thumbnail Studio (`thumbnail.py`, `/api/thumbnail/*`)

Titles come from the transcript plus 10 frames at 1024px, never the whole
video (same reasoning as the layout picker: an hour of video is ~1M tokens for
a text task). Two calls: a 25-title brainstorm across fixed styles, then a
critic that scores, dedupes by angle and returns 10, each paired with a 1-4
word `thumbnail_text` that complements the title rather than repeating it.
Rules baked in: payoff inside 50 characters (phones cut there), keyword in the
first 3 words, same language as the transcript. Text model is
`GEMINI_MODEL_THUMBNAIL` (default `gemini-3.7-flash`), deliberately not
`GEMINI_MODEL`: flash-lite is fine for a closed-choice layout pick and visibly
worse at creative titles. Image model is `GEMINI_IMAGE_MODEL` (default
`gemini-3.1-flash-image`).

Thumbnails are `count` **different concepts**, not one prompt repeated: a text
call designs each (hook text, side for the text, palette, scene prompt), then
one image call per concept in parallel. By default (`burn_text=true`) the
image model is told to leave that side as negative space and PIL sets the text
in Anton with a black stroke, so accents and spelling are never wrong; the
`AI painted` toggle lets the model render the text itself. Every output is
cover-cropped to 1280x720 and saved under YouTube's 2 MB limit.
`GET /api/thumbnail/frames/{session}` scores sampled frames by face area and
sharpness (MediaPipe + Laplacian), keeps them spread across the runtime, and
the dashboard offers them as the person reference so the thumbnail shows the
creator instead of a stranger; an uploaded face photo still wins.

### Video Reframing Modes

**A source already shot vertical is passed through untouched.**
`reframe_v2.source_already_fits()` gates it: every layout below reorganises the
frame to buy back width the crop threw away, and on a 9:16 upload there is none
to buy. GENERAL was the visible failure — its 0.42 height ratio, which buys
presence on a landscape source by overflowing the sides, scaled a 1080x1920
source down to a 453px sliver floating over a blurred copy of itself, and the
scene classifier routes every face-less shot (a slide, a screen recording) there.
So the picker is skipped (one Gemini call saved per upload), the classifier is
skipped, and every scene renders TRACK, whose crop is the whole frame.
`general_filtergraph` additionally floors the foreground at the height where the
source fills the output width, so the editor's explicit GENERAL override on a
portrait clip cannot reproduce the shrink either.

- **TRACK Mode** (single subject): MediaPipe face detection + YOLOv8 fallback with "Heavy Tripod" stabilization
- **GENERAL Mode** (groups/landscapes): Blurred background layout preserving full width
- **SPLIT Mode** (two-shot conversation, `split_layout.py`): both speakers stacked
  in half-frames. Off by default (`SPLIT_LAYOUT=1`); v2 engine only, so a
  fallback to the v1 loop silently renders GENERAL instead. It upgrades scenes
  the classifier already sent to GENERAL, never TRACK ones, and needs both faces
  visible **in the same frame** for at least half the sampled frames — that is
  what separates a real two-shot from a plano/contraplano, where stacking would
  show the same person twice. `SPLIT_TIGHTNESS` (default 0.8) trades a little
  upscale for keeping the other speaker out of each half. Captions on a SPLIT
  stretch sit on the seam between the halves (`{\an5}` per word event in
  `subtitles.generate_ass`), the one place they cover nobody; the render
  records which stretches are stacked in a `<clip>.layout.json` sidecar
  (`layout_ranges.py`) and every metadata writer copies it into the clip's
  `layout_ranges`, so `/api/subtitle` finds it after a restyle too. The fast
  rerender (cut without reframe) carries the canonical clip's ranges through
  the new cut (`layout_ranges.remap`, in `recut.perform_recut`). Only the
  ASS path can do this; SRT burns keep one alignment for the whole file.
- **SCREENCAST / WIDE Modes** (`screencast_layout.py`, `SCREENCAST_LAYOUT=1`):
  for scenes whose meaning lives outside the centre. Gemini reports each range's
  **width_fraction**, and that is the gate — coverage was tried before and did
  not separate a spreadsheet from a corner ticker, while width does (a bug spans
  ~15% and survives any crop, a spreadsheet spans ~100% and cannot). Content
  narrower than 0.5 moves nothing. Between 0.5 and 0.85 there is room beside the
  content, so SCREENCAST stacks it over the presenter. Above 0.85 the presenter
  is composited **on top of** the content and stacking would show it twice, so
  those scenes get WIDE: the GENERAL layout with side-cropping disabled.
- **INSET Mode** (`camera_inset.py`): pantalla a ancho completo arriba, el
  recuadro de la webcam ampliado abajo. Para el caso de una sola fuente con la
  cámara compuesta en una esquina (OBS, VOD de stream). Se encadena detrás de
  la decisión `screencast`, **no** se le pregunta a Gemini: ofrecido como cuarta
  opción respondió `screencast` en los 5 clips que tienen recuadro, en dos
  pasadas, y la exactitud global cayó de 92% a 83-85%. El detector geométrico
  encuentra esos 5 sin falsos positivos. Los tres filtros que hacen falta, cada
  uno pagado con una iteración: sujeto **pequeño**, **descentrado en
  horizontal** (una cara de talking head está centrada aunque esté alta), y
  **quieto entre muestras** (3-11px frente a 316px de una persona real).
- **ALTERNATE Mode** (`active_speaker.py`, `SPEAKER_SIGNAL=1` + `SPEAKER_CUT=1`):
  hard cuts to whoever is talking, rendered through the TRACK path as a
  trajectory with jumps. `SPEAKER_SIGNAL=1` alone just gates SPLIT on both people
  actually speaking. Mouth activity **must** be normalised per speaker before
  comparing (`normalise_activity`): raw frame-difference magnitude scales with
  local contrast and lighting, and on a real two-shot it handed one speaker
  90-100% of the scene.
- **Punch-in** (`punch_in.py`, `PUNCH_IN=1`): not a layout. A ~12% push on the
  clip's beats, riding the TRACK path by widening its per-frame crop command
  from x-only to w/h/x/y. Beats currently come from the audio envelope;
  `emphasis_times` is a plain list of seconds so the transcript's hook words can
  replace it without touching the module.

### Spoken language and Hinglish captions

`POST /api/process` takes `language`: a whisper code (`hi`, `es`, …), `auto`
(the default: whisper detects it) or `hinglish`. It rides down as the
`TRANSCRIBE_LANGUAGE` env var that `transcribe_backends.transcribe_language()`
reads; the dashboard exposes it in advanced options (`MediaInput.jsx`,
remembered in `localStorage.os_language`).

**Naming the language is not cosmetic on Hindi/Urdu.** Auto-detect there slides
into English TRANSLATION partway through a file — the reported bug was a Hindi
source whose clips came back with English captions, and forcing `hi` is what
stops it.

`hinglish` is not a whisper language: it transcribes as Hindi (Devanagari) and
then romanises the transcript word by word (`translit.py`), which is what the
audience actually types ("aap kaise hain") and what keeps the caption presets
usable — every one is a Latin display face, so Devanagari drops out of the chosen
style into whatever libass finds. The romanisation happens once, on the
transcript, so clip titles and the Gemini prompts see the same text the
captions do; `transcript["language"]` becomes `"hinglish"`.

The transliteration is ported from ClipForge's `translit.py` but has no
third-party dependency (the table is 60 lines; indic-transliteration's IAST
round-tripping bought nothing) and adds the two rules that make it read like
typed Hinglish rather than a dictionary: **schwa deletion** (final always —
`ghara` → `ghar`; then the VC_CV rule — `ladaki` → `ladki`, `matalaba` →
`matlab` — blocked by a closed syllable before it, so `zindagi` keeps its
schwa) and **long `ā` doubles inside a word but not at the end** (`aap`,
`pyaar`, but `kya`, `sharma`).

Both of those rules key off the **last syllable unit**, which is why attached
punctuation had to be peeled off before the syllable walk (`_split_affixes`):
`_units()` made a trailing comma the last unit, so neither rule fired and
`सर,` came out `sara,`, `कपिल,` `kapila,`, `क्या,` `kyaa,`. Caption text is
mostly sentence-final words, so it was visible on nearly every line. The
peeled punctuation is still romanised (`।` → `.`), just separately.

The `aa`/`a` choice also used to require more than one syllable, which left
`का` as `kaa`, `था` as `thaa`, `ना` as `naa` — some of the most frequent words
in the language. A single syllable shortens too now, as long as a consonant
carries the vowel; the two things that keep both letters are a bare vowel that
IS the whole word (`आ` = `aa`) and a nasalised ending, where the long vowel is
audible and typed (`हाँ` = `haan`). A final independent vowel after another
syllable still shortens (`हुआ` = `hua`).

**English loanwords get their English spelling back** (`_LOANWORDS`, ~150
entries plus nukta variants). Rule-based romanisation can only spell what it
heard, so `नेल पेंट` came out `nel pent`, `हेलो` `helo`, `स्टाइल` `staail` —
and these are a large share of Hindi media speech. The table is per TOKEN, not
per phrase, because each timed word is romanised on its own and that is what
keeps per-word caption timings (`नेल पेंट` is two entries).

Lookup folds the spellings whisper alternates between for the same word. Nukta
marks, since it writes both `फ़ोन` and `फोन`. And **two matra pairs**, `े`/`ै`
and `ो`/`ॉ`, because Devanagari has no settled spelling for an English vowel:
`मैसेज` was in the table and `मेसेज` was not, so one real episode captioned the
same word `message` on one line and `mesej` on another. `ि`/`ी` and `ु`/`ू` are
NOT folded — whisper does not alternate those. The fold skips `_NO_VARIANT`,
because a variant spelling can be a different word (`रोड` road but `रॉड` rod,
`रॉल` a roll not a role, `फेन` Hindi for foam, `टोप` a cap); that is the same
call as the ambiguous words kept out of the table.

A word that is ALSO a common Hindi word is deliberately absent: a lookup
cannot tell which was meant, and the Hindi reading usually wins. Counted over
one real episode, `चीज़` appeared 4 times and meant "thing" every time, never
"cheese"; `बस` 3 times, never the vehicle. Also out: `पास` (paas, "near"),
`हाय`. `सर` IS in, because "sir" dominates interview and comedy speech and the
cost when it means "head" is a mild `sir dard`.

### The model matters more than any of this on Hindustani

`WHISPER_MODEL=large-v3-turbo` is the wrong model for Hindi/Urdu and was the
real cause of "the Hinglish captions are not up to the mark". Measured on a
Comedy Nights episode (24-36s), turbo against full `large-v3`: `कापिल`/`कपिल`,
`सार`/`सर`, `अंगुथे पर`/`अंगुठे पे`, `नेलपेंट लगाय`/`नेल पेंट लगा`. Turbo is a
4-decoder-layer distillation and it degrades on low-resource languages and
code-switching, which is all Hinglish is.

So `subtitles.get_whisper_config(language)` takes the language and swaps a
turbo build for `large-v3` (override: `WHISPER_MODEL_HINDUSTANI`) on
`TURBO_UNSAFE_LANGUAGES` — an explicitly named non-turbo model is never
overridden, and English keeps turbo because it is unaffected there and
faster. Once the weights are cached this costs nothing measurable: 2.7s
against turbo's 4.3s on a 12s slice (float16, RTX 2070 SUPER).

`POST /api/process` also takes `transcribe_prompt` → `TRANSCRIBE_PROMPT`, the
names and domain words for the decode (dashboard: "names & terms in this
video"). Whisper spells a name it has never heard the way it sounded — `अजीए`
for अजय — and listing it fixes that word. **It is withheld from turbo**
(`whisper_supports_prompt`): prompted, turbo answers a Hindi source in English
("Hello Ajay sir.") and then returns nonsense, so the prompt is only safe
alongside the model swap above. Do not seed this from the video title either —
titles are usually English, and an English prompt over Hindi audio invites the
same drift.

**The prompt is not free, and it must not be written in Devanagari.** Measured
on four code-switched slices of the same episode against no prompt at all:

- A **Devanagari** prompt got **echoed verbatim** as the transcript on the
  English-heavy slice (`अजय देवगन, करीना कपूर, सिंघम रिटर्न्स` twice, and the
  actual speech gone). Whisper's `initial_prompt` is prior decoder context, so
  a prompt in the output script is indistinguishable from text it just emitted.
- An **English names-only** prompt destabilised Hindi-dominant lines
  (`नो प्रॉब्लम` → `नू प्रॉब्लम`, plus a hallucinated tail) but recovered a long
  English stretch as **real Latin English** — `I like your hairstyle first of
  all I am going to teach you dance` where the unprompted decode produced a
  semantically broken Devanagari transliteration.
- **No prompt** was the most stable on Hindi-dominant lines.

So it earns its place on name-bearing lines and costs stability elsewhere,
which is why it stays opt-in per job rather than becoming a default. Keep it
short, keep it to proper nouns, and keep it in Latin script.

**`initial_prompt` only reaches the FIRST 30s window, and `hotwords` — the fix
for that — is worse.** Because `WHISPER_TRANSCRIBE_PARAMS` sets
`condition_on_previous_text=False`, faster-whisper advances
`prompt_reset_since` after every window (`transcribe.py`: the
`not options.condition_on_previous_text` branch), so the initial-prompt tokens
sit in `all_tokens` below the cursor and `previous_tokens` is empty from
window 2 on. `hotwords` is re-read from `options` on every `get_prompt` call
instead, so it does persist — which is why it looks like the obvious fix. It
is not. Measured on a 110s slice of the same episode (3.7 windows, names at
+45s and +69s), one pass each:

| arm | segments | what happened |
|---|---|---|
| no prompt | 37 | all of 0-110s, coherent |
| `initial_prompt` | 37 | **the whole first 36s is gone** |
| `initial_prompt` + `hotwords` | 16 | **~52s of 110s dropped**, plus drift to `100 episode`, `70`, `इंवाइट` |

So the prompt does not merely destabilise wording, it can drop entire
windows of speech, and making it persist multiplies that across the file.
`hotwords` is deliberately NOT wired up. If it is ever revisited, the
acceptance test is coverage — spoken seconds emitted and the largest silent
gap — not whether one name came out right; a name-only check scores the arm
that dropped half the audio as the winner. (One pass per arm on one slice;
whisper's temperature fallback makes it worth repeating before treating the
exact numbers as settled. The direction was not subtle.)

A **Hindi fine-tuned whisper is not the upgrade it looks like.** Measured:
`vasista22/whisper-hindi-large-v2` (CTranslate2 float16) hears Hindi words
better than large-v3 — `मैसेज भेज` where large-v3 writes `मेसेज बेच` ("sell"),
`डेब्यू` for `डेबिव`, `सेंचुरी मारी` for `सेंचरी महरी` — and is disqualified
anyway. It reduced a 14-second English stretch to two words, and it
hallucinates All India Radio boilerplate into silence
(`आप आकाशवाणी रांची से सुन रहे हैं`, `इसी के साथ ये समाचार बुलेटिन समाप्त हुआ`),
because these models are trained on Shrutilipi/AIR news corpora and this
pipeline's input is entertainment. `vad_filter` and
`condition_on_previous_text=False` made it worse, so it is not a config
problem. A hallucinated sign-off becomes timed captions for words nobody said,
which is a failure a WER number hides. Any future candidate gets this
acceptance test: entertainment audio with silence in it, checked for invented
sign-offs.

One Windows-only trap worth knowing: the CUDA libs CTranslate2 needs ship
inside torch's own package (`torch/lib/cublas64_12.dll`) and it is torch's
import that puts that directory on the DLL search path. `_get_whisper_model`
therefore imports torch before `faster_whisper` — without it a GPU load dies
with "Library cublas64_12.dll is not found or cannot be loaded" and trips
`_whisper_force_cpu` for the rest of the process. `main.py` only ever got away
with it because ultralytics imports torch first.

A transcript that is REUSED rather than produced (the Thumbnail Studio
handover, the checkpoint an interrupted run leaves) never passed through the
backend's language choice, so `main.py` romanises it on the way in — only when
it actually carries Devanagari, so an English transcript is not retagged.

Every per-job env choice (this one, the layouts, the look, the generation
controls) is also written to the resume manifest as `job_env`, an explicit
ALLOWLIST (`app._RESUMABLE_ENV_KEYS`) — a resumed job rebuilds its env from
`os.environ`, so without it a redeploy mid-job would silently finish with the
deployment defaults. The allowlist, rather than an env diff, is what keeps
`GEMINI_API_KEY` out of a file sitting next to the user's video.

### Audience profiles and the chat route (`audience_profiles.py`, `manual_clips.py`)

Two channels are being fed from this fork — India (Hindi/Hinglish) and USA
(English) — and what makes a clip work differs enough between them that one
prompt cannot serve both. `PROFILES` holds a block of audience rules per
channel; `pick()` resolves one from `--audience`, then `AUDIENCE`, then the
transcript's language (`hinglish`/`hi`/`ur`/… → `in`, `en` → `us`), and
`gemini_block()` is prepended to **both** Gemini prompt templates in
`get_viral_clips`. That placement is the point: dashboard jobs get the right
rules with no new API parameter, purely from the language they already carry.

The India profile transcribes as **`hinglish`, not `hi`** — the profile asks
for Roman-script titles and hooks, every caption preset is a Latin display
faces, and Devanagari would drop out of the chosen style. Profile settings go
in through `setdefault`, so an explicit env var still wins.

`--transcribe-only` writes `paste_into_chat.txt` (the profile's prompt plus a
timestamped transcript) and stops; picks pasted back as `--clips clips.json`
are snapped to word edges (`clip_selection.snap_clip_to_words`) and skip the
Gemini picker entirely. That is the no-API-key route: a strong model picks the
moments in a chat window, this repo renders them.

Every clip records **`picked_by`**: `gemini:<model>` or `local:<model>` stamped
in `get_viral_clips` (from `llm_backend.active()`, so it names the model that
actually answered), `claude` in `manual_clips.load_manual_clips`. It cannot be
reconstructed afterwards and it is the whole question the picker A/B asks, so
it is stamped at the source. `metadata.json` is dumped wholesale and carries it
for free; the ZIP's CSV and the webhook payload both project fields explicitly
and each had to name it — and the CSV's `DictWriter` has an explicit
`fieldnames`, which raises on an unknown row key rather than dropping it, so
that list is part of the change. Clips made before this existed have no
`picked_by`; both exports handle its absence rather than failing.

The **prose in each profile is a placeholder** and needs rewriting against the
real channels. It is prompt text, not logic — treat it as copy, and keep the
"WHAT WINS" lines tied to measured performance rather than taste.

### The same video twice (`source_history.py`, `/api/source/check`)

Re-submitting a video by accident is the most expensive mistake the UI allows:
a 72-minute source is ~25 min of transcription plus a render per clip, and
nothing later in the flow would catch it. `output/.sources.json` records what
each finished job was cut from, and the dashboard asks `/api/source/check`
before submitting — with the file's NAME and BYTE SIZE only, never the file,
or checking a 600 MB upload would cost as much as running it.

Three independent signals, and the warning says which one fired: a YouTube id
(exact), the normalized title (what the user recognises), and size+duration
(the same file renamed, which a title cannot catch). Title normalization is
case/punctuation/spacing only — it deliberately does NOT strip `(HD)`,
`1080p` or `full episode`, because those are often the only difference
between two real uploads, and a false "you already did this" is worse than a
missed one: it teaches the user to click through the warning.

It is a JSON file rather than a table because the cloud build's `UserVideo`
is per signed-in user, holds finished CLIPS rather than sources, and does not
exist in a self-host install — which is exactly where one person runs the
whole pipeline and makes this mistake.

**Advisory, never a block.** `/api/process` is unchanged, so agents and MCP
callers are unaffected; the dashboard gates its own submit button behind a
"generate again anyway" tick. A missing or corrupt index reads as empty, and
the recorder swallows its own errors — this feature must never be the reason
a job fails or a submit is refused.

`_recover_jobs_from_disk` seeds the index from job dirs already on disk (one
write, idempotent, and it never overwrites a real run with an older backfilled
one). Without that the index starts empty and the first thing it fails to warn
about is the video cut yesterday, which is the whole case it exists for.

### Format, look and captions are chosen AFTER generation

The dashboard no longer asks for an output format, a cinematic look, captions
or a hook title at upload. `POST /api/process` from the dashboard always sends
`output_format=vertical`, `captions=false`, `auto_hook=0`, so a job delivers
plain 9:16 clips (the AI layout picker still runs, it is what makes the 9:16
worth keeping as the default). The user then decides per clip, or for every
clip at once (the dashboard loops the per-clip endpoints; there is no bulk
endpoint), from the clip card: **format & look** (`LookModal.jsx` →
`/api/clip/look`) and **subtitles** (`SubtitleModal.jsx` → `/api/subtitle`).
The hook title overlay is gone from the UI; `/api/hook` still serves API
callers. The pre-generation knobs (`cinematic_effects`, `auto_hook`,
`captions`, `output_format`) all still work on the API for agents and MCP.

**Layers are filename prefixes, innermost first:** `<base>_clip_<n>.mp4`
(clean reframe) → `recut_<ts>_<hex>_` (a different cut or format) →
`fx_<hex>_` (cinematic look) → `mu_<hex>_` (background track) →
`ov_<hex>_` (logos + custom text) → `hooked_<ts>_` → `subtitled_<ts>_`. The three
new prefixes are short and carry no timestamp on purpose: ext4 caps a filename
at 255 **bytes**, every layer nests the name below it, and a 120-byte title +
uuid + `recut_` + captions already leaves ~80 bytes for everything else
(resolution is by mtime, so the timestamp bought nothing). The under-caption layers are listed in
`_UNDER_CAPTION_LAYERS`; `_relayer(changed)` re-derives one and everything
above it while keeping the files below (a music change never re-grades),
and `_apply_layers` is what `perform_recut`'s `effects` hook runs on a fresh
render, so every layer survives a trim, a reframe or a format change.
Nothing is ever burned in place after generation: every change strips back to
the layer below (`_strip_burned_captions` / `_strip_burned_hook` /
`_strip_cinematic`, or `_strip_all_layers` for the bare render), re-derives,
and puts the outer layers back. `_canonical_clip_file` knows every prefix, so
a restart, the R2 archive and the ZIP export resolve the newest derivative.

- **Format** (`/api/clip/look` with `output_format`): a full re-render of the
  clip's recipe from the retained source through `recut.perform_recut`, with
  the clip's framing and hand-set crops, so it costs a reframe (metered like
  `/rerender`) and answers 409 once retention has removed the source. No
  transcription, no Gemini. Each clip stores its own `output_format`;
  `_clip_output_format` resolves it (the job-level `auto` always meant 9:16),
  and `/rerender` and `/reframe` render in the clip's format, not the job's.
- **Look** (`/api/clip/look` with `cinematic`): `cinematic.apply_cinematic_effects`
  into an `fx_` derivative of the bare render (never in place), stored on the
  clip as `cinematic` (normalized, every key) or `null`. `perform_recut` takes
  an `effects` hook, so a trim, a reframe or a format change re-applies the
  look under the captions instead of losing it.
- **Music** (`/api/clip/music`, `music.py`, library in `assets/music/` +
  `POST /api/music/upload`, previews at `/music/<file>`): ClipForge's
  reels-style mix, level in dB. The voice is split, one copy keys a
  `sidechaincompress` that ducks the track while someone talks (`duck`
  0-100 scales the ratio 1→20), the other is mixed back with
  `amix normalize=0` so dialogue stays at unity; the track loops,
  starts `start` s in and fades out over the last second. Video is
  stream-copied, so it is a 2 s pass. Stored on the clip as `music`.
- **Overlays** (`/api/clip/overlays`, `overlays.py`, logo library in
  `assets/overlays/` + `POST /api/overlays/upload`, served at `/overlays/<file>`):
  the manual replacement for the removed hook title. Items are logos or
  text blocks placed with the mouse in the editor; geometry travels as
  **fractions of the frame** (centre x/y, width; text size as a fraction
  of the height) so one list lands in the same relative spot on every
  format and "apply to all" needs no per-clip fixing, which is what makes
  text in the black band of a 1:1 clip work. Everything is rasterised by
  PIL into ONE full-frame RGBA image (text through hooks.py's emoji-aware
  drawing with the caption fonts, so it matches the browser's @font-face
  preview) and composited in a single ffmpeg `overlay` pass, no drawtext
  escaping. Stored on the clip as `overlays`.
- **Captions** (`/api/subtitle` with `preset` + `overrides`): `caption_styles`
  merges the preset with the normalized overrides and
  `subtitles.generate_ass_styled` writes the ASS with `PlayResX/Y` = the real
  frame, every px value scaled by `video_h/1920`, so one preset renders the
  same on 9:16, 1:1 and 16:9. Animations: `highlight` (spoken word recoloured
  via zero-length `\t`), `word_reveal`, `one_word`, `karaoke` (`\k`, with the
  Primary/Secondary colour swap libass needs); glow is a blurred copy on a
  lower layer; `pos_x/pos_y` pin with `\an5\pos` and win over the SPLIT-seam
  `\an5` rule; `offset_x`/`offset_y` (±50 % of the frame) nudge an anchored
  caption while keeping its alignment (`\an<anchor>\pos`), which is what the
  X/Y sliders and dragging the caption on the preview write. The choice is
  persisted on the clip as `caption_style`, and
  `_reapply_captions` / `_clip_layer_hooks` use it, so a later format change
  or trim brings back the user's captions, not `AUTO_CAPTION_STYLE`. A
  legacy-field request clears it.

  Two later additions and one that could not be built. **`pop`** punches the
  spoken word and settles it — scaling `scy` **only**, which is the whole
  design: libass advances text by the scaled glyph width, so a uniform
  `scx`/`scy` pop on a word inside a VISIBLE line shoves the rest of the
  line sideways and back on every word. Measured on a burn, the caption's ink
  box goes 51px → 57px → 51px tall while its x span stays byte-identical at
  339-740. (`word_reveal` gets away with a uniform scale because the words it
  pushes are still at `lpha&HFF&`.) **`slide_up`** is a group animation:
  `\move` + `ad` on the event, sliding 20px (scaled) from wherever the
  caption would have sat, which is why the anchor point is now computed even
  when the caption is not pinned. **`bounce`** was asked for and is absent:
  moving one word vertically inside a line needs a per-span offset, and ASS
  has only `\pos`/`\move`, which are per event — `slide_up` is that motion at
  the level the format can express. The three presets using them are
  `punch_pop`, `hinglish_pop` (bigger `max_chars`: romanised Hindi runs longer
  per word) and `clean_slide`.

  **Caption layout** (`max_lines` 1-2, `max_chars` 8-48, or the `one_word`
  animation) is the shape of the caption rather than its style, and the modal
  offers the three states as one choice at the top of the presets tab: 2
  lines / 1 line / 1 word. They were always reachable — `max_lines` sat at the
  bottom of the position tab and `one_word` among the colour tab's animations
  — which is why "I only get two lines" was a fair report about a setting that
  existed. Two things make it hold: every preset inherits `max_lines: 2` from
  `caption_styles.BASE`, and `choosePreset` used to clear all overrides, so a
  one-line choice reverted the moment the user tried another look; the shape
  keys now survive a preset change. And leaving "1 word" restores the
  animation it displaced instead of dropping to `none`. `max_lines: 1` never
  loses speech — `_styled_group_events` flushes a full line into a NEW event,
  and an empty line accepts a word however long it is. Fonts: the 30 TTFs under `fonts/` are the
  ClipForge set (Google Fonts, OFL); libass finds them through `fontsdir`,
  the dashboard `@font-face`s them from `/fonts/<file>` for the live preview.
  `GET /api/caption-styles` is the single source the modal renders from.

Cinematic implementation notes: glow has no native ffmpeg filter, so it is a
`split` into two branches, one `gblur`red, recombined with
`blend=all_mode=screen` (hence a `-filter_complex` graph rather than a flat
`-vf`); gradient scrims are stacked `drawbox` bars at increasing alpha because
ffmpeg has no per-pixel alpha ramp, same trick `edit_builder.py` uses for
`flash`. `cinematic.normalize()` clamps every field server-side so a bad
request degrades to "no effect", never to a broken filtergraph.

### Key Classes
- `SmoothedCameraman` - Stabilized camera movement with safe zone logic (prevents jitter)
- `SpeakerTracker` - Prevents rapid speaker switching, handles temporary occlusions

### API Endpoints
| Method | Route | Purpose |
|--------|-------|---------|
| POST | `/api/source/check` | Has this source been cut before? (title/size/URL, no file) |
| POST | `/api/process` | Submit video for processing (`language`: whisper code, `auto` or `hinglish`; `transcribe_prompt`: names/terms for the decode) |
| GET | `/api/status/{job_id}` | Poll job status and logs |
| POST | `/api/edit` | Apply AI video effects |
| POST | `/api/subtitle` | Burn captions on one clip: `preset` + `overrides` (caption_styles) or the legacy fields; auto-transcribes dubbed videos |
| GET | `/api/caption-styles` | Presets, themes, override vocabulary, fonts (files under `/fonts/`) |
| POST | `/api/clip/look` | Change a finished clip's output format (re-render from source) and/or cinematic look (`fx_` layer) |
| POST | `/api/hook` | Add text hook overlays |
| POST | `/api/translate` | AI voice dubbing via ElevenLabs |
| GET | `/api/translate/languages` | List supported dubbing languages |
| POST | `/api/social/post` | Post to social media (async upload) |
| POST | `/mcp` | MCP server (JSON-RPC): the pipeline as agent tools |
| POST/GET/DELETE | `/api/keys` | User API keys (cloud mode, session JWT only) |
| DELETE | `/api/account` | Erase the account and everything in it (GDPR art. 17) |

### Agent access (MCP, API keys, webhooks)

- **API keys** (`cloud/api_keys.py`): `osk_...` tokens, sha256-stored, created in
  the dashboard account page. `cloud/auth.get_current_user_optional` accepts
  them (`Bearer osk_...` or `X-API-Key`) and resolves the owner, so metering,
  entitlement, plan priority and job ownership apply to agents with zero
  endpoint changes. Key management itself refuses API-key auth: a leaked key
  cannot mint replacements.
- **MCP server** (`mcp_server.py`, mounted always): stateless Streamable-HTTP
  JSON-RPC at `/mcp` — no SDK dependency, ~3 methods + 8 tools. Each tool calls
  back into this same app in-process (`httpx.ASGITransport`) forwarding the
  caller's auth headers, so it can never drift from the REST behavior. Cloud
  mode 401s without a resolvable user; self-host stays BYOK-open.
- **stdio transport** (`mcp_stdio.py`): the same `handle_message` / `call_tool`
  as a subprocess, for hosts that only launch MCP servers as a command (Glama's
  Dockerfile deployments wrap one; a local client can skip the web server).
  Two invariants: `sys.stdout` is swapped for stderr **before `app` is
  imported**, because the pipeline prints everywhere and one stray line
  corrupts the JSON-RPC stream; and the app's lifespan is entered
  (`router.lifespan_context`), which `ASGITransport` does not do on its own.
- **OAuth for MCP clients** (`cloud/mcp_oauth.py`, cloud mode only): claude.ai
  and ChatGPT connect by URL, so the server publishes RFC 9728/8414 metadata
  under `/.well-known/`, accepts dynamic client registration (`POST
  /oauth/register`, public clients, PKCE S256 mandatory) and bounces
  `GET /oauth/authorize` to the dashboard consent screen (`#/oauth/authorize`),
  because the session JWT lives in localStorage on the frontend host and a
  bare API GET cannot see it. `POST /api/oauth/authorize` (session auth) mints
  the code; `POST /oauth/token` redeems it by **minting an ordinary `osk_`
  key** named after the client and returning it as the access token. No new
  auth path, no refresh tokens: the key shows up in Account → API keys and
  revoking it disconnects the app. The `/mcp` 401 carries
  `WWW-Authenticate: Bearer resource_metadata=...` so clients find the flow.
  `oauth_codes` is in `USER_OWNED_TABLES`; `oauth_clients` deliberately not.
- **Webhooks**: `POST /api/process` takes `webhook_url` + optional
  `webhook_secret` (HMAC-SHA256, `X-OpenShorts-Signature`). Validated with
  `security_utils.assert_public_url` at submit AND at delivery (DNS rebinding).
  Fired once per job from `run_job_wrapper` after the R2 archive so the payload
  can carry durable download links; survives redeploys via the resume manifest.
  `PUBLIC_API_URL` env sets the absolute-URL base when behind a proxy.

### Account erasure (GDPR art. 17)

`DELETE /api/account` (`cloud/account.py`, dashboard: Account → Delete account)
is immediate and irreversible: there is no recovery window because after the
delete there is nothing left to authenticate a recovery request against. It
refuses API-key auth (a leaked `osk_` must not destroy its own account) and
requires the caller to retype the account email.

The order of the steps is the design, and each one is a failure mode:
**Stripe cancel first**, aborting the whole thing if it fails, so we never erase
a user we are still billing; **R2 before the database**, because those rows are
the only index of which objects are theirs and dropping them first turns a
failed purge into permanent orphans; the DB delete is **one transaction** over
an explicit table list (`USER_OWNED_TABLES`) rather than the declared ON DELETE
CASCADEs, since `create_all` never ALTERs an existing table and a constraint
added after a table shipped exists in the models but not in production.
`tests/test_account_erasure.py` fails if a new table references `users.id`
without joining that list.

`app.py` registers a callback for the local working files, which record
ownership three different ways: the `.owner` file clip jobs write (so jobs
recovered from disk after a restart count too), `saas_jobs`, and
`thumbnail_sessions`. That last one is the only thing that ever deletes
generated thumbnails: the hourly sweep skips their directory and they are
served publicly at `/thumbnails/`.

What deliberately survives: the Stripe customer and its invoices (6-year
retention, Spanish commercial law) and one `account_deletions` row holding a
sha256 of the email as proof the erasure happened, itself purged after 5 years.
The "why are you leaving" answer is a closed list (`DELETION_REASONS`), never
free text — anything the user could type would land in a row designed to
outlive them. Deleting users also made one webhook path reachable that never
was before: `_apply_topup` reads the user id from Stripe metadata, so it now
confirms the row still exists before inserting, or the FK violation makes
Stripe retry the same doomed event for three days.

### Concurrency Model
Async job queue with semaphore-based concurrency control. Configure via `MAX_CONCURRENT_JOBS` env var (default: 5). Jobs auto-cleanup after 1 hour.

### Paid proxy accounting (`cloud/proxy_ledger.py`)

Downloads go direct → static ISP proxies (flat rate) → DataImpulse (per GB),
and the duration probe (`cloud/metering.probe_url_minutes`) follows the same
order, with one extra free step before any per-GB attempt: the fallback
clients through a static (`fallback-static`). **The client list is explicit
and shared** (`yt_clients.py`: `default,mweb` + the bgutil PO token
provider): with account cookies yt-dlp's own defaults are `tv_downgraded` +
`web`, and on a share of videos both come back UNPLAYABLE / SABR-only, which
yt-dlp reports as "Video unavailable". That was mistaken for an IP ban for a
week (it happened on every static IP too) and fed ~26 downloads a week to
the per-GB proxy, which then fetched 360p through the same dead list.
Measured in the prod container on 6-sep-2026, same static, same video:
cookies + defaults → unavailable; cookies + `default,mweb` → 1080p; no
cookies → 1080p. `mweb` needs the PO token, and the token needs the
webpage: never put `player_skip: webpage` back. A fallback attempt runs
anonymously when an HD attempt already failed with the cookies on that
route, and every attempt asks for the 1080p format spec (the old
`best[ext=mp4]` fallback spec was itself the 360p progressive file).
Two rules keep the per-GB proxy at zero on a normal day: the probe
reaches it **only** when a static route failed for a reason another IP can
fix (`static_failure_warrants_paid`: bot-check, 403/429, proxy/network
errors), never for a private/removed/members-only video, an uploader's
country block (the residential pool failed identically in 5 of 6 paid
probes, 3-5 sep) or a live stream
with no duration (those failed the same on every IP and used to cost ~1.7 MB
× 2 extractors each), and **never for a non-YouTube URL** (the download
plan already excluded those; Twitch, Kick, Rumble and product pages were
reaching it through the probe). The probe also carries `YOUTUBE_COOKIES`,
like the download does: an anonymous probe from the static IPs gets "Sign in
to confirm you're not a bot" in bursts (4-sep-2026: ~10 probes in one hour,
1.8 MB each on the per-GB proxy) because a datacenter IP's anonymous rate
limit is low and we make ~400 YouTube hits a day from three of them, while
the authenticated download sails through the same IPs. But the **first**
attempt on a route carries them and the second drops them, on the probe as
on the download: with the cookies attached YouTube answers UNPLAYABLE for
every client (`web_embedded`, `tv_downgraded`, `web` **and** `mweb`) on a
share of videos, which yt-dlp reports as "Video unavailable" (9-sep-2026,
same video on all three statics; anonymous on the same IP → 1080p 137+140).
Without that anonymous second attempt the probe read a cookie problem as an
IP problem and escalated to the per-GB proxy, which carries the same cookies
and fails identically, while the download recovered for free on the same
static. `main.py` prints `PROXY_ROUTE=<json>` after
every download (winner, paid bytes across all attempts including failed
paid ones, each free attempt's error); `app.py` persists it as a
`proxy_usage` row at job end and pages Telegram when the paid proxy carried
bytes, folding a burst into one message per 5 min. The in-memory monthly
counter and the container log (rotates within the hour) cannot answer "what
cost $14 on the 28th"; the table can. `PAID_PROXY_DAILY_MB` (default
500) is the hard ceiling: past it the paid proxy is dropped from the probe
and from every new job's env until UTC midnight. The watcher probes the
static pool against a real YouTube watch page (playable markers), not
google.com — the 28th happened because YouTube refused the static IPs while
google kept answering 204. On the dev Mac, do not keep
`PROXY_URL` in `.env`: every local `main.py` run then bills DataImpulse.

### Deploys and running jobs (handover + drain)

Every push to `main` redeploys the API container. Coolify starts the NEW
container before stopping the old one (rolling update) and both share
`output/`, so `app.py` coordinates them instead of relying on a fast swap:

- Each instance writes its id to `output/.instance` at startup. An instance
  that sees another id there is the old one and **drains**: it finishes the
  jobs it is running, starts none, and leaves queued manifests on disk.
- A running job heartbeats its `.resume.json` every 10 s. The resume scan
  (startup + every 30 s) re-enqueues only manifests nobody heartbeated for
  60 s, so no job runs twice and none is lost. Max 2 resume attempts.
- SIGTERM (`docker stop`) drains too, up to `DRAIN_TIMEOUT_SECONDS` (840),
  then hands the signal to uvicorn. The app's Coolify stop grace period is
  900 s (`application_settings.stop_grace_period`); keep the timeout below it.
  After the drain hands the signal to uvicorn, `--timeout-graceful-shutdown 15`
  (Dockerfile) caps the wait for in-flight connections: uvicorn's default is
  unbounded, and one open range download kept a drained container alive for
  the full grace period while Traefik still routed half the traffic to its
  closed port.
- `/health/ready` + the Dockerfile `HEALTHCHECK` are what keep Traefik off a
  dying container: its docker provider only routes to `healthy` containers,
  so an instance answers 503 from the moment it gets SIGTERM (out of rotation
  within ~10 s, socket still open) and a booting one gets no traffic until it
  answers. Only SIGTERM flips it, not the marker drain: at that point the new
  container is still booting and nobody else would be routable. The Coolify
  app has its health check enabled on that path so it waits for the new
  container to be `healthy` before stopping the old one. With that option on,
  Coolify replaces the Dockerfile HEALTHCHECK with its own curl/wget command
  AND its own interval/retries (5 s × 3), so the image must ship `curl` or
  every deploy rolls back as unhealthy, and a stopping container takes 15 s
  to turn `unhealthy`. That is why the drain keeps serving for
  `PROXY_DRAIN_SECONDS` (20) after the jobs are done before it hands the
  signal to uvicorn: closing the socket earlier is 502s until Traefik
  notices (measured ~60 s per deploy with retries=12 and no grace). And
  `HARD_EXIT_SECONDS` (30) after that the process is ended outright: uvicorn
  finishing does not end the interpreter while an executor thread hangs in
  a network probe, and that kept a drained container alive for the full 900 s.
  `/health` stays a plain liveness probe for the external watcher.
- `/api/status` answers from disk for a job this instance never held, so a
  poll landing on either container during the handover is fine.
- `main.py` leaves `.transcript_checkpoint.json` in the job dir so a job that
  does get re-run skips the paid transcription (download and Gemini repeat).

Before pushing, still batch small commits (tests, docs) with the next real
change: every deploy is a ~5 min build plus a handover.

## CI: a green pipeline closes the task, not the push
- After every `git push`, wait for the commit's workflow and confirm it is green: `ci-wait` (Victor's Mac) or `gh run watch $(gh run list -c $(git rev-parse HEAD) -L1 --json databaseId -q ".[0].databaseId") --exit-status`.
- If it is red: fix, push, check again. Never report the task as done with a red CI.
- Before pushing, run locally what the CI runs (lint + tests of this repo).
