# Film modules: Movie Shorts + Movie Recap — consolidated plan

Rev 6 (consolidates `movie-shorts-plan` rev 5, `movie-recap-plan` rev 2 and
rev 3). For Claude Code sessions in the `mutonby/openshorts` working tree.
Self-host only (`BILLING_ENABLED` unset): the film never leaves the machine,
only an SRT digest goes to Claude or ChatGPT in a chat window.

Two modules, one shared render layer:

| | Movie Shorts | Movie Recap |
|---|---|---|
| Unit | one standalone ~120 s short | a 3-part series, ~150 s each |
| Coverage | one angle, beats from anywhere in the film | the first 75% of the film, in order |
| Audio | film dialogue as captions + music bed, no narrator | user-recorded voiceover, bed muted by default |
| Ending | pays off inside the short | never shown, never described: the series sells the film |
| Per film | 5–12 shorts | one series |
| Planning passes | hooks → beats | spoiler map → structure → chunks (per part) |
| Module | `movieshorts.py` | `movierecap.py` |

Both plan in a chat window (no server-side LLM call) and paste JSON back.
Both render through `film_prep.py`. Build Shorts first: it exercises every
shared piece, and Recap is then a planning layer plus voiceover.

---

## 0. What the chat plans got wrong about this tree

The plans were written against an older reading of the repo. Verified on
17-sep-2026:

| Plan said | Tree has | Consequence |
|---|---|---|
| "`sidechaincompress` appears nowhere" | `music.build_audio_graph()` ducks a track under the voice with `sidechaincompress`, loops, fades, `amix normalize=0` | Reuse it. Shorts needs a `key="dialogue"` variant (ratio 6, LRA 7); Recap needs the VO-keys-bed variant (ratio 12). Two parameters, not a new mixer. |
| "Selectable aspect ratio is real work, own session first" | `output_format` = `vertical`/`square`/`horizontal` already flows through `recut.perform_recut` → `reframe_v2`, `/api/clip/look`, `_clip_output_format` | Session 2 shrinks to: verify 1:1 on a 2.39:1 source, add `fps` (60) to `video_encode_args`, and check `subtitles.generate_ass` scaling on square. |
| Grades in a new `film_prep.GRADES` | `cinematic.COLOR_GRADES` (warm/cool/vintage/vibrant) | Add `washed` and `cold` there; reuse `cinematic.apply_cinematic_effects` per shot. One table for the whole product. |
| "Animated caption styling missing" | `caption_styles` has `pop` (scy-only punch), `highlight`, `word_reveal`, glow, 30 bundled fonts; `subtitles.generate_ass_styled` scales to any frame | Add one preset `film_pop` (Anton, amber highlight, glow). No new ASS code. |
| Voiceover re-timing needs new code | `compilation.py`: `transcribe_vo`, `align_lines_to_words`, `pad_and_split`, `fit_shots`, `render` | Recap session 9 wraps `compilation`, it does not rewrite it. Word-aligned VO beats proportional rescaling. |
| Punch-in exists | `punch_in.py` is a ~12% animated push on beats | Add a static mode: `crop_boxes()` with a constant zoom per shot. |

Genuinely missing: SRT/VTT parsing, a speed change (`setpts`/`atempo`), a
music **library** (manifest, BPM, loudness, `pick()`), beat detection, static
per-shot punch-in, selective blur. Six things, all bounded.

---

## Status (17-sep-2026, evening)

**Movie Shorts was removed the same day it was first rendered.** The real
render of "The preacher hunts rebels with a rifle" (47 shots, 1.21 s mean
cut, 2.39:1 source) came out as a vibrating picture with glitching captions:
the crop ladder changes scale every ~1.4 s inside one continuous take and
after the reframe that reads as shaking, and the per-word pop rides subtitle
cues whose word timings are only evenly spread. Decision: shorts from a film
go through the existing clip maker (real reframe, ASR-timed captions). Gone:
`movieshorts.py`, `beat_grid.py`, `music_library.py`, `MovieShortsTab.jsx`,
the shot expansion in `film_prep`, the shorts routes in `film_api`, their
tests. Kept: SRT ingest and offset probe, the chat-planning pattern,
`film_render` (recap only), `music.MIX_PROFILES`, the `film_pop` preset and
the `washed`/`cold` grades. Sections 2.x below are history, left as written;
`0c58b66` has the code.

**Generated voiceover (Kokoro, §3.8) is built and measured.** Machine Gun
Preacher part 1, `am_michael`: at 1.0x, 411 words → 173 s (all 18 lines
fitted via head/tail room, none sped up); at 1.1x → 165.9 s narrated part in
141 s of render; VRAM flat at 3.75 GB through synthesis (CPU server). Default
`tts_speed` is 1.1. Trap found: Kokoro-FastAPI writes a streaming WAV header
(data length 0xFFFFFFFF), so durations are measured from bytes on disk.

Built in this tree, tests green (`tests/test_film_*.py`, `test_movierecap.py`):

