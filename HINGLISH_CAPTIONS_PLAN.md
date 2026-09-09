# Hinglish caption accuracy — analysis and plan

Investigated 9 Sep 2026 against a real failing job (`output/ea6432ee-.../`, Comedy Nights With Kapil, `language: hinglish`, 704 segments) with the source still on disk, so every claim below is measured on that audio, not inferred.

## TL;DR

**WhisperX does not fix the problem you have.** WhisperX's contribution is *word-level timestamp accuracy* via wav2vec2 forced alignment. The Hinglish captions are failing on *what the words are*, not *when they are*. The measured wins, in order of value per hour of work:

1. `WHISPER_MODEL=large-v3` instead of `large-v3-turbo` — fixes most of the wrong words. **One env var.**
2. `initial_prompt` with names/domain vocabulary — fixes proper nouns (`अजीए` → `अजय`). **~15 lines.**
3. `translit.py` punctuation bug — every sentence-final word is currently romanised wrong (`sar` → `sara`, `kapil` → `kapila`, `achcha` → `achchaa`). **~5 lines.**
4. WhisperX-style forced alignment — ~~only worth doing *after* the above~~. Measured after 1-3 shipped and **dropped**: the disagreement is 138ms median on word starts, part of it the aligner's own segment-edge snapping, and the only usable ungated Hindi CTC model is worse than the thing it would correct (see Phase 2 below).

## 1. The three independent failure axes

Caption quality in this pipeline is three separate things that get conflated:

| Axis | Owned by | Current state | Does WhisperX help? |
|---|---|---|---|
| **What the words are** (ASR text / WER) | `faster-whisper` model choice, decode params | **This is the broken one** | **No** — WhisperX wraps the same whisper weights |
| **When the words are** (word timestamps) | faster-whisper DTW cross-attention | Looks fine on inspection | Yes — this is its whole point |
| **How Devanagari becomes Latin** | `translit.py` | Good, except a punctuation bug | No |

## 2. Evidence: axis 1 (ASR text) is the failure

