"""Beat detection and the beat-aligned shot schedule for Movie Shorts.

numpy only, on purpose: the CI environment has no scipy or librosa, and the
render box's GPU is spoken for. Three stages, cached in the music manifest at
scan time and never run on the render path:

- spectral-flux onset envelope (``hop=256`` at 22050 Hz = 86.13 frames/s)
- global tempo by autocorrelation with a log-Gaussian prior at 120 BPM, so a
  half- or double-tempo harmonic does not win
- beat times by Ellis' dynamic programming (tracks tempo drift; a fixed grid
  cannot), ``beat_confidence`` in standard deviations (onset strength at the
  beats minus at random positions: the statistic that showed the reference
  clips were NOT beat-cut, reused as a gate), and a 4/4 downbeat phase.

The coordinate rule for the schedule: beats live in FINISHED time and cuts
are decided in SOURCE time before the 1.25x speed step. ``schedule()`` builds
every shot as a whole number of beats in finished seconds and converts each
to source by ``x speed`` - never rounding in source time - so accumulated
error cannot drift the way it does when the correction is applied backwards.
"""

import math

import numpy as np

SR = 22050
N_FFT = 1024
HOP = 256
FPS = SR / HOP

TEMPO_RANGE = (60.0, 200.0)
TEMPO_PRIOR_BPM = 120.0
TEMPO_PRIOR_WIDTH = 0.9  # octaves
TIGHTNESS = 100.0

SHOT_FINISHED_RANGE = (1.2, 1.8)
BEAT_BPM_RANGE = (90.0, 150.0)
BEAT_CONFIDENCE_MIN = 1.5
K_CYCLE = (3, 3, 2, 4, 3, 2)
DIALOGUE_NUDGE_MAX = 0.12  # finished seconds
NUDGE_WARN_SHARE = 0.15


# --- onset envelope ---------------------------------------------------------