| Session | State |
|---|---|
| 1 film_prep (parse, digest, offset probe, speed) | done |
| 2 aspect/fps | smoke-rendered on `demo-openshorts.mp4` (1:1, 60 fps) through `main.render_clip`; a 2.39:1 feature still to be checked |
| 3 movieshorts prompts + validator | done |
| 4 expand_beat + static punch-in + `/shots` | done (`film_prep.expand_plan`, `crop_filter`) |
| 5 shorts render | done (`film_render.render_short`) |
| 6 music_library | done; `assets/music/<mood>/` is empty until tracks are curated |
| 6b beat_grid + schedule + dialogue guard | done |
| 7 MovieShortsTab | done |
| 8 movierecap prompts + validator + lint + budget | done |
| 9 recap render + VO via compilation.py | done (`film_render.render_narrated`), not yet run on a real recording |
| 10 MovieRecapTab | done |
| 11 docs | this file + CLAUDE.md section |

Not done, in order of value: run passes A/B/C and hooks/beats by hand on one
real film and read the result; curate 10-15 tracks per mood and scan; a real
voiceover through session 9; composite shots are planned (`composite: true`)
but rendered as plain punch-ins until `split_layout` is wired per shot;
emphasis-word colouring in `film_pop` (the preset pops every spoken word).

## 1. Shared layer

```
film_prep.py      SRT/VTT parse -> internal transcript, digest, offset probe,
                  speed(), expand_beat()/expand_plan(), crop_filter()
film_render.py    assembly: cut (crop+grade) -> concat -> speed -> reframe ->
                  captions -> mix -> loudnorm/fps; render_narrated() for recap
film_api.py       /api/film/*, /api/movieshorts/*, /api/movierecap/*; sessions
                  under output/film/<id>/; renders in threads; /film/ mount
music_library.py  scan() manifest over assets/music/<mood>/, pick(), MOOD_RECIPES
beat_grid.py      onset envelope, tempo, DP beat tracker, downbeats, phase lock,
                  schedule() with the dialogue guard and the fallback ladder
```

### 1.1 Ingest

- `/api/uploads` accepts `.srt` and `.vtt` alongside video.
- **No transcription.** `film_prep.parse_subtitles(path)` returns the internal
  shape `{"language", "segments": [{"start","end","text","words"}]}` with
  words spread evenly across each cue, so `recut.remap_transcript` and
  `subtitles.generate_ass_styled` work unchanged.
- **Offset probe.** An SRT for another release runs 2–25 s out. Transcribe
  ~60 s around the 25% mark with `small` (never `large-v3`: 8 GB card),
  cross-correlate word onsets against cues in that window, report
  `offset_seconds` and a confidence; the session stores a manual override.
  In Shorts the error is visible (captions miss lips), so the tab shows it
  before anything else.
- **Digest.** Drop cues under 3 words, merge cues under 1.2 s apart, bucket
  into 2-minute blocks with `[00:14:00-00:16:00]` headers, `MM:SS` times.
  Target 8–12k words. Per-band digests for passes that only need one act.

### 1.2 Speed

`film_prep.speed(in, out, factor=1.25)`: `setpts=PTS/1.25`, `atempo=1.25`.
**Speed before reframe.** `reframe_v2` emits `sendcmd` crop timelines; a PTS
change afterwards lands every command on the wrong frame. Caption timings
divide by the factor after the speed step, or they drift: invisible at 20 s,
wrong by 120 s.

### 1.3 Assembly order (both modules)

```
0. pick music bed              music_library.pick        (Shorts; Recap: none)
1. beat grid on the bed        beat_grid.analyse (cached in manifest)
2. expand beats -> shots       film_prep.expand_beat     (Shorts only)
3. cut each shot/chunk         recut.cut_commands pattern + crop + grade
4. concat                      recut.concat_command (-c copy)
5. speed 1.25x                 film_prep.speed
6. reframe to ratio            reframe_v2 via recut.perform_recut(output_format)
7. burn captions               generate_ass_styled, timings / 1.25
8. mix                         music.build_audio_graph variant
9. loudnorm, fps               ffmpeg_utils.LOUDNORM_FILTER, -r 60
```

Rules that are each a debugging day: music bed after concat, never per
shot (restarts the track on every cut); steps 0–1 before any cutting when
`beat_sync` is on (the grid decides the shot schedule); `-ss` before `-i`
(input seeking; `recut.cut_commands` already does this); keep
`ffmpeg_utils.mark_ai_generated`.

### 1.4 Hardware budget (RTX 2070 8 GB, Ryzen 7, 32 GB)

Nothing loads a model during a render except YOLO + MediaPipe, which the
pipeline already runs. Turing caps concurrent NVENC at 2–3: `NVENC_PARALLEL`
(default 2) lanes when the encoder is NVENC, `CPU_PARALLEL` (default 4) when it
is libx264. **Every shot uses the same encoder.** The chat plan's idea of
splitting the lanes between GPU and CPU was tried on 17-sep-2026: the mixed
parts differ in SPS/profile, the `-c copy` concat joins them without a word,
and the speed pass then fails on the result with an empty stderr. ~86
intermediates per short: job dir on SSD, `film_render` removes them in its
`finally`.

---

## 2. Movie Shorts (`movieshorts.py`)

### 2.1 Reference, measured

