"""
Turns a full-length source video into one narrated vertical short: cut list +
narration script come from the "Short Creation" Gemini-chat workflow (see the
project's Task 5 edit-plan JSON), the voiceover from ElevenLabs, and this
module does the split, shot-fitting, crop/concat and mux entirely with
ffmpeg. Captioning reuses the repo's own subtitle pipeline
(subtitles.generate_srt_from_video + burn_subtitles) — no Premiere, no
separate NLE, and no re-transcription of anything but the finished mix.

Where the VO gets split: NOT the [long pause] silence markers the workflow's
Gemini prompt asks for. Eleven v3's pause-tag timing isn't guaranteed (the
workflow's own docs say so), and in practice generations regularly render
fewer real silences than lines, which breaks a silencedetect-based split
outright. Instead this transcribes the VO with faster-whisper (already
GPU-wired in this repo) and walks its word-level timestamps, consuming each
line's already-known `actual_words` count in script order — ground truth
from the audio itself instead of a fragile audio cue.

Expected plan JSON shape: the Task 5 block from the workflow's Claude Code
paste — "sequence", "fit", "padding", "shots" (each with src_in/src_out,
head_room/tail_room, lead_in/tail) and "vo" (one "lines" entry per shot,
each with actual_words/first_words/silent; "edge_guard" is reused here as
the padding kept around each aligned word span).
"""
import argparse
import json
import os
import re
import subprocess
import tempfile

from ffmpeg_utils import LOUDNORM_FILTER, video_encode_args


class CompilationError(Exception):
    pass


def _run(cmd):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise CompilationError(f"Command failed: {' '.join(cmd)}\n{result.stderr[-4000:]}")
    return result


def _probe_duration(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ], text=True)
    return float(out.strip())


def _probe_height(path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=height", "-of", "default=noprint_wrappers=1:nokey=1", path,
    ], text=True)
    return int(out.strip())


def transcribe_vo(vo_path):
    """Whisper the VO and flatten it to word-level timestamps, sorted by start."""
    import subtitles
    from recut import transcript_words

    transcript = subtitles.transcribe_audio(vo_path)
    return transcript_words(transcript)  # [{'w', 's', 'e'}, ...]


def _normalize_word(word):
    return re.sub(r"[^\w']", "", word).lower()


def align_lines_to_words(plan, words, resync_window=6):
    """
    Consumes each narrated line's already-known `actual_words` count from the
    whisper word stream, in script order — the word count came from Gemini's
    own script, so this is just replaying it against ground-truth timestamps
    instead of trusting where ElevenLabs happened to render a pause.

    Resyncs against each line's `first_words` before consuming: whisper's
    tokenization doesn't always match Gemini's word count exactly
    (contractions, numerals, hyphenation), and an uncorrected one-word drift
    on an early line would misalign every line after it. Searches a small
    forward window for the expected first word rather than assuming the
    running pointer is already correct.
    """
    lines = [line for line in plan["vo"]["lines"] if not line.get("silent")]
    pointer = 0
    aligned = []

    for idx, line in enumerate(lines):
        n = line.get("actual_words", 0)
        if n <= 0:
            raise CompilationError(
                f"Line {line.get('n')} is marked narrated but has no actual_words."
            )

        first_word = (line.get("first_words") or "").split(" ")
        expected_first = _normalize_word(first_word[0]) if first_word and first_word[0] else ""
        if expected_first:
            window = words[pointer:pointer + resync_window]
            match_offset = next(
                (i for i, w in enumerate(window) if _normalize_word(w["w"]) == expected_first),
                None,
            )
            if match_offset:
                pointer += match_offset

        available = len(words) - pointer
        if n > available:
            # A shortfall on an interior line would misalign every line after
            # it — that's a real script/audio mismatch, fail loud rather than
            # guess. The LAST line has nothing downstream to protect, so
            # ElevenLabs simply speaking a couple fewer words than Gemini's
            # script assumed (common on the final line) is safe to absorb:
            # take whatever's left instead of failing the whole compilation.
            if idx == len(lines) - 1 and available > 0:
                print(f"⚠️ Line {line.get('n')} needs {n} words but only {available} remain — "
                      f"using all {available} (last narrated line, nothing after it to misalign).")
                n = available
            else:
                raise CompilationError(
                    f"Line {line.get('n')} ({line.get('first_words', '')!r}) needs {n} words "
                    f"but only {available} remain in the VO transcript. The ElevenLabs "
                    "audio may be shorter than the script, or missing a line — check it against the script."
                )

        chunk_words = words[pointer:pointer + n]
        aligned.append({
            "shot_ref": line["shot_ref"],
            "start": chunk_words[0]["s"],
            "end": chunk_words[-1]["e"],
        })
        pointer += n

    return aligned