def onset_envelope(y, sr=SR, n_fft=N_FFT, hop=HOP):
    """Half-wave-rectified spectral flux, z-scored. Returns ``(flux, fps)``."""
    y = np.asarray(y, dtype=np.float32)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if len(y) < n_fft:
        y = np.pad(y, (0, n_fft - len(y)))
    n_frames = 1 + (len(y) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    window = np.hanning(n_fft).astype(np.float32)
    frames = y[idx] * window
    mag = np.abs(np.fft.rfft(frames, axis=1))
    # Log compression: a loud bar and a quiet bar contribute the same shape.
    mag = np.log1p(1000.0 * mag)
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    std = flux.std()
    if std > 0:
        flux = (flux - flux.mean()) / std
    else:
        flux = np.zeros_like(flux)
    return flux.astype(np.float64), sr / hop


# --- tempo ------------------------------------------------------------------

def estimate_tempo(flux, fps=FPS, lo=TEMPO_RANGE[0], hi=TEMPO_RANGE[1],
                   prior_bpm=TEMPO_PRIOR_BPM, prior_width=TEMPO_PRIOR_WIDTH):
    """Autocorrelation peak inside ``[lo, hi]`` BPM, weighted by a log-Gaussian
    prior. Returns ``(bpm, period_seconds, strength)``; strength is the
    normalised autocorrelation at the pick (0 = no periodicity)."""
    x = np.asarray(flux, dtype=np.float64)
    if len(x) < 4 or not np.any(x):
        return 0.0, 0.0, 0.0
    ac = np.correlate(x, x, mode="full")[len(x) - 1:]
    if ac[0] <= 0:
        return 0.0, 0.0, 0.0
    ac = ac / ac[0]
    lags = np.arange(len(ac))
    with np.errstate(divide="ignore"):
        bpm = 60.0 * fps / np.maximum(lags, 1)
    prior = np.exp(-0.5 * (np.log2(bpm / prior_bpm) / prior_width) ** 2)
    band = (bpm >= lo) & (bpm <= hi) & (lags > 0)
    if not band.any():
        return 0.0, 0.0, 0.0
    score = np.where(band, ac * prior, -np.inf)
    lag = int(np.argmax(score))
    return float(60.0 * fps / lag), lag / fps, float(max(0.0, ac[lag]))


# --- beat tracking (Ellis 2007) --------------------------------------------

def track_beats(flux, fps, period_seconds, tightness=TIGHTNESS):
    """Beat positions (frame indices) by dynamic programming: each beat wants
    to sit on an onset AND about one period after the previous beat."""
    x = np.asarray(flux, dtype=np.float64)
    n = len(x)
    period = period_seconds * fps
    if n == 0 or period < 2:
        return np.array([], dtype=int)
    local = np.maximum(x, 0.0)
    lo_lag = max(1, int(round(period / 2.0)))
    hi_lag = int(round(period * 2.0))
    lags = np.arange(lo_lag, hi_lag + 1)
    txwt = -tightness * (np.log(lags / period)) ** 2
    cumscore = np.zeros(n)
    backlink = -np.ones(n, dtype=int)
    for i in range(n):
        cands = i - lags
        valid = cands >= 0
        if not valid.any():
            cumscore[i] = local[i]
            continue
        scores = txwt[valid] + cumscore[cands[valid]]
        j = int(np.argmax(scores))
        cumscore[i] = local[i] + scores[j]
        backlink[i] = cands[valid][j]
    # Best terminal among the last period of frames.
    tail_start = max(0, n - int(period))
    end = tail_start + int(np.argmax(cumscore[tail_start:]))
    beats = []
    while end >= 0:
        beats.append(end)
        end = backlink[end]
    return np.array(beats[::-1], dtype=int)


def beat_confidence(flux, fps, period_seconds):
    """How much onset energy a RIGID grid at the detected period collects at
    its best phase, in SD of the (z-scored) envelope, minus the ±1-frame
    tolerance a real beat needs.

    Deliberately not measured on the DP-tracked beats: the tracker chases
    local maxima, so on pure noise it still lands on peaks and reports a
    confident beat. A rigid grid cannot chase anything; on noise its best
    phase collects ~0.2 SD, on a click track several SD.
    """
    x = np.asarray(flux, dtype=np.float64)
    period = period_seconds * fps
    if len(x) == 0 or period < 2:
        return 0.0
    # Per ~15 s window, because the period is quantised to a frame lag and a
    # 0.2 BPM error drifts a rigid grid off the beat over a full track.
    win = max(int(15 * fps), int(4 * period))
    scores = []
    for start in range(0, len(x), win):
        chunk = x[start:start + win]
        if len(chunk) < 4 * period:
            continue
        n_phases = int(round(period))
        best = -np.inf
        for p in range(n_phases):
            idx = np.round(np.arange(p, len(chunk), period)).astype(int)
            idx = idx[idx < len(chunk)]
            if len(idx) < 4:
                continue
            # a beat may sit one frame either side of the grid
            neigh = np.stack([chunk[np.clip(idx + d, 0, len(chunk) - 1)] for d in (-1, 0, 1)]).max(axis=0)
            best = max(best, float(neigh.mean()))
        if np.isfinite(best):
            scores.append(best)
    return float(np.mean(scores)) if scores else 0.0


def downbeat_phase(flux, beat_frames):
    """4/4 phase (0-3) whose beats carry the most onset energy."""
    x = np.asarray(flux, dtype=np.float64)
    if len(beat_frames) < 4:
        return 0
    sums = [x[np.clip(beat_frames[p::4], 0, len(x) - 1)].sum() for p in range(4)]
    return int(np.argmax(sums))


def analyse(y, sr=SR):
    """Everything the manifest caches for one track."""
    flux, fps = onset_envelope(y, sr)
    bpm, period, strength = estimate_tempo(flux, fps)
    if bpm <= 0:
        return {"bpm": 0.0, "period": 0.0, "beats": [], "beat_confidence": 0.0,
                "downbeat_phase": 0, "tempo_strength": 0.0, "energy": []}
    frames = track_beats(flux, fps, period)
    conf = beat_confidence(flux, fps, period)
    phase = downbeat_phase(flux, frames)
    return {
        "bpm": round(bpm, 2),
        "period": round(period, 5),
        "beats": [round(float(f) / fps, 4) for f in frames],
        "beat_confidence": round(conf, 3),
        "downbeat_phase": phase,
        "tempo_strength": round(strength, 3),
        "energy": [round(float(v), 4) for v in energy_curve(flux, fps)],
    }


def energy_curve(flux, fps, window_seconds=2.0, step_seconds=0.5):
    """Rolling onset energy, 0-1, one value per ``step_seconds``. The
    ``energy`` fallback tier reads shot length off this."""
    x = np.maximum(np.asarray(flux, dtype=np.float64), 0.0)
    if len(x) == 0:
        return np.array([])
    win = max(1, int(window_seconds * fps))
    step = max(1, int(step_seconds * fps))
    kernel = np.ones(win) / win
    smooth = np.convolve(x, kernel, mode="same")
    sampled = smooth[::step]
    hi = sampled.max()
    return sampled / hi if hi > 0 else sampled


def phase_lock(cuts, beats, period):
    """Circular concentration of cut times against the beat grid (0 = random,
    1 = every cut on a beat). The reference channel scores 0.26-0.30; the
    acceptance bar for the ``beat`` tier is 0.80."""
    cuts = np.asarray(cuts, dtype=np.float64)
    if len(cuts) == 0 or period <= 0 or len(beats) == 0:
        return 0.0
    ph = 2 * np.pi * ((cuts - beats[0]) % period) / period
    return float(abs(np.exp(1j * ph).mean()))


# --- the schedule -----------------------------------------------------------

def usable_k(bpm, lo=SHOT_FINISHED_RANGE[0], hi=SHOT_FINISHED_RANGE[1]):
    """Integer beat counts whose finished length falls in the shot band."""
    if bpm <= 0:
        return []
    t = 60.0 / bpm
    return [k for k in range(1, 8) if lo - 1e-9 <= k * t <= hi + 1e-9]


def tier_for(grid):
    """Which rung of the fallback ladder a bed lands on."""
    if not grid or not grid.get("bpm"):
        return "free"
    ok_bpm = BEAT_BPM_RANGE[0] <= grid["bpm"] <= BEAT_BPM_RANGE[1]
    if ok_bpm and grid.get("beat_confidence", 0) >= BEAT_CONFIDENCE_MIN and usable_k(grid["bpm"]):
        return "beat"
    if grid.get("energy"):
        return "energy"
    return "free"


def _representable(n, ks):
    """Can ``n`` beats be tiled by shots of the usable ``ks``?"""
    if n <= 0:
        return n == 0
    reach = {0}
    for _ in range(n):
        reach |= {r + k for r in reach for k in ks if r + k <= n}
        if n in reach:
            return True
    return n in reach


def _nearest_representable(want, ks, ceiling):
    """The count nearest ``want`` that the ks tile, never above ``ceiling``
    (the footage the beat actually has); ties go to the smaller count."""
    if not ks:
        return max(1, want)
    ceiling = max(min(ks), ceiling)
    best = None
    for n in range(min(ks), ceiling + 1):
        if _representable(n, ks):
            if best is None or abs(n - want) < abs(best - want):
                best = n
    return best if best is not None else min(ks)


def _fill(n, ks, cycle, offset=0):
    """Shot lengths in beats summing exactly to ``n``, following ``cycle``
    where the tail still tiles, backtracking otherwise."""
    if n <= 0:
        return []
    order = list(cycle) + [k for k in sorted(ks, reverse=True) if k not in cycle]
    out = []
    remaining = n
    ci = offset
    while remaining > 0:
        placed = False
        for step in range(len(order)):
            k = order[(ci + step) % len(order)]
            if k <= remaining and _representable(remaining - k, ks):
                out.append(k)
                remaining -= k
                ci += 1
                placed = True
                break
        if not placed:  # n itself not tileable: hand the rest to the last shot
            if out:
                out[-1] += remaining
            else:
                out.append(remaining)
            remaining = 0
    return out


def _word_spans(words, start, end):
    return [(float(w["start"]), float(w["end"])) for w in words or []
            if float(w["end"]) > start and float(w["start"]) < end]


def _inside_word(t, spans):
    for a, b in spans:
        if a < t < b:
            return a, b
    return None


def schedule(beats, grid, *, speed=1.25, words=None, strictness="dialogue_first"):
    """Shot durations per story beat, in SOURCE seconds, locked to the grid.

    ``beats``: the validated beat plan's beats (dicts with ``start``, ``end``,
    ``weight``, ``captions``). ``grid``: ``analyse()`` output for the bed.
    ``words``: the film's word timings in source seconds (from
    ``film_prep.cues_to_transcript``), for the dialogue guard.

    Returns ``{"tier", "schedule": {beat_index: [src durations]}, "report"}``.
    ``schedule`` is what ``film_prep.expand_plan(schedule=...)`` consumes; a
    ``free``/``energy`` tier returns durations from the energy curve or none.
    """
    tier = tier_for(grid)
    report = {"tier": tier, "nudged": 0, "moved_a_beat": 0, "truncated": 0,
              "boundaries": 0, "downbeat_starts": 0, "bpm": grid.get("bpm") if grid else None}
    out = {}
    if tier == "energy":
        out = _energy_schedule(beats, grid, speed)
        report["boundaries"] = sum(len(v) for v in out.values())
        return {"tier": tier, "schedule": out, "report": report}
    if tier == "free":
        return {"tier": tier, "schedule": {}, "report": report}

    T = 60.0 / grid["bpm"]                  # one beat, finished seconds
    ks = usable_k(grid["bpm"])
    cycle = [k for k in K_CYCLE if k in ks] or [ks[len(ks) // 2]]
    cursor_beats = 0                        # finished position, in beats
    all_words = words or []
    last_k = {}                             # beat index -> k of its last shot

    for i, beat in enumerate(beats):
        src_len = float(beat["end"]) - float(beat["start"])
        n_beats = (src_len / speed) / T     # what the beat's footage buys
        key = beat.get("weight") == "key"

        # Start on a downbeat. Key beats always; normal beats when it is
        # cheap. The alignment is paid for by the PREVIOUS beat's last shot,
        # which grows by up to one beat or shrinks by up to one beat (staying
        # a legal k), never by stretching this beat's footage.
        phase = cursor_beats % 4
        wait = (4 - phase) % 4
        prev = out.get(i - 1)
        if wait and prev is not None and (key or wait <= 1 or wait == 3):
            prev_src = float(beats[i - 1]["end"]) - float(beats[i - 1]["start"])
            if wait == 1 and sum(prev) + T * speed <= prev_src + 1e-6:
                prev[-1] += T * speed
                cursor_beats += 1
            elif wait == 3 and last_k.get(i - 1, 0) - 1 >= min(ks):
                prev[-1] -= T * speed
                last_k[i - 1] -= 1
                cursor_beats -= 1
            elif key and wait <= 2 and sum(prev) + wait * T * speed <= prev_src + 1e-6:
                prev[-1] += wait * T * speed
                cursor_beats += wait
            elif key and last_k.get(i - 1, 0) - (4 - wait) >= min(ks):
                prev[-1] -= (4 - wait) * T * speed
                last_k[i - 1] -= (4 - wait)
                cursor_beats -= (4 - wait)
        if cursor_beats % 4 == 0:
            report["downbeat_starts"] += 1

        # Whole bars where they fit within ±1 beat, else whole beats, and in
        # both cases a count the usable ks can actually add up to (at 120 BPM
        # only k=3 fits the band, so n must be a multiple of 3).
        bars = round(n_beats / 4.0)
        want = bars * 4 if bars >= 1 and abs(bars * 4 - n_beats) <= 1.0 else max(1, int(round(n_beats)))
        n = _nearest_representable(want, ks, ceiling=int(n_beats + 1e-6))

        # Fill n beats with shots of k beats each; the key-line shot gets k+1.
        key_at = None
        caps = beat.get("captions") or []
        if caps and isinstance(caps[0], dict) and caps[0].get("at") is not None:
            key_at = float(caps[0]["at"])
        shots_k = _fill(n, ks, cycle, offset=i)
        # The shot under the first caption runs one beat longer, paid for by
        # the longest other shot so the beat still ends on the grid.
        if key_at is not None and len(shots_k) > 1:
            t_src = float(beat["start"])
            for si, k in enumerate(shots_k):
                seg = k * T * speed
                if t_src <= key_at < t_src + seg:
                    donor = max((j for j in range(len(shots_k)) if j != si),
                                key=lambda j: shots_k[j])
                    if shots_k[donor] - 1 >= min(ks) and shots_k[si] + 1 <= max(ks):
                        shots_k[donor] -= 1
                        shots_k[si] += 1
                    break
                t_src += seg

        durations = [k * T * speed for k in shots_k]
        total = sum(durations)
        if total > src_len + 1e-6:
            durations[-1] -= total - src_len
            report["truncated"] += 1
            if durations[-1] <= 0.3 and len(durations) > 1:
                durations.pop()
                shots_k.pop()
        cursor_beats += n
        last_k[i] = shots_k[-1] if shots_k else 0

        # Dialogue guard: a boundary inside a spoken word moves outside it
        # (<= 120 ms finished), or a whole beat if that is what it takes. A
        # whole-beat move may leave a neighbour at k=2 (1.0 s at 120 BPM):
        # slightly short beats being off the grid or cutting a word.
        if strictness == "dialogue_first" and all_words:
            spans = _word_spans(all_words, float(beat["start"]), float(beat["end"]))
            t = float(beat["start"])
            min_src = 2 * T * speed
            for si in range(len(durations) - 1):
                t += durations[si]
                hit = _inside_word(t, spans)
                if not hit:
                    continue
                a, b = hit
                nearest = a if (t - a) <= (b - t) else b
                delta = nearest - t
                if abs(delta) <= DIALOGUE_NUDGE_MAX * speed:
                    durations[si] += delta
                    durations[si + 1] -= delta
                    t = nearest
                    report["nudged"] += 1
                    continue
                for step in sorted((T * speed, -T * speed), key=lambda s: abs(delta - s)):
                    if (durations[si] + step >= min_src - 1e-6
                            and durations[si + 1] - step >= min_src - 1e-6
                            and not _inside_word(t + step, spans)):
                        durations[si] += step
                        durations[si + 1] -= step
                        t += step
                        report["moved_a_beat"] += 1
                        break
        out[i] = [round(d, 4) for d in durations]
        report["boundaries"] += max(0, len(durations) - 1)

    if report["boundaries"]:
        share = (report["nudged"] + report["moved_a_beat"]) / report["boundaries"]
        report["nudge_share"] = round(share, 3)
        report["tempo_fights_dialogue"] = share > NUDGE_WARN_SHARE
    return {"tier": tier, "schedule": out, "report": report}


def _energy_schedule(beats, grid, speed):
    """Shot length inversely proportional to the bed's rolling onset energy:
    1.8 s finished in quiet passages, 1.2 s in loud ones."""
    energy = list(grid.get("energy") or [])
    lo, hi = SHOT_FINISHED_RANGE
    out = {}
    cursor = 0.0  # finished seconds
    step = 0.5
    for i, beat in enumerate(beats):
        src_len = float(beat["end"]) - float(beat["start"])
        durations = []
        used = 0.0
        while src_len - used > lo * speed:
            e = energy[min(len(energy) - 1, int(cursor / step))] if energy else 0.5
            fin = hi - e * (hi - lo)
            d = min(fin * speed, src_len - used)
            if src_len - used - d < lo * speed * 0.6:
                d = src_len - used
            durations.append(round(d, 4))
            used += d
            cursor += d / speed
        if not durations:
            durations = [round(src_len, 4)]
        out[i] = durations
    return out