Five clips from the reference channel: 720×720, 60 fps, 120–128 s, 72–92
cuts, average shot 1.33–1.74 s, audio median −12 to −15 dB with 0.1% of
frames below −40 dB. **No beat sync** (phase lock 0.26–0.30; random ≈ 0.31);
cuts land at ends of dialogue lines. ~15 story beats per clip, each shown as
5–8 shots of the same scene at different crop scales. Heavy punch-in, warm or
washed grade, word-animated captions carrying the film's dialogue, occasional
two-shot composite, selective circular blur.

### 2.2 Beats and shots

```
LLM plans BEATS        14–18 per short, 8–15 s source each
Renderer expands SHOTS each beat -> 5–8 shots of 1.2–1.8 s, varied crops
```

Per short: 120 s finished (90–150 configurable), 1.25×, 150 s source, ~86
shots at 1.4 s finished = 1.75 s source. The model never sees "shot".

`film_prep.expand_beat()`:
1. Scene detection inside the beat (`scene_detection.detect_scenes` on the
   cut range): real cuts become shot boundaries first.
2. Subdivide remaining stretches; each sub-shot gets a different crop.
3. Crop ladder cycled, not random: `[1.00, 1.45, 1.15, 1.70, 1.25, 1.55]`,
   anchor drifting between face centre (existing tracking) and thirds.
   Never two equal scales adjacent; never above 1.8 (720p source artefacts).
4. Reserve 10–15% of shots for a composite (`split_layout` / `panel_layout`)
   on `weight: "key"` beats where `composite_ok`.
5. Jitter length 1.2–1.8 s; first shot of a beat and the key-line shot
   slightly longer.

### 2.3 Prompts

**Pass 1, hooks (once per film).** Standalone shorts, complete on their own.
A hook is one angle on the whole film. Output: premise (setup only), `{HOOK_COUNT}`
hooks with `title` (a present-tense statement under 8 words: "He has a dual
personality"), `angle`, `payoff`, `cold_open` (timestamped, must make the
title make sense within two seconds), `spans` (≥12 min total), `mood` (one of
tense/ominous/sad/epic/romantic/eerie/driving), `strength`. Titles must not
contain Part, Explained, Recap, Summary, Ending, Full Story. No two hooks share
a payoff.

**Pass 2, beats (once per hook).** 14–18 beats, 8–15 s each, ascending,
non-overlapping, inside `spans`, total `{FOOTAGE_BUDGET}` ±8%; first beat
contains `cold_open`, last delivers `payoff`. Each beat: `why`, `captions`
(`at`, `text` from the digest, at most one `emphasis` word), `weight`
(`normal|key`), `composite_ok`. Band digest only.

Full prompt text lives in `movieshorts.PROMPTS` and is emitted by
`GET /api/movieshorts/prompt/{session}?pass=hooks|beats&hook=N`.

### 2.4 Validation: `movieshorts.BeatPlan`

Hard-fail, returned as a structured error list the tab renders so corrections
paste straight back to the model:

- 14–18 beats; each 8–15 s; ascending; non-overlapping; inside a span and `[0, duration]`
- footage within ±8% of `target_duration × speed`
- first beat contains `cold_open`
- every beat ≥1 caption; every caption `at` inside its beat
- **caption text fuzzy-matches a real cue in that window (`difflib` ratio ≥ 0.8).**
  Models invent plausible dialogue confidently; without this you burn
  subtitles nobody said.
- `emphasis` is a word in the caption

### 2.5 Render specifics

- Per-shot crop + grade at step 3. Grade from `cinematic.COLOR_GRADES` +
  new `washed` (`eq=saturation=0.78:contrast=1.04:brightness=0.03`) and
  `cold` (`eq=saturation=0.95:gamma_b=1.08:gamma_r=0.95`).
- Ratio `9:16 | 1:1` → `output_format` `vertical | square`. 1:1 first: a
  2.39:1 source cropped to 1:1 keeps 42% of the width against 23% for 9:16,
  which is why the reference can punch in that hard and hold a two-shot.
- Output 1080×1920 or 1080×1080 at 60 fps.
- Captions: preset `film_pop` in `caption_styles` (Anton 58, white fill,
  amber `&H00A0FF&` highlight on the emphasis word via the existing `pop`
  animation, `bord 3.5`, glow layer, `an2` with `MarginV` ~18%).
- Selective blur: optional per-shot `blur: {x,y,w,h}` → `gblur` on a
  cropped region, set by hand in the shots preview. Advertiser tool, not a
  copyright one.

### 2.6 Music library (`music_library.py`)

**Never generated, never fetched during a render.** Curated folder,
`assets/music/<mood>/`, read through `manifest.json`. Reasons: the GPU is
spoken for, and a downloaded track carries a licence you can cite.

Sources to verify (terms change): Pixabay Music (Pixabay Content Licence,
no attribution; user-uploaded, so not a Content ID guarantee), CreatorMix
(read the licence page once, record the answer). Manifest per track:
duration, BPM, integrated loudness, mean flux energy, `loop_safe`,
`beat_confidence`, `downbeat_phase`, `source`, `licence`,
`attribution_required`, `url`, `uses`.

