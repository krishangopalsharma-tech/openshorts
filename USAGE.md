# Using OpenShorts

How to actually run this thing and get clips out of it. For *why* it works the
way it does, see `CLAUDE.md` — that file carries the measurements and the
reasoning; this one is the manual.

---

## 1. Start it

```
F:\openshorts\start-dev.bat
```

Backend on port 8000, dashboard on 5173, browser opens by itself.
`stop-dev.bat` kills both.

**Use `http://127.0.0.1:5173`, not `localhost`.** With Docker Desktop running,
its port forwarder also listens on 8000 over IPv6, `localhost` resolves to
`::1` first, and the dashboard's API calls land on the wrong listener. The
symptom is a job that stays "queued" forever with no error.

Manual start, if you prefer two terminals:

```
venv\Scripts\uvicorn app:app --host 0.0.0.0 --port 8000
cd dashboard && npm run dev
```

---

## 2. Make clips

There are two routes. They differ only in **who chooses the moments**.

### Route A — the dashboard

Drop a file or paste a URL, then **Generate**. Everything else is decided per
clip afterwards, so the job just delivers plain 9:16 clips.

Advanced options worth touching:

| option | set it when |
|---|---|
| **spoken language** | Hindi audio → `hinglish`. See §5 — this is not cosmetic |
| **names & terms in this video** | a name is mangled **in the first 30 seconds**. See §5 |
| **vertical layout** | leave on `auto`. `none` disables the AI layout picker |
| **number of clips** | you want an exact count |

### Route B — the chat route (no API cost)

Your GPU does the transcription and rendering; **your Claude or ChatGPT
subscription picks the moments**. No API key, no per-token billing.

**Step 1 — transcribe and export the brief:**

```
venv\Scripts\python main.py -i "uploads\video.mp4" -o output\myjob --audience in --transcribe-only
```

Stops before any picking or rendering and writes into `output\myjob\`:

| file | what it is |
|---|---|
| `paste_into_chat.txt` | **the one you use** — full instructions + timestamped transcript |
| `transcript_for_ai.txt` | the transcript alone |
| `transcript.json` | full transcript with per-word timings |

The brief tells the model **how many clips to pick**, scaled to the length of
the video — a 60-minute episode asks for 6 to 12, a short one asks for fewer.
It is the same band the Gemini path uses, and `CLIP_TARGET_MIN` /
`CLIP_TARGET_MAX` override it here too. Nothing caps the render, so if you
want more than the brief suggests, just ask the chat for more and save the
longer reply.

**Step 2 — paste it into the chat.** Open `paste_into_chat.txt`, select all,
paste. You write nothing yourself; the file already contains the role, the
audience profile, the clip rules and the required JSON shape. A 72-minute
video is about 19k tokens, which fits comfortably. If one ever does not,
upload the file instead of pasting.

**Step 3 — save the reply as `clips.json`, verbatim.** No cleanup needed. The
parser tolerates:

- ` ```json ` fences
- prose before and after (it reads from the first `{` to the last `}`)
- `83.5`, `"83.5"`, `"1:23.5"` or `"0:01:23.5"`
- `{"shorts": […]}`, `{"clips": […]}` or a bare `[…]`
- missing optional fields — only `start` and `end` are required

**Step 4 — render:**

```
venv\Scripts\python main.py -i "uploads\video.mp4" -o output\myjob --clips clips.json
```

`--clips` is resolved against the directory you are IN, not the job folder, so
give it the full path — `--clips output\myjob\clips.json`, not `clips.json`.

Use the **same `-i` and `-o`** as step 1, or it re-transcribes. Each clip is
snapped to real word boundaries, so rough timestamps are fine, and Gemini is
skipped entirely. A clip with a broken `start`/`end` is skipped with a
warning; the others still render.

**What it saves:** the picking cost only. Transcription still takes the same
~25 minutes on a 72-minute video either way.

