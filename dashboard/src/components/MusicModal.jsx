import { useEffect, useRef, useState } from 'react';
import { Loader2, Music, Pause, Play, Upload, VolumeX } from 'lucide-react';
import Modal from './ui/Modal';
import { getApiUrl } from '../config';
import { apiFetch } from '../lib/api';

/**
 * Background music for one clip (or every clip of the job), applied after the
 * render. The spec handed to onApply / onApplyAll is
 * `{ track, volume_db, duck, start }`, or `null` to remove the music.
 *
 * The preview here plays the clip and the track together at the chosen gain
 * and offset; the server render additionally ducks the music under speech,
 * which a browser preview cannot do.
 */

const DEFAULT_VOLUME_DB = -18;
const DEFAULT_DUCK = 70;
const DEFAULT_START = 0;
const FALLBACK_START_MAX = 60;

// Fetched once per page load; refetched after an upload. The list is tiny and
// every card would otherwise hit the endpoint each time its modal opens.
let trackCache = null;
let trackFetch = null;

async function fetchTracks(force = false) {
    if (!force && trackCache) return trackCache;
    if (!force && trackFetch) return trackFetch;
    trackFetch = apiFetch('/api/music')
        .then(async (res) => {
            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();
            trackCache = Array.isArray(data.tracks) ? data.tracks : [];
            return trackCache;
        })
        .finally(() => { trackFetch = null; });
    return trackFetch;
}