What a usable bed is: no vocals, no lead in 300 Hz–3 kHz, steady (5th
percentile within 20 dB of median), no signature hook, ≥2 min or loop-safe,
one mood. `python -m music_library scan` computes everything offline, flags
(does not delete) tracks under 60 s, too dynamic, or short and not loop-safe,
and writes a loudness-normalised copy under `assets/music/.normalised/`.

`MOOD_RECIPES` (seven moods → primary/alternate search terms, instrument
hints, BPM band) feeds `GET /api/movieshorts/music-hint?mood=` and the tab
when a fallback fires.

`pick(mood, duration, beat_sync)`: filter by mood → with `beat_sync`, BPM
90–150 and `beat_confidence ≥ 1.5` are hard filters → prefer a track that
covers the short, else `loop_safe` with a 1.5 s crossfade → sort by `uses`
ascending, increment → fall back to the adjacent mood (`tense→ominous`,
`sad→romantic`, `eerie→ominous`, `driving→epic`), mark it, never fail.

Mix (dialogue is the sidechain; the bed dips, it does not vanish):
`volume=-18dB`, `sidechaincompress threshold=0.05 ratio=6 attack=8 release=260`,
`amix duration=first`, `loudnorm I=-14 TP=-2 LRA=7`. This is
`music.build_audio_graph` with three numbers changed; add a `profile`
argument rather than a second graph builder.

### 2.7 Beat-aligned editing (`beat_grid.py`)

`beat_sync: "beat" | "energy" | "free"`, default `"beat"`. The reference does
**not** do this; it is the one visible difference between templated and
authored.

**The coordinate problem.** Beats live in finished time; cuts are decided in
source time before the 1.25× step. Plan forward: schedule shots in finished
seconds against the grid (a shot is `k` beats), convert each to source by
`× speed`, walk each story beat's source range allocating in order, never
round in source time. If a beat cannot absorb its allocation, shorten the
last shot and log; do not rescale.

Detection, numpy/scipy only: spectral-flux onset envelope (`hop=256`,
`sr=22050`), autocorrelation tempo with a log-Gaussian prior at 120 BPM over
60–200, Ellis DP beat tracking (`tightness≈100`), `beat_confidence` = onset
strength at beats minus at random positions in SD, downbeats by the 4/4
phase maximising onset energy. All cached at `scan()`.

Admissible tempo: shots of 1.2–1.8 s need an integer `k` with `k·60/bpm` in
band → 90–150 BPM works, 100–140 has two `k` values and is the sweet spot.

Schedule: story beats start on a downbeat (`key` must, normal should);
cycle `k` over `[3,3,2,4,3,2]`; key-caption shot gets `k+1`; crop ladder
keeps its own period (shared period = visible pulse).

Dialogue guard (`dialogue_first`, default): a boundary inside a caption word
shifts up to 120 ms outside it, else moves a whole beat. Report nudges; >15%
means the bed fights the dialogue rhythm, pick another track. `strict` for
montage.

Accents: snap the emphasis pop to the nearest beat within 100 ms; 2-frame
~103% punch on the downbeat of `key` beats only, ~3 per short.

Fallback ladder, reported in the render result: `beat` → `energy` (shot
length inversely with 2 s rolling onset energy) → `free` (jitter). Drones
(`eerie`, `ominous`) legitimately land in `energy`.

Acceptance: phase lock `r = |mean(exp(iφ))|` of detected cuts against the
grid. `r > 0.80` locked; `≈0.30` not; decaying across the short = the
coordinate bug. Regression test: `r` over first and last 30 s within 0.1.

### 2.8 Endpoints

| Method | Route |
|---|---|
| POST | `/api/movieshorts/session` — film + SRT → duration, cue count, offset |
| GET | `/api/movieshorts/prompt/{session}?pass=hooks\|beats&hook=N` |
| POST | `/api/movieshorts/hooks/{session}` |
| POST | `/api/movieshorts/beats/{session}/{hook}` — validate |
| GET | `/api/movieshorts/shots/{session}/{hook}` — expanded plan, editable before render |
| POST | `/api/movieshorts/render/{session}/{hook}` — `ratio`, `fps`, `grade`, `caption_style`, `music_mood`, `beat_sync`, `strictness` |
| GET | `/api/movieshorts/music-hint?mood=` |
| GET | `/api/movieshorts/status/{session}` |

All self-host only, gated like `/api/process/local`. Session JSON persisted
beside the session dir; the hook list is the reusable asset.

### 2.9 Dashboard: `MovieShortsTab.jsx`

Stepper modelled on `SaaShortsTab.jsx`: upload film + SRT (offset +
override) → settings (ratio, grade, caption style, hook count, length,
`beat_sync`, `source_license`) → copy hook prompt / paste JSON → hook cards
→ per hook: beat prompt / paste / validate → **shots preview** (86-shot
strip, crop scale editable, blur tool, beat grid drawn under, nudges
flagged) → render → preview + checklist (rhythm tier, phase lock, music
credit) → `/api/social/post`.

---

## 3. Movie Recap (`movierecap.py`)

### 3.1 Design goal

