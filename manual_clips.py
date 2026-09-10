"""Pick clips in a chat app (Claude.ai / ChatGPT subscription), render them with OpenShorts.

No API key needed for the picking step:

  1. python main.py -i video.mp4 -o output/myvid --audience in --transcribe-only
       -> output/myvid/paste_into_chat.txt  (India or USA prompt + transcript)
  2. Upload that file to Claude.ai or ChatGPT, save the JSON reply as output/myvid/clips.json
  3. python main.py -i video.mp4 -o output/myvid --audience in --clips output/myvid/clips.json
       -> cuts, reframes, captions (and hooks with AUTO_HOOK=1) exactly those moments

Step 3 reuses the transcript from step 1 automatically (same output dir).
Prompts live in audience_profiles.py.
"""
import json
import os
import re

from clip_selection import clip_duration_bounds, snap_clip_to_words


def write_ai_transcript(output_dir, transcript, duration, title="", audience=None):
    """Write transcript.json, transcript_for_ai.txt and paste_into_chat.txt (prompt + transcript)."""
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "transcript.json"), "w", encoding="utf-8") as f:
        json.dump(transcript, f, ensure_ascii=False)

    lines = [
        f"VIDEO: {title}",
        f"DURATION_SECONDS: {float(duration):.1f}",
        f"LANGUAGE: {transcript.get('language', 'unknown')}",
        "TRANSCRIPT (each line = [start-end in seconds] spoken text):",
    ]
    for seg in transcript.get("segments", []):
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"[{float(seg['start']):.1f}-{float(seg['end']):.1f}] {text}")

    body = "\n".join(lines) + "\n"
    with open(os.path.join(output_dir, "transcript_for_ai.txt"), "w", encoding="utf-8") as f:
        f.write(body)

    import audience_profiles
    path = os.path.join(output_dir, "paste_into_chat.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(audience_profiles.chat_prompt(audience) + "\n\n=== TRANSCRIPT ===\n" + body)
    label = audience_profiles.PROFILES.get(audience, {}).get("label", "generic (no audience matched)")
    print(f"🎯 Prompt profile: {label}")
    return path


def _to_seconds(value):
    """Accept 83.5, "83.5", "1:23.5" or "0:01:23.5"."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if ":" in text:
        total = 0.0
        for part in text.split(":"):
            total = total * 60 + float(part)
        return total
    return float(text)


def _extract_json(raw):
    """Chat apps often wrap JSON in ``` fences or add a sentence around it."""
    raw = re.sub(r"```(?:json)?", "", raw)
    starts = [i for i in (raw.find("{"), raw.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON object or array found in the clips file")
    start = min(starts)
    end = max(raw.rfind("}"), raw.rfind("]"))
    return json.loads(raw[start:end + 1])


def load_manual_clips(path, transcript, duration):
    """Read clips picked in a chat app into the {"shorts": [...]} shape main.py renders."""
    with open(path, "r", encoding="utf-8") as f:
        data = _extract_json(f.read())
    items = data.get("shorts", data.get("clips", [])) if isinstance(data, dict) else data

    words = []
    for seg in (transcript or {}).get("segments", []):
        for w in seg.get("words", []) or []:
            words.append({"w": w["word"], "s": w["start"], "e": w["end"]})
    min_secs, max_secs = clip_duration_bounds()

    shorts = []
    for n, item in enumerate(items, 1):
        try:
            start = max(0.0, _to_seconds(item["start"]))
            end = min(float(duration), _to_seconds(item["end"]))
        except (KeyError, TypeError, ValueError) as e:
            print(f"   ⚠️ Skipping clip {n}: bad start/end ({e})")
            continue
        if end <= start:
            print(f"   ⚠️ Skipping clip {n}: end is not after start")
            continue
        if words:
            start, end = snap_clip_to_words(start, end, words, duration,
                                            min_duration=min_secs, max_duration=max_secs)

        title = item.get("video_title_for_youtube_short") or item.get("title") or ""
        hook = item.get("viral_hook_text") or item.get("hook") or title
        desc = item.get("description") or title
        shorts.append({
            "start": round(float(start), 3),
            "end": round(float(end), 3),
            "predicted_score": item.get("predicted_score", item.get("score", 0)),
            "video_title_for_youtube_short": title[:100],
            "viral_hook_text": hook,
            "video_description_for_tiktok": item.get("video_description_for_tiktok") or desc,
            "video_description_for_instagram": item.get("video_description_for_instagram") or desc,
            "video_tags": item.get("video_tags") or item.get("tags") or "",
            "reason": item.get("reason", ""),
        })

    if not shorts:
        raise ValueError(f"no usable clips in {path}")
    print(f"📋 Loaded {len(shorts)} hand-picked clip(s) from {path}")
    return {"shorts": shorts}