function formatClock(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function gainFromDb(db) {
    return Math.min(1, Math.max(0, Math.pow(10, db / 20)));
}

function clampStart(start, max) {
    return Math.min(Math.max(0, Math.round(start || 0)), max);
}

function Slider({ label, value, min, max, step, format, onChange, hint }) {
    return (
        <div>
            <div className="flex items-center justify-between mb-1">
                <span className="readout">{label}</span>
                <span className="readout">{format(value)}</span>
            </div>
            <input
                type="range"
                min={min}
                max={max}
                step={step}
                value={value}
                onChange={(e) => onChange(parseFloat(e.target.value))}
                className="w-full accent-[var(--color-accent)]"
                aria-label={label}
            />
            {hint && <p className="text-[11px] leading-relaxed text-muted mt-1">{hint}</p>}
        </div>
    );
}

export default function MusicModal({ isOpen, onClose, clip, clipCount = 1, bulkProgress, isProcessing, onApply, onApplyAll, videoUrl }) {
    const current = clip?.music || null;

    const [tracks, setTracks] = useState(trackCache || []);
    const [loadingTracks, setLoadingTracks] = useState(false);
    const [loadError, setLoadError] = useState(null);
    const [uploading, setUploading] = useState(false);

    const [track, setTrack] = useState(current?.track || null);
    const [volumeDb, setVolumeDb] = useState(current?.volume_db ?? DEFAULT_VOLUME_DB);
    const [duck, setDuck] = useState(current?.duck ?? DEFAULT_DUCK);
    const [start, setStart] = useState(current?.start ?? DEFAULT_START);

    // Which preview is sounding: a track row ('track') or the clip ('clip').
    const [previewing, setPreviewing] = useState(null);

    // One <audio> for every preview, kept outside the Modal so it survives the
    // modal's unmount and can always be stopped from here.
    const audioRef = useRef(null);
    const videoRef = useRef(null);
    const fileInputRef = useRef(null);

    const stopAll = () => {
        const a = audioRef.current;
        if (a) { a.pause(); a.removeAttribute('src'); a.load(); }
        const v = videoRef.current;
        if (v) v.pause();
        setPreviewing(null);
    };

    // Re-seed from the clip each time the modal opens so it never shows a
    // stale choice; stop every preview each time it closes.
    useEffect(() => {
        if (!isOpen) { stopAll(); return; }
        setTrack(current?.track || null);
        setVolumeDb(current?.volume_db ?? DEFAULT_VOLUME_DB);
        setDuck(current?.duck ?? DEFAULT_DUCK);
        setStart(current?.start ?? DEFAULT_START);
    }, [isOpen, current?.track, current?.volume_db, current?.duck, current?.start]);

    // Stop on unmount too.
    useEffect(() => () => {
        const a = audioRef.current;
        if (a) { a.pause(); a.removeAttribute('src'); a.load(); }
    }, []);

    useEffect(() => {
        if (!isOpen) return;
        let cancelled = false;
        setLoadingTracks(!trackCache);
        setLoadError(null);
        fetchTracks()
            .then((list) => { if (!cancelled) setTracks(list); })
            .catch((e) => { if (!cancelled) setLoadError(e.message || 'could not load the music library'); })
            .finally(() => { if (!cancelled) setLoadingTracks(false); });
        return () => { cancelled = true; };
    }, [isOpen]);

    const selected = tracks.find((t) => t.file === track) || null;
    const startMax = selected?.duration && selected.duration > 5
        ? Math.floor(selected.duration - 5)
        : FALLBACK_START_MAX;

    // A shorter track than the offset the user had chosen: pull the offset in.
    useEffect(() => {
        setStart((s) => clampStart(s, startMax));
    }, [startMax]);

    // Live gain while a preview sounds.
    useEffect(() => {
        if (audioRef.current) audioRef.current.volume = gainFromDb(volumeDb);
    }, [volumeDb]);

    // Live re-seek while a preview sounds.
    useEffect(() => {
        const a = audioRef.current;
        if (!a || !previewing) return;
        const v = videoRef.current;
        a.currentTime = previewing === 'clip' && v ? start + v.currentTime : start;
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [start]);

    const trackUrl = (file) => getApiUrl('/music/' + encodeURIComponent(file));

    const loadTrack = (file) => {
        const a = audioRef.current;
        if (!a) return null;
        const url = trackUrl(file);
        if (a.dataset.file !== file) {
            a.src = url;
            a.dataset.file = file;
            a.load();
        }
        a.volume = gainFromDb(volumeDb);
        return a;
    };

    const togglePreview = (file) => {
        if (previewing === 'track' && track === file) {
            stopAll();
            return;
        }
        if (videoRef.current) videoRef.current.pause();
        setTrack(file);
        const a = loadTrack(file);
        if (!a) return;
        a.currentTime = start;
        a.play().then(() => setPreviewing('track')).catch(() => setPreviewing(null));
    };

    const toggleClipPreview = () => {
        const v = videoRef.current;
        if (!v) return;
        if (previewing === 'clip') {
            v.pause();
            stopAll();
            return;
        }
        if (!track) return;
        loadTrack(track);
        v.muted = false;
        v.currentTime = 0;
        v.play().catch(() => setPreviewing(null));
    };

    // The clip drives the sync: on every play the track jumps to
    // start + video position, on pause/end the track stops.
    const onVideoPlay = () => {
        const a = audioRef.current;
        const v = videoRef.current;
        if (!a || !v || !track) return;
        loadTrack(track);
        a.currentTime = start + v.currentTime;
        a.play().then(() => setPreviewing('clip')).catch(() => {});
    };
    const onVideoPause = () => {
        const a = audioRef.current;
        if (a) a.pause();
        setPreviewing((p) => (p === 'clip' ? null : p));
    };

    const handleUpload = async (e) => {
        const file = e.target.files?.[0];
        e.target.value = '';
        if (!file) return;
        setUploading(true);
        setLoadError(null);
        try {
            const fd = new FormData();
            fd.append('file', file);
            const res = await apiFetch('/api/music/upload', { method: 'POST', body: fd });
            if (!res.ok) {
                const text = await res.text();
                let detail = text;
                try { detail = JSON.parse(text).detail || text; } catch { /* plain text */ }
                throw new Error(detail);
            }
            const data = await res.json();
            const list = await fetchTracks(true);
            setTracks(list);
            if (data.track?.file) {
                stopAll();
                setTrack(data.track.file);
                setStart(DEFAULT_START);
            }
        } catch (err) {
            setLoadError(err.message || 'upload failed');
        } finally {
            setUploading(false);
        }
    };

    const buildSpec = () => (track ? { track, volume_db: volumeDb, duck, start } : null);

    const bulkRunning = !!bulkProgress?.running;
    const busy = isProcessing || bulkRunning || uploading;
    // "No music" on a clip that has none is a no-op; everything else applies.
    const nothingToApply = !track && !current;
    const disabled = busy || nothingToApply;

    const handleClose = () => {
        stopAll();
        onClose?.();
    };

    const apply = (fn, spec) => {
        stopAll();
        fn(spec);
    };

    return (
        <>
            <audio ref={audioRef} preload="none" onEnded={() => setPreviewing(null)} />
            <Modal
                isOpen={isOpen}
                onClose={handleClose}
                size="md"
                eyebrow="BACKGROUND MUSIC"
                title="music"
                footer={
                    <div className="space-y-2">
                        <div className="flex gap-2">
                            <button type="button" onClick={handleClose} className="btn-ghost" disabled={busy}>
                                cancel
                            </button>
                            <button
                                type="button"
                                onClick={() => apply(onApply, buildSpec())}
                                disabled={disabled}
                                className="btn-primary flex-1"
                            >
                                {(isProcessing && !bulkRunning) && <Loader2 size={16} className="animate-spin" />}
                                {(isProcessing && !bulkRunning) ? 'applying…' : 'apply to this clip'}
                            </button>
                        </div>
                        {onApplyAll && clipCount > 1 && (
                            <button
                                type="button"
                                onClick={() => apply(onApplyAll, buildSpec())}
                                disabled={disabled}
                                className="btn-ghost w-full flex items-center justify-center gap-2"
                            >
                                {bulkRunning
                                    ? <><Loader2 size={16} className="animate-spin" />applying {bulkProgress.current} / {bulkProgress.total}</>
                                    : `apply to all ${clipCount} clips`}
                            </button>
                        )}
                        {current && (
                            <button
                                type="button"
                                onClick={() => apply(onApply, null)}
                                disabled={busy}
                                className="w-full text-[11px] lowercase text-muted hover:text-danger transition-colors py-1 disabled:opacity-45 disabled:cursor-not-allowed"
                            >
                                remove music from this clip
                            </button>
                        )}
                    </div>
                }
            >
                <div className="space-y-5">
                    {/* Track list */}
                    <div>
                        <div className="flex items-center justify-between gap-3 mb-2">
                            <p className="eyebrow">Track</p>
                            <button
                                type="button"
                                onClick={() => fileInputRef.current?.click()}
                                disabled={uploading}
                                className="flex items-center gap-1.5 text-[11px] lowercase text-ink2 hover:text-brass transition-colors disabled:opacity-45"
                            >
                                {uploading ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
                                {uploading ? 'uploading…' : 'upload'}
                            </button>
                            <input
                                ref={fileInputRef}
                                type="file"
                                accept="audio/*,video/*"
                                className="hidden"
                                onChange={handleUpload}
                            />
                        </div>

                        <div className="space-y-1.5 max-h-56 overflow-y-auto custom-scrollbar pr-0.5">
                            <button
                                type="button"
                                onClick={() => { stopAll(); setTrack(null); }}
                                className={`w-full flex items-center gap-3 px-3 py-2 rounded-input border text-left transition-colors ${
                                    track === null
                                        ? 'border-[var(--color-accent)] bg-paper3'
                                        : 'border-rule bg-paper hover:bg-paper3'
                                }`}
                            >
                                <span className="w-7 h-7 flex items-center justify-center rounded-full text-muted shrink-0">
                                    <VolumeX size={14} />
                                </span>
                                <span className="text-xs text-ink2 lowercase flex-1">no music</span>
                            </button>

                            {loadingTracks && (
                                <div className="flex items-center gap-2 px-3 py-2 text-xs text-muted">
                                    <Loader2 size={14} className="animate-spin" /> loading library…
                                </div>
                            )}

                            {!loadingTracks && tracks.length === 0 && !loadError && (
                                <p className="px-3 py-2 text-[11px] text-muted">
                                    the library is empty. upload an mp3, m4a, wav or a video to use its audio.
                                </p>
                            )}

                            {tracks.map((t) => {
                                const isSelected = track === t.file;
                                const isPlaying = isSelected && previewing === 'track';
                                return (
                                    <div
                                        key={t.file}
                                        role="button"
                                        tabIndex={0}
                                        onClick={() => { if (!isSelected) { stopAll(); setTrack(t.file); } }}
                                        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); stopAll(); setTrack(t.file); } }}
                                        className={`w-full flex items-center gap-3 px-3 py-2 rounded-input border text-left cursor-pointer transition-colors ${
                                            isSelected
                                                ? 'border-[var(--color-accent)] bg-paper3'
                                                : 'border-rule bg-paper hover:bg-paper3'
                                        }`}
                                    >
                                        <button
                                            type="button"
                                            onClick={(e) => { e.stopPropagation(); togglePreview(t.file); }}
                                            aria-label={isPlaying ? `pause ${t.name}` : `play ${t.name}`}
                                            className={`w-7 h-7 flex items-center justify-center rounded-full border shrink-0 transition-colors ${
                                                isPlaying ? 'border-[var(--color-accent)] text-brass' : 'border-rule text-muted hover:text-brass'
                                            }`}
                                        >
                                            {isPlaying ? <Pause size={13} /> : <Play size={13} />}
                                        </button>
                                        <span className="text-xs text-ink2 truncate flex-1 min-w-0" title={t.name}>{t.name}</span>
                                        {t.duration != null && (
                                            <span className="readout shrink-0">{formatClock(t.duration)}</span>
                                        )}
                                    </div>
                                );
                            })}
                        </div>

                        {loadError && (
                            <p className="text-[11px] text-danger mt-2 break-words">{loadError}</p>
                        )}
                    </div>

                    {/* Levels */}
                    <div className="pt-4 border-t border-rule space-y-4">
                        <Slider
                            label="VOLUME"
                            value={volumeDb}
                            min={-40}
                            max={0}
                            step={1}
                            format={(v) => `${v} dB`}
                            onChange={setVolumeDb}
                            hint="Resting level of the music. It is ducked further while someone speaks."
                        />
                        <Slider
                            label="DUCKING"
                            value={duck}
                            min={0}
                            max={100}
                            step={1}
                            format={(v) => `${v}%`}
                            onChange={setDuck}
                            hint="how hard the music dips under the voice"
                        />
                        <Slider
                            label="START AT"
                            value={clampStart(start, startMax)}
                            min={0}
                            max={startMax}
                            step={1}
                            format={formatClock}
                            onChange={(v) => setStart(clampStart(v, startMax))}
                            hint="seek into the track so a beat lands at the clip start"
                        />
                    </div>

                    {/* Preview with the clip */}
                    {videoUrl && (
                        <div className="pt-4 border-t border-rule">
                            <div className="flex items-center justify-between gap-3 mb-2">
                                <p className="eyebrow">Preview</p>
                                <button
                                    type="button"
                                    onClick={toggleClipPreview}
                                    disabled={!track}
                                    className="flex items-center gap-1.5 text-[11px] lowercase text-ink2 hover:text-brass transition-colors disabled:opacity-45 disabled:cursor-not-allowed"
                                >
                                    {previewing === 'clip' ? <Pause size={13} /> : <Music size={13} />}
                                    {previewing === 'clip' ? 'stop preview' : 'preview with clip'}
                                </button>
                            </div>
                            <div className="bg-black rounded-input overflow-hidden mx-auto max-w-[180px] aspect-[9/16]">
                                <video
                                    ref={videoRef}
                                    src={videoUrl}
                                    controls
                                    playsInline
                                    preload="metadata"
                                    className="w-full h-full object-contain"
                                    onPlay={onVideoPlay}
                                    onPause={onVideoPause}
                                    onEnded={onVideoPause}
                                    onSeeked={() => {
                                        const a = audioRef.current;
                                        const v = videoRef.current;
                                        if (a && v && previewing === 'clip') a.currentTime = start + v.currentTime;
                                    }}
                                />
                            </div>
                            <p className="text-[11px] leading-relaxed text-muted mt-2">
                                The render also ducks the music under speech; this preview does not.
                            </p>
                        </div>
                    )}
                </div>
            </Modal>
        </>
    );
}