After all three parts, the viewer must want to watch the film **more**. The
climax is never shown, described or resolved. A recap that summarises the
plot substitutes for the film (factor four, market effect: the factor that
decides these cases). A recap that explains how the film works advertises
it, and is also the stronger video. No flip, no mirroring, no
detection-evasion step: those help no legal factor and are circumvention
under YouTube's ToS. Not legal advice; fair use is US law and India's fair
dealing (§52) lists criticism and review but not "summary".

Nine craft rules, encoded in the prompts: withhold resolutions, never
questions; discuss craft, not events; name that a twist exists, never its
content; promise what the format cannot deliver; use the second-best
material; prefer the moment before; hard wall at 75% of runtime; each part
raises the viewer's estimation; the kill test (a sentence that could
substitute for a scene is cut).

| Part | Subject | Ends on | Withholds |
|---|---|---|---|
| 1 | What kind of film this secretly is (first act) | open question about method | first-act consequences |
| 2 | The mechanism, evidenced across the middle | larger question pointing at act three | the midpoint's outcome |
| 3 | Why the ending's design only works experienced; what to watch for | a direct invitation to watch | the ending, entirely |

Part 3 is the hard one; the validator exists mostly to stop it becoming the
third act.

### 3.2 Three passes

- **A, spoiler map**: protected ranges tiered `ending > twist > reveal >
  midpoint`, `safe_premise`, `central_question` (unanswered), `twist_exists`,
  `twist_location`, `twist_effect`. Server unions with the final-25% wall
  and manual exclusions.
- **B, series structure**: `series_title`, three parts with `title` (a
  claim, not a chapter heading), `claim`, `evidence_band`, `open_question`,
  `withheld`, `cannot_deliver`. One screen; the user reviews here.
- **C, chunk plan, once per part**: 14–22 chunks of 6–15 s, ~450 words,
  ~25 per chunk, every chunk narrated, `shows`, `withholds`,
  `closing_lines`, `spoiler_self_check`. Band digest only.

**Recap mode (18-sep-2026).** The channel does not want the teaser: "I
don't care about spoiling the movie, I need an amazing story; if people like
the story they will watch the movie." So `recap_mode` `story` is the API
default: Pass A returns a **story map** (`StoryMap`: premise, protagonist,
want, obstacle, hook line, 6-10 turning points, climax, ending, mood arc),
Pass B three parts covering the film in order with a **cliffhanger** each
and part 3 reaching the end, Pass C tells it all in the storyteller voice
(`NARRATION_RULES_FULL`, `CLIP_RULES["story"]`) with part 3's ending rule.
No wall, no spoiler map; only the user's own exclusions bind. The
resolution-language lint is off in story mode, the lecture-word lint stays.
`teaser` keeps everything below as written. Library function defaults stay
`teaser` so the original tests keep their meaning.

**Narration style (18-sep-2026).** The first real scripts came out as a
lecture ("watch the lens", "the camera holds this kindness like an exhibit",
eleven "the film"s in one part): "discuss craft, not events" taken literally.
Pass B now returns a `mood` per part; Pass C has two registers,
`narration_style` `story` (default: the moment told from inside it, in the
part's mood, apparatus vocabulary banned, "notice"/"watch how" at most once
per part) and `essay` (the original voice). `validate_plan` warns
`craft_language` per chunk in story mode and once for the part when more
than a third of its chunks lecture. The spoiler rules did not move: no
payoff, no resolution, the open question at the end, felt rather than asked.

Full text in `movierecap.PROMPTS` (rev 3 wording). Per-chunk `flip`
survives as a manual editorial flag, default `false`; `flip: true` needs a
`flip_reason`, and >3 flips per part or a regular alternation fails: that
guard is deliberate.

### 3.3 Validator and lint: `movierecap.RecapPlan`

Timestamps: no chunk intersects a protected range (names it); none at or
after the wall; inside the part's band; ascending, non-overlapping; ≤15 s;
mean 6–13 s; footage `≈ target × 1.25` ±8%. Narration: no silent chunk;
words within budget +10%; resolution-language lint (`then he/she`, `turns
out`, `finally`, `ends with`, `we learn`, `it is revealed`, `in the end`,
`the twist is`, `dies`, `kills`, `escapes`, `wins`, `survives`) warns per
chunk, never auto-fails; `spoiler_self_check` non-empty and not "nothing".
Structure: three parts with `claim`/`open_question`/`withheld`; banned title
words (Setup, Beginning, Middle, End, Ending, Finale, Explained, Recap,
Summary, Full Story); `withheld` distinct across parts.

### 3.4 Budget panel

| Metric | Target |
|---|---|
| Total footage | < 10 min, < 10% of runtime |
| Longest / mean chunk | < 15 s / 6–13 s |
| Narration words ÷ footage seconds | > 2.5 |
| Runtime without narration | < 5% |
| Chunks in protected ranges / past the wall | 0 / 0 |
| Resolution-language hits | reviewed |
| `withheld` per part | present and distinct |

### 3.5 Render and voiceover

Silent parts: chunks → `run_cut_concat` → `speed` → reframe (`vertical`).