def pad_and_split(vo_path, aligned, pad, workdir):
    """
    Cuts one wav chunk per aligned line, padding a little room around the
    word span on each side (ASR word timestamps run tight against the audio)
    without biting into the neighboring line's chunk.
    """
    total = _probe_duration(vo_path)
    chunks = []
    for i, a in enumerate(aligned):
        prev_end = aligned[i - 1]["end"] if i > 0 else 0.0
        next_start = aligned[i + 1]["start"] if i + 1 < len(aligned) else total
        start = max(prev_end, a["start"] - pad)
        end = min(next_start, a["end"] + pad)

        out_path = os.path.join(workdir, f"vo_chunk_{i + 1:02d}.wav")
        _run(["ffmpeg", "-y", "-i", vo_path, "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
              "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", out_path])
        chunks.append({
            "path": out_path, "start": start, "end": end, "duration": end - start,
            "shot_ref": a["shot_ref"],
        })
    return chunks


def fit_shots(plan, chunks):
    """
    shot_length = lead_in + chunk_duration + tail for a narrated shot, or
    planned_duration for a silent one. Extends src_out using tail_room
    first, then src_in using head_room; raises if neither covers the gap —
    matches the workflow's rule against speed-ramping, freezing or looping
    to fill a shot that's simply too short for its line.
    """
    lines_by_shot = {line["shot_ref"]: line for line in plan["vo"]["lines"]}
    chunks_by_shot = {c["shot_ref"]: c for c in chunks}
    default_lead_in = plan["padding"]["default_lead_in"]
    default_tail = plan["padding"]["default_tail"]

    fitted = []
    timeline_pos = 0.0

    for shot in plan["shots"]:
        line = lines_by_shot.get(shot["n"])
        silent = line is None or line.get("silent")
        lead_in = shot.get("lead_in", default_lead_in)
        tail = shot.get("tail", default_tail)

        if silent:
            shot_length = shot["planned_duration"]
            voice_start = None
            chunk = None
        else:
            chunk = chunks_by_shot.get(shot["n"])
            if chunk is None:
                raise CompilationError(f"No aligned VO chunk found for shot {shot['n']}.")
            shot_length = lead_in + chunk["duration"] + tail
            voice_start = timeline_pos + lead_in

        src_in, src_out = shot["src_in"], shot["src_out"]
        native = src_out - src_in
        diff = shot_length - native
        if diff > 1e-6:
            tail_room = shot.get("tail_room", 0.0)
            head_room = shot.get("head_room", 0.0)
            if tail_room >= diff:
                src_out += diff
            elif tail_room + head_room >= diff:
                remaining = diff - tail_room
                src_out += tail_room
                src_in = max(0.0, src_in - remaining)
            else:
                raise CompilationError(
                    f"Shot {shot['n']} ({shot.get('description', '')!r}) needs "
                    f"{shot_length:.2f}s but only has "
                    f"{native + tail_room + head_room:.2f}s of head/tail room available. "
                    "Trim the narration for this line and regenerate the VO."
                )
        elif diff < -1e-6:
            src_out += diff

        fitted.append({
            **shot,
            "src_in": src_in, "src_out": src_out,
            "shot_start": timeline_pos, "shot_length": shot_length,
            "voice_start": voice_start, "chunk": chunk, "silent": silent,
        })
        timeline_pos += shot_length

    return fitted, timeline_pos