**What you can do with the JSON:** edit it by hand before step 4 — change a
timestamp, delete a clip, rewrite a title. Or iterate in the chat ("clip 2
cuts before the punchline, extend to 0:48") and re-save.

### The same thing from the dashboard, no command line

On the idle screen, under the uploader: **"Use a file on this computer, or
picks from a chat"**.

| control | |
|---|---|
| **Browse…** | walks this machine's drives and folders; click a video to choose it. Nothing is uploaded — it is read where it sits |
| **Choose clips.json…** | an ordinary file picker. Optional: without it the AI picks the moments |

The two use different mechanisms on purpose. A browser **never** tells
JavaScript a file's path (`File.path` is Electron, not the web), so a video
that must not be uploaded can only be named by walking the SERVER's
filesystem — hence `GET /api/local/browse`. clips.json is small, so its picker
reads the file's **contents** in the browser and posts the JSON itself; no
path involved, and it works wherever the file happens to sit.

It checks both paths and parses the picks **before** queueing, so a wrong path
or a broken JSON says so immediately instead of forty minutes into a render.
The job then behaves like any other: progress, clip cards, and every per-clip
tool.

Self-host only. The endpoint reads local filesystem paths, so it does not
exist when `BILLING_ENABLED` is set — on a hosted instance it would let a
visitor name a path on the server.

### Seeing CLI clips in the dashboard

A job rendered from the command line writes to disk but the dashboard never
started it, so it does not appear on its own. On the idle screen, under the
uploader, open **"Open clips from a job made outside this window"** and paste
the job id — the folder name under `output/`. The clips load with every
per-clip tool available: subtitles, format & look, music, overlays,
download-all.

No restart needed: `/api/status` reads the job directory when the id is not in
memory. The one exception is an id the backend already holds in a different
state — for example a dashboard job that failed, which you then re-rendered
into the same folder from the CLI. Restart the backend and it reads from disk.

### CLI reference

```
-i, --input PATH        local video
-u, --url URL           YouTube URL
-o, --output DIR        job directory
--audience {auto,in,us} channel profile; auto = by transcript language
--transcribe-only       write paste_into_chat.txt and stop
--clips FILE            render these picks, skip the AI picker
--transcript FILE       reuse a transcript, skip transcription
--format {auto,vertical,horizontal,square}
--keep-original         keep the downloaded YouTube file
--skip-analysis         convert the whole video, no clipping
```

---

## 3. Style each clip, after generation

Nothing is burned in at upload. Every clip card offers:

| control | what it does |
|---|---|
| **format & look** | 9:16 / 1:1 / 16:9, plus grade, glow, grain, vignette, scrims, letterbox |
| **subtitles** | the caption studio (§4) |
| **music** | background track, automatically ducked under speech |
| **overlays** | logos and text blocks placed with the mouse |

Each is a layer. Changing one re-derives it and everything above it while
keeping what is below — a music change never re-grades the clip, and captions
survive a trim or a format change.

"Apply to all" loops the same change over every clip in the job.

---

## 4. Captions

### Shape — first control in the *presets* tab

**2 lines / 1 line / 1 word.** It survives changing the look; it used to reset
silently when you picked a different preset, which is what made it look
broken. One line never loses speech — a full line becomes a new caption event
rather than being truncated.

### The 22 presets

Bold White (default), Karaoke Yellow, Minimal, Hormozi Green, Hormozi Yellow,
Beast Pop, Raj Shamani Clean, Alex Bold Caps, One-Word Punch, Word Reveal,
Bebas Clean, Comic Punch, Slab Impact, Marker, Serif Elegant, Neon Pop, Boxed,
Oswald News, Green Word, **Punch Pop**, **Hinglish Pop**, **Clean Slide**.

The last three are new:

| preset | for |
|---|---|
| **Hinglish Pop** | the Hindi channel — wider character limit, because romanised Hindi runs longer per word than English |
| **Punch Pop** | English, big and punchy, one line |
| **Clean Slide** | quiet — no colour change, no scaling, just the group easing in |

### Animations (*colour* tab)

`none`, `highlight`, `word_reveal`, `one_word`, `karaoke`, plus **`pop`** (the
spoken word punches and settles) and **`slide_up`** (each caption group slides
up and fades in).

There is no `bounce`: moving one word vertically inside a line needs a
per-span offset and the ASS format has only per-event positioning. `slide_up`
is that motion at the level the format can express.

### Placement

Drag the caption on the preview, or use the X/Y sliders. Anchor is top /
centre / bottom. One preset renders identically on 9:16, 1:1 and 16:9 because
every pixel value is scaled by the real frame height.

### Themes

Five override bundles — classic, clean, bold, neon, typewriter — applied on
top of whichever preset is selected.

---

## 5. Language and transcription

### Naming the language matters on Hindi

Set **spoken language** to `hinglish` for Hindi audio. Two reasons:

1. Auto-detect on Hindi slides into English **translation** partway through a
   file — the original bug was a Hindi video whose clips came back with
   English captions.
2. `hinglish` transcribes Hindi then romanises it, which is what the audience
   types and what the caption presets can draw. Every preset is a Latin
   display face, so Devanagari falls out of the chosen style into whatever
   font libass can find.

Hindi and Hinglish jobs automatically use the `large-v3` whisper model even
though `.env` says `large-v3-turbo`; turbo is measurably worse on Hindi and
code-switching. English keeps turbo, where it is unaffected and faster.

### "names & terms in this video" — read this before using it

It only reaches the **first 30 seconds** of the video. That is a whisper
limitation, not a bug here: the repo sets `condition_on_previous_text=False`,
which makes faster-whisper drop the prompt after every window.

The obvious fix (`hotwords`, which does persist) was measured and **rejected**
— it dropped ~52 seconds of a 110-second Hindi slice and drifted toward
English.

So: use it for a name spoken early. Keep it to a few proper nouns, **in Latin
script** — a prompt written in Devanagari gets echoed back verbatim as the
transcript. Expect nothing from it at minute 40.

### Vocal isolation

`TRANSCRIBE_VOCALS=1` (currently **on**) strips laughter, crowd and music
before transcribing. On a comedy source the raw mix returned 60 seconds as two
run-on segments and lost an entire exchange including its punchline, where the
isolated voice returned all eighteen lines.

Costs about 10 extra minutes on a 72-minute video. Set it to `0` for clean
studio audio, where it gains nothing and can slightly soften a clean line.

---

## 6. Things that happen automatically

### Duplicate-video warning

Pick a video you have already cut and a warning appears above the Generate
button with the clip count and date. Tick "Generate clips from it again
anyway" to override. It is never a hard block.

It matches three ways: the YouTube id, the normalised title, or size plus
duration — so it still catches the same file under a different name.

### `picked_by`

Every clip records who chose it: `gemini:<model>`, `local:<model>` or
`claude`. It is in `metadata.json` and in the CSV inside the download-all ZIP.

**This is for a comparison you have to run yourself:** cut some videos through
the dashboard and some through the chat route, then after 48 hours compare
views and viewed-vs-swiped grouped by `picked_by`. That is the only way to
learn whether your subscription picks better than Gemini, and it cannot be
reconstructed later, which is why it is recorded from the start.

### Resume

A job interrupted by a restart is re-queued and skips the transcription it
already paid for.

---

## 7. Settings (`.env`)

Currently set:

| setting | value | meaning |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.7-flash` | picks moments **and** writes titles and hook text |
| `WHISPER_MODEL` | `large-v3-turbo` | English; Hindi auto-upgrades to `large-v3` |
| `WHISPER_DEVICE` / `WHISPER_COMPUTE` | `cuda` / `float16` | GPU transcription |
| `TRANSCRIBE_VOCALS` | `1` | strip noise before transcribing |
| `FFMPEG_ENCODER` | `auto` | NVENC when available, else libx264 |
| `ASR_GPU_CONCURRENCY` | `1` | one transcription at a time on the shared GPU |

Useful but unset:

| setting | what it would do |
|---|---|
| `AUDIENCE` | force `in` or `us` instead of detecting from language |
| `WHISPER_MODEL_HINDUSTANI` | override the Hindi model (default `large-v3`) |
| `VOCALS_MODEL` | override the Demucs model (default `htdemucs`) |
| `LLM_BASE_URL` + `LLM_MODEL` | run the moment picker on a local model (Ollama, LM Studio, vLLM) instead of Gemini |
| `HF_TOKEN` | higher Hugging Face rate limits; only needed for new or gated models |

Never commit `.env` — it is gitignored and holds your keys.

---

## 8. Audience profiles — the one thing still worth your time

`audience_profiles.py` holds a block of rules per channel (`in` / `us`) that
is prepended to **both** Gemini prompts, and to the chat-route brief. It is
chosen automatically from the transcript language, or forced with
`--audience`.

Right now it states only what is **known**: the language, the Roman-script
rule, the clip-length band, and what any clip must do to stand alone. It used
to also assert things about your viewers — "relatable desi situations: family,
shaadi, office, boss, salary, padosi", "the viewer should want to forward it
on WhatsApp" — which nobody had measured. Those were removed, because a
confidently wrong prompt is worse than a plain one: the model follows it.

**To make it good, add a `WHAT WINS` section from your own numbers** — the top
and bottom clips by viewed-vs-swiped, not from taste. That text goes straight
into the prompt that writes every title and hook, so it is the highest-leverage
edit available to you. `tests/test_audience_profiles.py` fails if the old
placeholder words come back.

---

## 9. When something goes wrong

| symptom | cause |
|---|---|
| job stuck at "queued" forever | using `localhost` instead of `127.0.0.1` (§1) |
| "Error starting job: Internal Server Error" | the backend is not running — `start-dev.bat` |
| transcription log frozen at 25% | not frozen. Progress prints only at 25/50/75/100%, so a 24-minute transcription updates four times |
| `cublas64_12.dll not found` | CPU-only torch installed; reinstall the CUDA build |
| captions in Devanagari, wrong font | language was `hi`, not `hinglish` (§5) |
| double captions | the source video already has burned-in subtitles — common on reposts |
| 4 failing tests locally | pre-existing and Windows-only (cp1252 decoding, an R2 path, subprocess timeouts because this machine has the full ML stack that CI omits) |

Check a job's real state with the GPU rather than the log: `nvidia-smi` will
show utilisation while it is working.

---

## 10. Measured and rejected — do not re-try these

Written up with numbers in `CLAUDE.md`. All four were tried properly and lost:

| | why |
|---|---|
| **WhisperX** | fixes word *timing*; the Hinglish problem was word *identity* |
| **Forced alignment** | 138 ms median disagreement, and the only ungated Hindi aligner is worse than the thing it would correct |
| **Hindi fine-tuned whisper** | hallucinates All India Radio sign-offs into silence — a failure a WER score hides |
| **pycaps caption engine** | 6-7x slower than the current pass and no better at timing |
| **`hotwords`** | dropped ~52 s of a 110 s Hindi slice |