Voiceover, one file per part: **mute the bed by default** (score is
separately claimed and audio fingerprinting is the most reliable match;
narration alone also sounds better). `keep_audio: true` per chunk where the
dialogue is the evidence; those get the VO-keyed duck (`ratio=12
release=300`, `aresample=48000` on the upload, flat `volume=0.18`
fallback).

Re-timing: reuse `compilation.py`. `transcribe_vo` + `align_lines_to_words`
place each chunk's narration on the recorded words; `fit_shots` re-cuts
against the real timing. Within ±10% of target, re-cut; outside, refuse with
word count, measured and target duration. Re-cutting beats time-stretching.

### 3.6 Endpoints

| Method | Route |
|---|---|
| POST | `/api/movierecap/session` |
| GET | `/api/movierecap/prompt/{session}?pass=a\|b\|c&part=N` |
| POST | `/api/movierecap/spoilers/{session}` |
| POST | `/api/movierecap/structure/{session}` |
| POST | `/api/movierecap/plan/{session}/{part}` — validate + lint |
| GET | `/api/movierecap/budget/{session}` |
| POST | `/api/movierecap/render/{session}` |
| POST | `/api/movierecap/voiceover/{session}/{part}` |
| GET | `/api/movierecap/status/{session}` |

### 3.7 Dashboard: `MovieRecapTab.jsx`

Upload → excluded ranges on a scrubber (wall pre-filled) → Pass A prompt /
paste → Pass B prompt / paste / edit structure → per part: Pass C prompt /
paste → validator errors + lint hits + live budget → render three silent
parts → per part VO upload → mix → pre-publish checklist → `/api/social/post`.

---

## 3.8 Generated voiceover with Kokoro (planned 17-sep-2026, not built)

The recording step is the one part of the recap loop that still needs a
person and a second tool. The Pass C plan already holds the whole script
(`chunks[].narration`, `closing_lines`), so the narration is synthesised
from the session itself, per chunk, and fitted to the cut. Kokoro-82M is the
engine: open weights, 68 voices across 9 languages including Hindi, and it
runs on this machine's **CPU** fast enough that the GPU never sees it.

### Measured on the render box (17-sep-2026, `F:\kokoro\kokoro-env`, kokoro 0.9.4)

| | GPU (RTX 2070 SUPER) | CPU (Ryzen 7) |
|---|---|---|
| model load | 14.7 s | 10.7 s |
| VRAM | 318 MB after load, **1.36 GB reserved at peak** while synthesising | 0 |
| one 25-word chunk (9.8 s of speech) | 3.0 s | 3.5 s |
| a whole 150 s part (168 words) | 1.0 s | 15.9 s |
| pace at `speed=1.0` | 171–190 wpm | same |

The card idles at 3.9 GB of 8 GB used by the desktop before any job starts.
The pipeline's own GPU residents on a job are TransNetV2 (scene cuts), YOLO +
MediaPipe (reframe), NVENC sessions (2–3 max on Turing), whisper (in the
job process, or in the API for `/api/subtitle` and the Studio, released
after each call since upstream `60fa37a`), and Demucs when
`TRANSCRIBE_VOCALS=1`. Upstream's note on `release_models` records what
happens when something else sits on the card: NVENC cannot open a session
(exit 187) and TransNetV2 hits CUDA OOM. Kokoro on the GPU would add 1.4 GB
resident and a 15 s load to save **15 seconds per part**. It is not worth
one failed render. **Decision: Kokoro runs on the CPU, always.** No
`KOKORO_DEVICE=cuda` option is offered until a real need appears; the
measurement above is the argument against it.

### Licence, and why it is a separate process

Kokoro-82M weights, the `kokoro` package and remsky's Kokoro-FastAPI wrapper
are all **Apache 2.0**: commercial use is fine. The one GPL component is
**espeak-ng** (GPLv3), which misaki loads as the phonemiser fallback for
out-of-dictionary English words and as the primary G2P for several other
languages (`start-gpu.ps1` points `PHONEMIZER_ESPEAK_LIBRARY` at
`C:\Program Files\eSpeak NG\libespeak-ng.dll`). Same rule as `lameenc` in
`vocal_isolation`: it never enters this process. OpenShorts talks to a
Kokoro **server** over HTTP; nothing here imports `kokoro`, `misaki` or
`phonemizer`, and the repo's dependency set stays MIT/Apache.

### Process model

Kokoro-FastAPI, started by hand from `F:\kokoro\Kokoro-FastAPI` with a copy
of `start-cpu.ps1` (it pins `USE_GPU=false`, port 8880). Persistent server
rather than a subprocess per job because the model load is 11 s and the
voice **preview** button has to answer in a second; the cost is ~1.5 GB of
RAM resident, on a 32 GB box. What OpenShorts does with it:

- `KOKORO_URL` (default `http://127.0.0.1:8880`), `KOKORO_TIMEOUT` (120 s).
- Health: `GET /v1/audio/voices` at first use and on demand; cached 60 s.
  Offline is a state the dashboard shows with the exact command to run,
  never an error that fails a render: "upload voiceover" stays available.
- Concurrency: **one synthesis at a time** across the API
  (`threading.Semaphore(1)` in `film_voice`). Kokoro-FastAPI is fast on
  the CPU but two parts synthesising together would each take twice as
  long and nothing is gained; the queue also keeps the fit report's timings
  reproducible.