The shipped caption for 25.3-26.4s of that episode was `alo ajee saar`. Ground truth: *"Hello Ajay sir"*. Re-transcribing the exact slice (24s +12s, `language=hi`, the repo's own `WHISPER_TRANSCRIBE_PARAMS`):

| | Devanagari | after `translit` |
|---|---|---|
| `large-v3-turbo` (current `.env`) | `हेलो अजयी सर, हेलो कापिल सार` | `helo ajyi sara, helo kaapil saar` |
| `large-v3` | `हेलो अजीए सर` / `हेलो कपिल सर` | `helo ajie sar` / `helo kapil sar` |
| `large-v3` + `initial_prompt` | `हेलो अजय सर, हेलो कपिल सर` | `helo ajay sara, helo kapil sar` |

Per-word, turbo → large-v3:

| Truth | turbo | large-v3 |
|---|---|---|
| कपिल (Kapil) | `कापिल` ✗ | `कपिल` ✓ |
| सर (sir) | `सार` ✗ | `सर` ✓ |
| अंगूठे पे | `अंगुथे पर` ✗ | `अंगुठे पे` ✓ |
| नेल पेंट (nail paint) | `नेलपेंट लगाय` ✗ | `नेल पेंट लगा` ✓ |
| अजय (Ajay) | `अजयी` ✗ | `अजीए` ✗ → `अजय` ✓ *with prompt* |

**`large-v3-turbo` is the wrong model for Hindi.** Turbo is a 4-decoder-layer distillation of large-v3; it degrades on low-resource languages and code-switching. Worse, adding an `initial_prompt` to *turbo* made it collapse into English translation and then produce nonsense — the exact failure `transcribe_backends.py`'s own docstring warns about:

```
large-v3-turbo + prompt:  "Hello Ajay sir. Hello Kapil sir."      <- switched to English
                          "अम्धुते भें नेलकों लगा है?"              <- nonsense
large-v3       + prompt:  "हेलो अजय सर, हेलो कपिल सर"              <- correct
```

So `initial_prompt` is only safe **together with** the large-v3 swap. Do not ship it on turbo.

### Cost of the swap (measured, RTX 2070 SUPER, float16, cached model)

| Model | Decode, 12s slice | Throughput |
|---|---|---|
| `large-v3` | 2.7s (2.1s with prompt) | ~4.4-5.7x realtime |
| `large-v3-turbo` | 4.3s | ~2.8x realtime |

The first `large-v3` run took 758s — that was the ~3GB model **download**, not decode. Once cached it is not slower than turbo on this GPU at these settings. Still worth a throughput check on a full hour-long source before making it the global default, since VRAM is 8GB and shared with YOLO/MediaPipe/TransNetV2.

## 3. Evidence: axis 3 (`translit.py`) has one real bug

`translit.to_roman` is otherwise good — `हैलो`→`hailo`, `अजय`→`ajay`, `सर`→`sar`, `ज़िंदगी`→`zindagi`, `बहुत`→`bahut` all correct. But **trailing punctuation breaks two rules at once**:

```
'सर'    -> 'sar'      |  'सर,'    -> 'sara,'     <- final-schwa deletion didn't fire
'कपिल'  -> 'kapil'    |  'कपिल,'  -> 'kapila,'
'वह'    -> 'vah'      |  'वह?'    -> 'vaha?'
'क्या'   -> 'kya'      |  'क्या,'   -> 'kyaa,'     <- aa-vs-a rule didn't fire either
'अच्छा'  -> 'achcha'   |  'अच्छा!'  -> 'achchaa!'
```

**Root cause:** `to_roman` (translit.py:198) splits on whitespace only, so `"सर,"` reaches `_romanize_word` with the comma attached. `_units()` makes the comma its own trailing unit, so `_delete_schwas`'s rule 1 tests `units[-1]` — the comma — instead of `र`, and never fires. The `aa`/`a` choice in `_romanize_word` keys off `idx == len(units) - 1`, the same wrong unit, so it breaks identically.

This hits **every sentence-final word**, which in caption text is a large fraction of all words. High visibility, ~5-line fix.

## 4. Axis 2 (timestamps): not currently the problem

Word timings from the failing job look sane — e.g. `25.32-25.88 ' alo'`, `25.88-26.10 ' ajee'`, `26.10-26.44 ' saar'`: monotonic, no overlaps, plausible 0.2-0.6s durations, no drift against segment bounds. faster-whisper's DTW timings are less precise than forced alignment, but they are not what makes these captions read wrong. Quantified in Phase 2 below: 138ms median disagreement on word starts, and DTW's one real structural flaw is that it reports no inter-word silence at all.

One real (unrelated) find while reading that output: the shipped word was `' alo'` while re-transcription gives `हेलो`/`helo` — the leading `ह` went missing from the **word array** specifically. That suggests a word-boundary/merge artifact in the DTW word split or `merge_continuation_words`, not a romanisation error. Worth a separate look if leading consonants keep vanishing; forced alignment (§5) would also make it moot.

## 5. If/when you do want WhisperX: do not `pip install whisperx`

WhisperX 3.8.7's pins vs this venv:

| | whisperx wants | we have |
|---|---|---|
| torch | `~=2.8.0` (i.e. <2.9) | **2.11.0+cu128** |
| torchvision | `~=0.23.0` | **0.26.0+cu128** |
| torchaudio | `~=2.8.0` | not installed |
| pyannote-audio | `>=4.0.0` | not installed (diarization — we don't need it) |
| numpy / ctranslate2 / faster-whisper | `>=2.1.0` / `>=4.5.0` / `>=1.2.0` | 2.4.6 / 4.8.2 / 1.2.1 ✓ |

Installing it **downgrades torch two minor versions**, against `requirements.txt`'s `torch==2.11.0` pin, risking `ultralytics==8.4.46` and `transnetv2-pytorch` (both load torch), plus a ~2.5GB re-download and a large `pyannote` dependency tree for a feature we never call. License is fine (BSD-2-Clause).

**What WhisperX actually does, that we'd want:** per-language wav2vec2 CTC forced alignment. Its Hindi entry is `DEFAULT_ALIGN_MODELS_HF["hi"] = "theainerd/Wav2Vec2-large-xlsr-hindi"` — **which reports 72.62% WER on Common Voice Hindi**. Alignment tolerates a weak acoustic model better than ASR does, but that is the weakest link in its Hindi path, and it is trivially overridable (`load_align_model(..., model_name=...)`). Better Hindi CTC candidates: `ai4bharat/indicwav2vec-hindi`, `Harveenchadha/vakyansh-wav2vec2-hindi-him-4200`.

Useful detail for Hinglish specifically: WhisperX maps characters missing from the alignment model's vocab to a **wildcard** token rather than dropping them, and interpolates any word left with `NaN` bounds. So Latin-script English words inside a Devanagari transcript still get timings — which is exactly the code-switching case. Its aligner is a custom trellis + backtrack (`get_trellis`/`backtrack`), not `torchaudio.functional.forced_align`.

**Recommended integration: port the alignment step, not the package.** ~120 lines using `transformers` (Wav2Vec2ForCTC) + `torchaudio` at versions matching our torch 2.11 (`torchaudio==2.11.0+cu128` exists). Zero dependency conflict, no pyannote, we choose the Hindi model. Slots in behind the existing contract as a post-ASR stage — `transcribe_backends.transcribe_media()` already returns a fixed transcript shape, and alignment only rewrites `words[].start/end`.

Order matters: align on **Devanagari, before** `translit`. `romanize_transcript` maps word-by-word and preserves timings, so alignment upstream survives romanisation.

## 6. Plan of execution

### Phases 0 and 1 — DONE (9 Sep 2026)

Measured on the same 12s slice, through `transcribe_media()`, with `.env` left
on turbo so the model swap had to fire:

| stage | output |
|---|---|
| originally shipped | `alo ajee saar` · `anguthe pe nelpent lagaaya hue` |
| after Phase 0 | `helo ajay sar, helo kapil sar` · `anguthe pe nel pent laga hue` |
| after Phase 1 | `hello ajay sir, hello kapil sir` · `anguthe pe nail paint laga hue` |

Phase 0 shipped: per-language model swap (`subtitles.TURBO_UNSAFE_LANGUAGES`,
`get_whisper_config(language)`), `TRANSCRIBE_PROMPT` with the turbo guard
(`whisper_supports_prompt`) through `/api/process` + `_RESUMABLE_ENV_KEYS` + a
dashboard field, the `translit.py` punctuation peel (`_split_affixes`), and the
torch-before-faster-whisper import fix.

Phase 1 shipped: `_LOANWORDS`, 226 entries / 213 English words, per token so
per-word caption timings survive, nukta-folded lookup (`फ़ोन` and `फोन` both
→ `phone`). Every key was audited by forcing the phonetic path and diffing
against the English word — no misspelled Devanagari. That audit caught three
entries colliding with common Hindi words; counted over the real episode
transcript, `चीज़` appeared 4 times meaning "thing" (never cheese) and `बस` 3
times (never the vehicle), so `चीज़`/`बस`/`हाय`/`पास` are excluded and `सर` is
kept.

Also fixed while verifying (pre-existing, high frequency): the `aa`/`a` rule
required more than one syllable, so `का`/`था`/`ना` came out `kaa`/`thaa`/`naa`.
Guards: a bare vowel that is the whole word stays `aa` (`आ`), a nasalised
ending keeps both letters (`हाँ` = `haan`), and a final independent vowel after
a syllable still shortens (`हुआ` = `hua`).

Tests: 736 passed, 5 pre-existing failures (3 read `main.py` as cp1252 on
Windows, 2 need `jwt`), verified identical with the changes stashed.

Item 7 (a Gemini pass over the transcript for names/loanwords) was NOT done —
the dictionary is deterministic and free, and `TRANSCRIBE_PROMPT` already
covers names. Revisit only if specific words keep slipping through.

### Phase 0 — original list, for reference (est. half a day)

1. **`WHISPER_MODEL=large-v3`** for Hindi/Hinglish. Options: flip `.env` globally, or make it language-conditional in `subtitles.get_whisper_config()` (turbo is fine for English and 1.5x faster — a per-language default is probably the right call rather than paying large-v3 everywhere).
2. **Fix the `translit.py` punctuation bug** (translit.py:170-205). In `_romanize_word`, peel leading/trailing non-Devanagari characters off the token, romanise the core, re-attach. Both the final-schwa and `aa`/`a` rules then see the true last unit. Add table-driven tests for `सर,` `कपिल,` `वह?` `क्या,` `अच्छा!`.
3. **`initial_prompt` support** — plumb a `transcribe_prompt` / `TRANSCRIBE_PROMPT` env through `WHISPER_TRANSCRIBE_PARAMS` (only applied when the model is not turbo; guard it, per §2). Seed it from what we already know per job: the video title, and for the dashboard a free-text "names/terms in this video" box. Measured effect: `अजीए` → `अजय`.
4. **Fix the torch-import-order fragility** (real bug found while testing): `cublas64_12.dll` ships only inside `venv/Lib/site-packages/torch/lib/`, and CTranslate2 finds it **only if `torch` was imported first** (torch registers that DLL dir on import). `transcribe_backends._get_whisper_model()` does `from faster_whisper import WhisperModel` with no torch import — it works in `main.py` purely because ultralytics imports torch first. Any process that transcribes without importing torch (e.g. a dubbed-subtitle path in `app.py`) silently trips `_whisper_force_cpu` and degrades for the rest of that process. Add a bare `import torch` before the faster-whisper import, with a comment saying why.
5. **Re-run the same 12s A/B as a regression check**, then a full re-run of the Kapil episode, and eyeball the `.ass` output.

### Phase 1 — Hinglish-specific text quality (est. 1 day)

6. **Loanword/name restoration in `translit.py`.** Rule-based romanisation phonetically mangles English loanwords: `नेल पेंट`→`nel pent` (want *nail paint*), `स्टाइल`→`staail` (*style*), `हेलो`→`helo` (*hello*). Add a Devanagari→English-spelling dictionary applied before the phonetic fallback. A few hundred entries covers most Hinglish media speech (technology, film, everyday loanwords). This is the single biggest remaining *legibility* win after Phase 0 and needs no models.
7. Consider whether Gemini should get a pass at the transcript for names/loanwords — it already sees the transcript for clip picking, and it is good at this. Cost: one extra text call per job. Optional; the dictionary is deterministic and free.

### Phase 2 — forced alignment: MEASURED, NOT BUILT (9 Sep 2026)

Before adding a module, a 1.2GB model and a second GPU tenant, the actual
disagreement between faster-whisper's DTW timings and wav2vec2 CTC forced
alignment was measured on the same Comedy Nights slice (12s, 20 words,
`Harveenchadha/vakyansh-wav2vec2-hindi-him-4200`, `torchaudio.functional.forced_align`).
Threshold set in advance: >150ms median would justify building it, <80ms would not.

| | median | p90 | max |
|---|---|---|---|
| word **start** disagreement | 138 ms | 390 ms | 399 ms |
| word **end** disagreement | 86 ms | 699 ms | 739 ms |

**Verdict: skip it.** Three reasons, in order of weight:

1. **The disagreement is not evidence of DTW error.** It goes in both
   directions and the largest values cluster on the *first word of each
   segment*, where CTC snaps the token to the edge of the aligned window
   (`1.26` for a 1.46 word with 0.20s of pad — exactly the pad boundary, three
   times out of three). Some of the measured "error" is the aligner's.
2. **The available Hindi aligner is the ceiling.** WhisperX's default for `hi`
   is `theainerd/Wav2Vec2-large-xlsr-hindi` at **72.62% WER**; vakyansh is
   better and still produced the segment-edge snapping above. The good one,
   `ai4bharat/indicwav2vec-hindi`, is a **gated HF repo** (401 without an
   approved access request) and therefore cannot be a default — item 10 below
   is wrong as written. An aligner whose own error is the size of the effect
   cannot measure the effect.
3. **The one structural finding has no cheap fix.** DTW emits **zero**
   inter-word gaps — every word ends exactly where the next begins — while
   forced alignment finds real silence in 11 of 17 adjacent pairs. That is
   real and direction-free: DTW pads each word's *start* backwards to fill the
   pause, so `highlight`/`one_word`/`word_reveal` can light a word up to ~0.4s
   before it is spoken. The obvious cheap fix (cap a word's span by syllable
   budget, anchored to the more-trustworthy end) was checked against the data
   and **rejected**: span length does not predict disagreement — `हेलो` at
   `9.18-10.08` is 0.90s long and forced alignment agrees to within 50ms, so a
   duration cap would damage correct timings to fix incorrect ones.

Worth revisiting only if (a) an ungated, low-WER Hindi/Indic CTC model becomes
available, or (b) the complaint becomes specifically "the word highlight fires
early", which is the one symptom this would fix. For English sources the
calculus is different — the English aligners are strong — but English is not
the failing case here.

Probe kept out of the repo (scratchpad `align_probe.py` / `align_probe2.py`);
v1 aligned the whole file at once and reported 1.5s outliers that were its own
framing, not DTW's. v2 aligns per segment, which is what WhisperX does.

### Phase 2 — original list, for reference (est. 1-2 days)

8. New module `align.py`: `align_transcript(transcript, media_path, language)`, wav2vec2 CTC + trellis/backtrack (port from WhisperX's `alignment.py`, BSD-2-Clause, keep attribution), rewriting only `words[].start/end`, interpolating unalignable words, and returning the transcript unchanged on any failure (same fail-open contract as the rest of the pipeline).
9. Deps: `pip install torchaudio==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128` + `transformers`. Do **not** add `whisperx`.
10. Hindi model configurable via `ALIGN_MODEL`, defaulting to `ai4bharat/indicwav2vec-hindi` rather than WhisperX's 72%-WER default. Gate the whole stage behind `ASR_ALIGN=1` so it can be turned off per deployment.
11. Run it inside the existing `_ASR_GATE` semaphore — it is a second GPU model, and VRAM here is 8GB shared with YOLO/MediaPipe/TransNetV2.

### Phase 3 — in progress (9 Sep 2026)

Test set built for the *caveat*, not the headline: four slices of the same
episode chosen from the shipped transcript for **code-switching**, since "a
Hindi-specialised model may handle code-switched English worse" is the only
thing that can sink this phase.

| slice | source time | what it tests |
|---|---|---|
| `cs1_no_problem` | 2205 +12s | an English phrase inside a Hindi sentence |
| `cs2_long_english` | 3344 +14s | a long, genuinely English stretch |
| `cs3_english_nouns` | 1834 +10s | English nouns (number, message) in Hindi |
| `cs4_midsentence` | 2078 +10s | a mid-sentence switch ("firstly", "century") |

**Finding 1 — `large-v3` forced to `hi` writes English in Devanagari.**
`नो प्रॉब्लम`, `हेयरस्टाइल`, `मेसेज`, `सेंचरी`, `फर्स्ट ली`. So on code-switched
speech the loanword table is not a nice-to-have, it is the only thing standing
between the audience and `heyrastaail`. This is a *win* for the current design
(the Devanagari is at least recoverable) and it moved work back into Phase 1 —
see the matra fold below.

**Finding 2 — the shipped `transcribe_prompt` is not free.** Three arms over
the four slices:

| arm | Hindi-dominant lines | the long English stretch |
|---|---|---|
| no prompt | most stable | broken Devanagari transliteration |
| English, names only | `नो प्रॉब्लम`→`नू प्रॉब्लम`, hallucinated tail | **correct Latin English** |
| Devanagari, names only | same drift | **prompt echoed verbatim, content gone** |

`initial_prompt` is prior decoder context, so a prompt written in the *output*
script reads as text whisper just emitted: the Devanagari arm returned
`अजय देवगन, करीना कपूर, सिंघम रिटर्न्स` twice and dropped the speech. The
dashboard hint used to say "write them the way they are spoken in the video",
which invites exactly that; it now asks for a few proper nouns in Latin
letters. Documented in CLAUDE.md.

**Phase 1 refinement this produced — the matra fold.** `मैसेज` was in the
loanword table and `मेसेज` was not, so the same word captioned `message` on one
line and `mesej` on another. `translit.py` now folds `े`/`ै` and `ो`/`ॉ` for the
lookup the same way it already folded nukta marks (226 → 354 entries), with a
`_NO_VARIANT` blocklist because a variant spelling can be a different word
(`रोड` road / `रॉड` rod, `रॉल` roll, `फेन` foam, `टोप` cap). `ॊ` (U+094A) is not
folded at all — it is a Dravidian matra Hindi never uses, and it was a third of
the candidates. Verified no variant is claimed by two different English words.
Three loanwords the measurement exposed outright were added: `hairstyle`,
`sexy`, `century`. 3 of the 4 slices improved; 739 tests pass.

**Still pending:** the fine-tune itself. `vasista22/whisper-hindi-large-v2`
converted to CTranslate2 float16 and run over the same four slices, against
these `large-v3` outputs. What would sink it: Devanagari-only output on
`cs2_long_english`, or losing the English nouns in `cs3`.

Remaining errors that no arm fixed, all axis 1: `डेबिव` for "debut", `महरी` for
"maari", and `फर्स्ट ली` — whisper split "firstly" into two timed tokens, so no
per-token lookup can rejoin it.

### Phase 3 — original list, for reference (est. 2-3 days)

12. A **Hindi fine-tuned whisper** beats generic large-v3 on Hindi by a wide margin (`vasista22/whisper-hindi-large-v2`, AI4Bharat IndicWhisper). Needs CTranslate2 conversion (`ct2-transformers-converter`) to run under faster-whisper, then it drops into the existing `WHISPER_MODEL` slot with no code change. Caveat worth testing before committing: a Hindi-specialised model may handle *code-switched English* worse than large-v3, which is precisely what Hinglish content is full of — measure on real clips, both axes, before adopting.

### Explicitly rejected

- `pip install whisperx` into this venv — torch downgrade 2.11→2.8 against an explicit pin, breaks/risks ultralytics + transnetv2, drags in pyannote for an unused feature (§5).
- WhisperX in an isolated second venv called over subprocess — clean, but a second ~3GB torch install plus per-job process/model startup, for a benefit (timestamps) that is not the current complaint. Reconsider only if Phase 2's in-repo port proves harder than expected.

## 7. Suggested first step

Phase 0 items 1-4 are independent, small, and individually verifiable, and together they address everything actually measured as broken. Item 2 (translit punctuation) and item 4 (torch import order) are outright bugs and worth fixing regardless of the Hinglish work.
