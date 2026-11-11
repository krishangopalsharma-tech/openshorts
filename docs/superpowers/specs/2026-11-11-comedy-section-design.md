# Dedicated Comedy Section & Twitter/X Comedy Layout — Design Spec

**Date:** 2026-11-11
**Target Repo:** `F:\openshorts` (`mine/main`)

## 1. Goal & Scope
Add a dedicated **Comedy** section in OpenShorts for extracting standalone comedy shorts from long-form movies and web series episodes, rendered in a Twitter/X post-style frame (`1080x1920` white canvas, circular profile avatar, bold channel name + blue verification checkmark + `@handle`, POV/meme caption text with color emojis directly below the header, and a slightly rounded `4:5` square-ish video frame below).

## 2. Architecture & Modules
1. **`comedy_card.py` (Twitter/X Comedy Frame Compositor):**
   - Renders the `1080x1920` RGBA card overlay with:
     - Solid white (`#FFFFFF`) background outside the rounded video window (`992x1220` at `x=44`, corner radius `18px`).
     - Top-left circular avatar (`112x112 px`), bold channel name (`#0F1419`), blue verified checkmark badge (`#1D9BF0` starburst/circle with white checkmark), and `@handle` (`#536471`).
     - Left-aligned POV/comedy caption text (`#0F1419`) with inline color emojis (`Segoe UI Emoji` / `NotoColorEmoji`) directly below the profile header.
   - Composites the face-tracked / split-screen reframed video into the rounded `992x1220` window in a single FFmpeg pass (`gpu_decode` + `video_encode_args`).
2. **`comedy_studio.py` (Comedy Section Backend & FastAPI Router):**
   - **Transcript & Whisper Sync (`/api/comedy/prepare`):**
     - Accepts a local `video_path` and optional `transcript_text` / `subtitle_path` (`.srt`, `.vtt`, `.txt`, `.json`).
     - When a subtitle/transcript is provided, runs Whisper audio verification (`film_prep.probe_offset`) to detect audio-to-text drift and shifts all transcript timestamps to align with the audio track.
     - When no subtitle/transcript is provided, runs Whisper transcription (`transcribe_media`) to produce word-aligned segments.
   - **Mandatory Metadata Gate (`/api/comedy/build-prompt`):**
     - Validates `content_type` (`"series"` or `"movie"`).
     - For `"movie"`: requires non-empty `title`.
     - For `"series"`: requires non-empty `title`, `season`, and `episode`.
     - **Hard Gate:** Raises `HTTP 400` if any required metadata field is missing; never builds the prompt without metadata.
   - **Internet-Database + Transcript Comedy Prompt Builder:**
     - Builds `paste_into_chat.txt` instructing ChatGPT / Gemini / Claude to combine internet database knowledge (IMDb, fan wikis, episode guides, iconic comedic scenes/quotes, and character dynamics for that exact Movie or Series + Season + Episode) with the aligned transcript timestamps to pick standalone comedy clips (`start`, `end`, `title`, `pov_text`, `why_funny`).
   - **Clip Parser & Render Launcher (`/api/comedy/parse-clips`, `/api/comedy/render`, `/api/comedy/avatar`):**
     - Parses the pasted JSON reply, preserves `pov_text` per clip, and launches a standard OpenShorts job with `COMEDY_CARD=1` and the user's channel profile header config so clips render through `main.py` (`reframe_v2.py` → `comedy_card.py`) and can be opened in the Clip Editor.
3. **`dashboard/src/components/ComedyTab.jsx` & `dashboard/src/App.jsx`:**
   - Dedicated **Comedy** navigation tab (`id: 'comedy'`).
   - Step 1: Video picker + optional `.srt`/`.vtt`/transcript upload/paste + Whisper audio verification/alignment status & manual offset override.
   - Step 2: Mandatory Metadata Gate (Web Series: Series Title + Season + Episode; Movie: Movie Title) — disables prompt generation until filled.
   - Step 3: Channel Header configurator (Avatar upload, Channel Name, `@handle`, Verified Checkmark toggle, Burn Subtitles toggle — off by default) + live Twitter/X card preview + Copy/Download Comedy Prompt (`paste_into_chat.txt`).
   - Step 4: Paste AI JSON reply, inline-edit each clip's `pov_text` caption, select vertical tracking layout (`auto`, `split`, `speaker_cut`, `none`), and click **Render Comedy Clips**.