- CPU contention is the only resource question left. A synthesis is a
  burst of 16 s of all-core work. It is started **before** the render
  thread begins cutting (the cut phase runs 2–4 ffmpeg encodes in parallel
  and would slow both), so the sequence per part is strictly: synthesise →
  fit → cut → concat → speed → reframe (GPU) → mix → finalize. Kokoro is
  never running while ffmpeg or the reframe are.

Verified endpoints (Kokoro-FastAPI `api/src/routers`):

- `POST /v1/audio/speech` (OpenAI-compatible): `input`, `voice`, `speed`
  (0.25–4.0), `response_format=wav`, 24 kHz mono. Language follows the voice
  prefix (`a` US English, `b` UK, `h` Hindi, `e` Spanish, `f` French, `i`
  Italian, `p` Portuguese, `j` Japanese, `z` Chinese).
- `POST /dev/captioned_speech`: same plus word timestamps, if per-word
  caption sync is ever wanted for the recap.
- `GET /v1/audio/voices`; `POST /v1/audio/voices/combine` for weighted
  mixes (a "custom narrator" later, not now).

### `film_voice.py`

httpx only. Four functions and a fit algorithm:

- `health()` → `{"online", "url", "voices": n}`; `voices()` → grouped by
  language and gender from the prefix, cached.
- `synthesize(text, voice, speed, out_wav)` → path + measured duration
  (ffprobe or `wave`), under the semaphore. Retries once on a connection
  error, then reports offline.
- `narrate_plan(plan, part, protected, duration, voice, speed, workdir)` →
  the `movierecap.fitted_to_source` shape (`chunk.path`, `voice_start`,
  `shot_start`, `shot_length`, `keep_audio`) plus a per-chunk report.
  Deterministic, **no whisper**, so the GPU is not touched for alignment:
  1. `slot = (end - start) / speed_video` finished seconds; lead 0.15 s and
     tail 0.25 s (the padding `compilation.py` already uses) leave
     `available = slot - 0.40`.
  2. Line fits → placed at `shot_start + lead`. Action `fit`.
  3. Too long → grow the chunk with its head/tail room, bounded by the
     neighbours and by protected ranges (`movierecap.compilation_plan`
     computes exactly this in finished units; reuse it). Action `room`.
  4. Still too long → re-synthesise at `min(1.25, vo / available)`. Action
     `speed`.
  5. Still too long → `overrun`: the line is placed anyway, the chunk keeps
     its footage, and the report says by how much. The video is never
     time-stretched; the fix is a shorter sentence in the JSON.
  Pace check up front: at 171–190 wpm a 25-word chunk is 8–9 s against an
  8.3 s slot, so most lines land in step 2 or 3 at `tts_speed` 1.0–1.1.
  If the first real report shows the majority in step 4, the Pass C prompt
  drops to ~22 words per chunk; the speed knob does not go up.
- The same output feeds `film_render.render_narrated` unchanged, so the
  generated and the uploaded voiceover share one render path and one bug
  surface.

### API

| Method | Route |
|---|---|
| GET | `/api/film/voices` — Kokoro health + grouped voice list + the start command when offline |
| POST | `/api/movierecap/narrate/{sid}/{part}/preview` — `{voice, speed}`; synthesises the part's first line to `/film/<sid>/preview_<voice>.wav` |
| POST | `/api/movierecap/narrate/{sid}/{part}` — `{voice, speed}`; thread: synthesise every line (CPU) → fit → `render_narrated` (GPU) → `renders["recap-N"]` with `voice`, `tts_speed`, the fit report and the script that was read |

`voice` and `tts_speed` join the recap settings (defaults `am_michael`,
1.0), remembered per session. A render already running for that part
answers 409, as the upload route does. The narrated wavs stay in the
session dir (`vo_partN_<voice>/`), so re-rendering after a caption or
format change does not re-synthesise.

### Dashboard (`MovieRecapTab`, steps 5/6, per part)

Voice picker grouped by language, defaulting to the session language;
**preview** plays line 1 inline; speed 0.9–1.2; **generate voiceover**
button → progress → the narrated part appears where the uploaded one
would; the fit table (chunk, slot, voice seconds, action, overrun seconds)
under it, overruns in red with the offending sentence so the user can
shorten it and re-run only that part. "upload voiceover" stays as the
manual path. When Kokoro is offline the button is disabled and the panel
shows the start command.

### Server lifecycle (built 18-sep-2026)

`film_voice.ensure_server()`: if no server answers, `KOKORO_HOME/.venv`'s
python launches `uvicorn api.src.main:app` with the same env as
`start-cpu.ps1` (`USE_GPU=false`, `PHONEMIZER_ESPEAK_LIBRARY` when the DLL
exists, log at `output/film/kokoro.log`) and the call waits up to
`KOKORO_START_TIMEOUT` (90 s) for `/v1/audio/voices`. Lazy: the first
voices/narrate/preview call or the tab's **start Kokoro** button
(`POST /api/film/voices/start`) triggers it; `KOKORO_AUTOSTART=0` disables
it. `stop_server()` runs in the app's lifespan shutdown and only stops a
process this API started. Measured cold start to first answer: 17.4 s.