def render(plan, fitted_shots, total_duration, video_path, output_path):
    """
    One ffmpeg pass: crops every fitted shot to the sequence's WxH, concats
    them (video + native audio), builds a second audio bed from the VO
    chunks delayed to their voice_start, and mixes the two — original
    ambience under the narration, never muted, matching the workflow's
    "don't duck unless asked" default.
    """
    seq = plan["sequence"]
    width, height = seq["width"], seq["height"]
    source_height = _probe_height(video_path)
    scale_percent = (height / source_height) * 100

    inputs = ["-i", video_path]
    narrated = [s for s in fitted_shots if not s["silent"]]
    for shot in narrated:
        inputs += ["-i", shot["chunk"]["path"]]

    filters = []
    v_labels, a_labels = [], []
    for i, shot in enumerate(fitted_shots):
        filters.append(
            f"[0:v]trim=start={shot['src_in']:.3f}:end={shot['src_out']:.3f},"
            f"setpts=PTS-STARTPTS,scale=-2:{height},crop={width}:{height}[v{i}]"
        )
        filters.append(
            f"[0:a]atrim=start={shot['src_in']:.3f}:end={shot['src_out']:.3f},"
            f"asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo[a{i}]"
        )
        v_labels.append(f"[v{i}]")
        a_labels.append(f"[a{i}]")

    concat_inputs = "".join(f"{v}{a}" for v, a in zip(v_labels, a_labels))
    filters.append(f"{concat_inputs}concat=n={len(fitted_shots)}:v=1:a=1[vout][a_orig]")

    vo_labels = []
    for i, shot in enumerate(narrated):
        delay_ms = int(round(shot["voice_start"] * 1000))
        src_index = i + 1
        filters.append(
            f"[{src_index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"adelay={delay_ms}:all=1[vo{i}]"
        )
        vo_labels.append(f"[vo{i}]")

    if vo_labels:
        filters.append(
            f"{''.join(vo_labels)}amix=inputs={len(vo_labels)}:duration=longest:normalize=0[vo_mix]"
        )
        filters.append("[a_orig][vo_mix]amix=inputs=2:duration=first:normalize=0[amixed]")
    else:
        filters.append("[a_orig]anull[amixed]")

    # loudnorm has to live inside the complex graph, not as a trailing -af:
    # ffmpeg refuses to mix simple and complex filtering on the same output
    # stream, and [aout] already came out of filter_complex.
    if os.environ.get("AUDIO_NORMALIZE", "1").strip() != "0":
        filters.append(f"[amixed]{LOUDNORM_FILTER}[aout]")
    else:
        filters.append("[amixed]anull[aout]")

    filter_complex = ";".join(filters)

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        *video_encode_args(), "-c:a", "aac",
        output_path,
    ]
    _run(cmd)
    print(f"Rendered {output_path} — {total_duration:.2f}s, scale {scale_percent:.1f}% applied via height-fit crop")
    return output_path


def build_compilation(plan_path, video_path, vo_path, output_path, workdir=None, burn_captions=True):
    with open(plan_path) as f:
        plan = json.load(f)

    if any("timeline_in" in s or "timeline_out" in s for s in plan["shots"]):
        raise CompilationError(
            "Plan JSON is the old format (timeline_in/timeline_out). Regenerate Task 5 — "
            "positions must come from measured audio, not planning estimates."
        )

    workdir = workdir or tempfile.mkdtemp(prefix="compilation_")
    os.makedirs(workdir, exist_ok=True)

    words = transcribe_vo(vo_path)
    if not words:
        raise CompilationError("Whisper found no words in the VO audio — check the file isn't silent or corrupt.")
    print(f"Transcribed {len(words)} words from the VO")

    aligned = align_lines_to_words(plan, words)
    pad = plan["vo"].get("edge_guard", 0.1)
    chunks = pad_and_split(vo_path, aligned, pad, workdir)

    fitted_shots, total_duration = fit_shots(plan, chunks)

    raw_output = os.path.join(workdir, "compilation_raw.mp4")
    render(plan, fitted_shots, total_duration, video_path, raw_output)

    if not burn_captions:
        os.replace(raw_output, output_path)
        return output_path

    import subtitles
    srt_path = os.path.join(workdir, "compilation.srt")
    subtitles.generate_srt_from_video(raw_output, srt_path)
    subtitles.burn_subtitles(raw_output, srt_path, output_path)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Build a narrated compilation short from a Task 5 edit plan.")
    parser.add_argument("plan", help="Path to the Task 5 edit-plan JSON")
    parser.add_argument("video", help="Path to the source video")
    parser.add_argument("vo", help="Path to the ElevenLabs VO.mp3")
    parser.add_argument("--out", default="compilation_output.mp4")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--no-captions", action="store_true")
    args = parser.parse_args()

    output = build_compilation(
        args.plan, args.video, args.vo, args.out,
        workdir=args.workdir, burn_captions=not args.no_captions,
    )
    print(f"Done: {output}")


if __name__ == "__main__":
    main()