### Captions and logos on a part (built 18-sep-2026)

The clip editor's `SubtitleModal` and `OverlayEditor` open on a narrated
part unchanged except for one prop (`transcriptPath`). They post to
`/api/movierecap/part/{sid}/{part}/captions` (`preset` + `overrides`,
optional edited `words`, `preset: null` removes) and `.../overlays`
(`overlays.normalize` items, `[]` removes). `_relayer_part` strips back to
the narrated base (`renders[...].base_file`), composites overlays, then
burns captions on top through `app._burn_styled_captions`, and drops the
previous derivatives. Caption words are the narration lines placed on the
finished timeline (`narration_cues`: start = `voice_start`, end = start +
the wav's duration), spread per line the way SRT cues are; per-word
timestamps from `/dev/captioned_speech` are the upgrade if the pop looks
uneven.

### Silent render must actually be silent

Found in the first recap test: the silent parts carry the film's
soundtrack, because `film_render.render_short` keeps source audio through
the cuts and the recap job passed no bed. Fix: a `mute` flag on
`render_short` that drops the audio stream at the final pass (`-an`), used
by the recap's silent render; the narrated render rebuilds its audio from
scratch anyway.

### Tests (no Kokoro, no GPU in CI)

- `tests/test_film_voice.py`: fake HTTP server via `httpx.MockTransport`
  returning wavs of chosen lengths; fit algorithm hits each of the five
  actions; protected ranges bound the room; the semaphore serialises two
  concurrent calls; offline health is a state, not an exception.
- `tests/test_film_api.py`: narrate endpoint with `film_voice` faked,
  409 on a running render, preview URL shape.
- Acceptance on the box: part 1 of Machine Gun Preacher with `am_michael`,
  watch `nvidia-smi` during synthesis (expect no new process), read the fit
  report, listen.

### Order

1. `mute` flag; `film_voice.health/voices/synthesize`; `/api/film/voices`.
2. `narrate_plan` with tests against the mock; the narrate and preview
   endpoints; the fit report.
3. Dashboard picker, preview, generate button, fit table, offline state.
4. Real part 1; tune lead/tail, default speed and the words-per-chunk rule
   from the report. Then decide whether `/dev/captioned_speech` word
   timestamps are worth wiring for recap captions.

## 4. Publishing (both modules)

Upload **unlisted**, run YouTube Studio's copyright check, then decide.
`source_license` per session (`owned | affiliate_program | licensed |
unlicensed`) in the checklist. Every description: title, year, director, a
legitimate way to watch. Music credit line when `attribution_required`.

## 5. Upstream

`kamilstanuch/Autocrop-vertical` is 7 commits ahead touching only `main.py`,
`README.md`, `requirements.txt`; this fork is 400+ commits ahead. Do not
merge or cherry-pick. Port by hand, in order: `--plan-only` dry run, input
media summary logging, VFR normalisation, broader encoder probing. Skip
their batch-YOLO change (reverted upstream for accuracy).

---

## 6. Build order

| # | Scope | Done when |
|---|---|---|
| 1 | `film_prep.py`: SRT/VTT parse → transcript, digest, offset probe, `speed()` | `tests/test_film_prep.py` round-trips a fixture SRT; speed keeps A/V sync |
| 2 | Aspect + fps check: `square` on a 2.39:1 source through `recut.perform_recut`, `-r 60`, ASS scaling on 1:1 | one flag renders 1:1 and 9:16 at 60 fps |
| 3 | `movieshorts.py`: both prompts, `BeatPlan`, caption fuzzy-match | a plan with invented dialogue fails loudly |
| 4 | `film_prep.expand_beat()`, static mode in `punch_in`, `/shots` | 16 beats → ~86 shots with a sane ladder, inspectable JSON |
| 5 | Shorts render: per-shot crop/grade, concat, speed, reframe, `film_pop` captions | a silent short plays and looks like the reference; shot rhythm measured 1.3–1.7 s |
| 6 | `music_library.py`: scan, manifest, `pick`, recipes, mix profile in `music.py` | bed ducks under dialogue, never restarts on a cut |
| 6b | `beat_grid.py`, schedule, dialogue guard, ladder | `r > 0.80`, stable first vs last 30 s |
| 7 | `MovieShortsTab.jsx` | whole loop without curl |
| 8 | `movierecap.py`: passes A/B/C, `RecapPlan`, lint, budget | a plan touching a protected range or reciting plot fails loudly |
| 9 | Recap render + VO via `compilation.py`, bed policy, duck | a part with real VO sounds right |
| 10 | `MovieRecapTab.jsx` | whole loop without curl |
| 11 | Docs: CLAUDE.md sections, `docs/`, USAGE | a fresh session can find every rule above |

Sessions 1, 3, 8 need no GPU and no film (fixture SRT). Session 5 on a
10-minute test video before a feature. **Test the prompts by hand before
building either UI**: run the passes on one film you know, and ask whether
you would watch it afterwards.

Per `CLAUDE.md`: batch small commits before pushing, run lint and tests
locally, confirm CI green before calling a session done.
