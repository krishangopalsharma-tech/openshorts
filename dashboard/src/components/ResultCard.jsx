import React, { useState, useEffect } from 'react';
import { Download, Share2, Instagram, Youtube, Video, AlertCircle, Loader2, Copy, Check, Wand2, Type, Calendar, Languages, FileText, Link2, Scissors, Crosshair, TrendingUp, Clapperboard, Music, ImagePlus } from 'lucide-react';
import { getApiUrl } from '../config';
import { apiFetch } from '../lib/api';
import SubtitleModal from './SubtitleModal';
import LookModal from './LookModal';
import MusicModal from './MusicModal';
import OverlayEditor from './OverlayEditor';
import TranslateModal from './TranslateModal';
import Modal from './ui/Modal';
import SegmentedControl from './ui/SegmentedControl';
import WatermarkModal, { watermarkNoticeDismissed } from './WatermarkModal';
import TikTokDraftNotice from './TikTokDraftNotice';
import { useAuth } from '../contexts/AuthContext';
import { renderInBrowser } from '../lib/renderInBrowser';

const QUIET_BTN = 'group flex flex-col items-center justify-center gap-1 py-2.5 sm:py-2 px-1 rounded-input border border-rule hover:bg-paper3 text-[11px] lowercase text-ink2 whitespace-nowrap transition-colors disabled:opacity-45 disabled:cursor-not-allowed';

const PLATFORM_OPTIONS = [
    { value: 'tiktok', label: 'tiktok', icon: <Video size={16} /> },
    { value: 'instagram', label: 'instagram', icon: <Instagram size={16} /> },
    { value: 'youtube', label: 'youtube', icon: <Youtube size={16} /> },
];

function clipDurationSeconds(clip) {
    // A recut clip's start/end are the covering source range (segments may be
    // non-contiguous or reordered); its real duration is the segment sum.
    const segments = clip.recipe?.segments;
    if (segments?.length) {
        return segments.reduce((acc, s) => acc + (s.end - s.start), 0);
    }
    return clip.end && clip.start ? clip.end - clip.start : NaN;
}

function formatDuration(clip) {
    const secs = Math.floor(clipDurationSeconds(clip));
    if (!Number.isFinite(secs) || secs < 0) return null;
    return `${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(secs % 60).padStart(2, '0')}`;
}

// Badge labels for the clip's output format and colour grade. Mirrors the
// option lists in LookModal (kept local: component files may only export
// components under the react-refresh lint rule).
const FORMAT_LABELS = { vertical: '9:16', square: '1:1', horizontal: '16:9' };
const GRADE_LABELS = {
    warm: 'Warm', cool: 'Cool', teal_orange: 'Teal & Orange',
    vintage: 'Vintage', vibrant: 'Vibrant', bw: 'Black & White',
};

// Tailwind classes for the preview column per output format. Written out in
// full (not composed) so the JIT scanner finds them.
const PREVIEW_ASPECT = {
    vertical: 'aspect-[9/16] md:w-[236px]',
    square: 'aspect-square md:w-[320px]',
    horizontal: 'aspect-video md:w-[320px]',
};

export default function ResultCard({ clip, index, jobId, durable, uploadPostKey, uploadUserId, geminiApiKey, elevenLabsKey, isManaged, onPlay, onPause, onBulkSubtitle, onBulkLook, onBulkMusic, onBulkOverlays, clipCount = 1, bulkProgress, bulkLookProgress, bulkMusicProgress, bulkOverlaysProgress, initialState = null, onStateChange, connectedPlatforms = null, onConnectSocials, onEditClip = null, onReframeClip = null }) {
    const [showModal, setShowModal] = useState(false);
    const [showDescModal, setShowDescModal] = useState(false);
    const [showSubtitleModal, setShowSubtitleModal] = useState(false);
    const [showLookModal, setShowLookModal] = useState(false);
    const [showMusicModal, setShowMusicModal] = useState(false);
    const [showOverlayEditor, setShowOverlayEditor] = useState(false);
    const [showWatermarkModal, setShowWatermarkModal] = useState(false);
    // The clip's output format, mirrored locally so the card reflects a look
    // change before the parent refreshes the job result.
    const [outputFormat, setOutputFormat] = useState(clip.output_format || 'vertical');
    useEffect(() => {
        setOutputFormat(clip.output_format || 'vertical');
    }, [clip.output_format]);
    // The clip's background music ({ track, volume_db, duck, start } or null),
    // mirrored locally for the same reason: the badge and the modal reflect an
    // apply before the parent refreshes the job result.
    const [music, setMusic] = useState(clip.music || null);
    useEffect(() => {
        setMusic(clip.music || null);
    }, [clip.music]);
    // The clip's logo/text overlays (array of items or []), mirrored locally
    // for the badge and the editor's initial state.
    const [overlays, setOverlays] = useState(Array.isArray(clip.overlays) ? clip.overlays : []);
    useEffect(() => {
        setOverlays(Array.isArray(clip.overlays) ? clip.overlays : []);
    }, [clip.overlays]);
    const { plan } = useAuth();
    const videoRef = React.useRef(null);
    // Pristine base clip (no burned subtitles/hook), stable regardless of how
    // clip.video_url mutates after server edits. Used as the compositing base
    // for the Remotion preview so it never stacks subtitles over an already-
    // subtitled file (double-subtitle bug).
    const stripBurns = (filename) => {
        let f = filename || '', prev;
        do { prev = f; f = f.replace(/^subtitled_\d+_/, '').replace(/^hooked_\d+_/, '').replace(/^hook_/, ''); } while (f !== prev);
        return f;
    };
    const originalVideoUrl = getApiUrl((clip.video_url || '').replace(/[^/]+$/, stripBurns((clip.video_url || '').split('/').pop())));
    const [currentVideoUrl, setCurrentVideoUrl] = useState(getApiUrl(clip.video_url));
    // Where the <video> element pulls its bytes from. The clips are archived to
    // R2 anyway, and R2 egress is free and edge-served, while /videos is served
    // by the same single-worker API process that is running the renders. So play
    // from R2 when possible.
    //
    // ONLY when R2 holds exactly the file the server considers current for this
    // clip (durable.filename === serverVideoFile). Every server-side edit sends
    // input_filename: serverVideoFile and rewrites clip.video_url, but the R2
    // re-archive behind it is fire-and-forget, so for a few seconds the durable
    // copy is the PRE-edit clip. Preferring it blindly would silently show the
    // clip without the subtitles/hook the user just burned.
    //
    // This is display-only: currentVideoUrl remains the source of truth for the
    // download button and for every server operation, so no edit can be routed
    // to the wrong file by this.
    const [durableSrc, setDurableSrc] = useState(null);
    const [durableFailed, setDurableFailed] = useState(false);
    // Switching src reloads the element and restarts playback, so the durable copy
    // is only ever adopted before the user has touched this player. After an edit
    // the chase in App.jsx can land while they are watching the result, and losing
    // their position to save a few seconds of buffering is a bad trade.
    const [hasPlayed, setHasPlayed] = useState(false);

    // A delivered clip is tens of MB, and on a slow link the old silent
    // fetch-then-save took minutes with nothing on screen, which reads as a dead
    // button. Stream it instead and report progress.
    const [downloadPct, setDownloadPct] = useState(null);

    const downloadClip = async () => {
        try {
            setDownloadPct(0);
            const response = await fetch(currentVideoUrl);
            if (!response.ok) throw new Error('Download failed');
            const total = Number(response.headers.get('content-length')) || 0;
            let blob;
            // No body reader (old browser) or no length to measure against: fall
            // back to the plain path rather than lose the download.
            if (!response.body || !total) {
                blob = await response.blob();
            } else {
                const reader = response.body.getReader();
                const chunks = [];
                let received = 0;
                for (;;) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    chunks.push(value);
                    received += value.length;
                    setDownloadPct(Math.min(99, Math.round((received / total) * 100)));
                }
                blob = new Blob(chunks, { type: 'video/mp4' });
            }
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = `clip-${index + 1}.mp4`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
        } catch (err) {
            console.error('Download error:', err);
            window.open(currentVideoUrl, '_blank');
        } finally {
            setDownloadPct(null);
        }
    };
    // Latest file that exists ON THE SERVER (blob: previews don't count).
    // All server-side operations must chain from this, so burned-in edits
    // (subtitles, hooks, effects) never get silently dropped.
    // A reopened project seeds it from the persisted project state.
    const [serverVideoFile, setServerVideoFile] = useState(initialState?.server_file || (clip.video_url || '').split('/').pop());
    const [videoErrored, setVideoErrored] = useState(false);
    const [resolution, setResolution] = useState(null);

    // Adopt the durable copy only while it matches the current server file, and
    // pin the first signed URL seen for that file: /api/history mints a fresh
    // signature on every call, and swapping src mid-playback restarts the video.
    useEffect(() => {
        if (durable?.url && durable.filename && durable.filename === serverVideoFile && !hasPlayed) {
            setDurableSrc((prev) => prev || durable.url);
        } else {
            setDurableSrc(null);
        }
    }, [durable?.url, durable?.filename, serverVideoFile, hasPlayed]);

    // If the local video failed and a durable R2 URL is (now) available, use it.
    // Handles the race where the video errors before the durable URL has loaded.
    // Deliberately NOT version-gated: reaching here means the local file is gone
    // (retention sweep after a reload), so an older durable copy still beats a
    // broken player.
    useEffect(() => {
        if (videoErrored && durable?.url && currentVideoUrl !== durable.url) {
            setCurrentVideoUrl(durable.url);
            setVideoErrored(false);
        }
    }, [videoErrored, durable, currentVideoUrl]);

    // When an external refresh changes this clip's server file (e.g. bulk
    // subtitles applied from another card), adopt it so the card shows the
    // freshly subtitled video instead of a stale one.
    useEffect(() => {
        const serverUrl = getApiUrl(clip.video_url);
        const serverName = (clip.video_url || '').split('/').pop();
        if (serverName && serverName !== serverVideoFile) {
            setServerVideoFile(serverName);
            setCurrentVideoUrl(serverUrl);
            setDurableFailed(false);
            setHasPlayed(false);
            if (videoRef.current) videoRef.current.load();
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [clip.video_url]);

    const [platforms, setPlatforms] = useState({
        tiktok: true,
        instagram: true,
        youtube: true
    });
    const [postTitle, setPostTitle] = useState("");
    const [postDescription, setPostDescription] = useState("");
    const [isScheduling, setIsScheduling] = useState(false);
    const [scheduleDate, setScheduleDate] = useState("");

    const [posting, setPosting] = useState(false);
    const [postResult, setPostResult] = useState(null);
    const [copied, setCopied] = useState(null);

    const handleCopy = async (field, text) => {
        try {
            await navigator.clipboard.writeText(text || '');
            setCopied(field);
            setTimeout(() => setCopied(null), 2000);
        } catch {
            // clipboard unavailable — silent
        }
    };

    const [isEditing, setIsEditing] = useState(false);
    const [isSubtitling, setIsSubtitling] = useState(false);
    const [isLooking, setIsLooking] = useState(false);
    const [isApplyingMusic, setIsApplyingMusic] = useState(false);
    const [isApplyingOverlays, setIsApplyingOverlays] = useState(false);
    const [isTranslating, setIsTranslating] = useState(false);
    const [showTranslateModal, setShowTranslateModal] = useState(false);
    const [editError, setEditError] = useState(null);

    const [clipDuration, setClipDuration] = useState(() => {
        const secs = clipDurationSeconds(clip);
        return Number.isFinite(secs) ? secs : 30;
    });

    // Accumulate Remotion layers across operations. A reopened project restores
    // the layers persisted in its project state, so the next edit composes over
    // them instead of silently dropping previous browser-side work.
    const [activeLayers, setActiveLayers] = useState(initialState?.active_layers || { subtitles: null, hook: null, effects: null });

    // Report edit state upward (debounced sync to the project record). Skip the
    // mount run: only user-driven changes are worth persisting.
    const stateReported = React.useRef(false);
    useEffect(() => {
        if (!stateReported.current) { stateReported.current = true; return; }
        onStateChange?.(index, { activeLayers, serverVideoFile });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeLayers, serverVideoFile]);

    // True when the current server file already carries burned-in content.
    // Browser (Remotion) renders compose over the ORIGINAL clip, so using them
    // here would silently drop those burns — chain via server FFmpeg instead.
    const hasServerBurns = /(^|_)(subtitled|hook|hooked)_/.test(serverVideoFile || '');

    // The file UNDER the caption and overlay layers, for the overlay editor's
    // stage: previewing the current file would show the burned overlays under
    // the editable copies of the same items. Same prefix scheme the server
    // walks back (subtitled_<ts>_ outermost, then the legacy hook, then the
    // ov_<hex>_ layer); the stripped name is still served from /videos/.
    const overlayStageFile = (serverVideoFile || '')
        .replace(/^subtitled_\d+_/, '')
        .replace(/^(?:hooked_\d+_|hook_)/, '')
        .replace(/^(?:ov|overlay)_(?:\d+_)?[0-9a-f]{6}_/, '');
    const overlayStageUrl = (serverVideoFile && overlayStageFile !== serverVideoFile && jobId)
        ? getApiUrl(`/videos/${jobId}/${overlayStageFile}`)
        : currentVideoUrl;

    // Fetch clip duration from transcript endpoint
    useEffect(() => {
        if (!jobId || index === undefined) return;
        apiFetch(`/api/clip/${jobId}/${index}/transcript`)
            .then(res => res.ok ? res.json() : null)
            .then(data => {
                if (data && data.durationSec) setClipDuration(data.durationSec);
            })
            .catch(() => {});
    }, [jobId, index]);

    // Which platforms the selected profile actually has linked. `null` means
    // unknown (profile list not loaded) — in that case nothing is gated.
    const knownConnections = Array.isArray(connectedPlatforms);
    const noAccountsConnected = knownConnections && connectedPlatforms.length === 0;
    const platformOptions = knownConnections
        ? PLATFORM_OPTIONS.map((o) => (connectedPlatforms.includes(o.value) ? o : { ...o, disabled: true, hint: 'not connected' }))
        : PLATFORM_OPTIONS;

    const handleConnectAccounts = () => {
        setShowModal(false);
        if (onConnectSocials) onConnectSocials();
        else window.open('https://app.upload-post.com', '_blank', 'noopener');
    };

    // Initialize/Reset form when modal opens
    useEffect(() => {
        if (showModal) {
            setPostTitle(clip.video_title_for_youtube_short || "Viral Short");
            setPostDescription(clip.video_description_for_instagram || clip.video_description_for_tiktok || "");
            setIsScheduling(false);
            setScheduleDate("");
            setPostResult(null);
            // Only preselect platforms the profile can actually publish to.
            if (knownConnections) {
                setPlatforms({
                    tiktok: connectedPlatforms.includes('tiktok'),
                    instagram: connectedPlatforms.includes('instagram'),
                    youtube: connectedPlatforms.includes('youtube'),
                });
            }
        }
        // Reset only when the modal opens for a clip; connection changes while
        // it is open must not wipe the user's selection.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [showModal, clip]);

    const handleAutoEdit = async () => {
        setIsEditing(true);
        setEditError(null);
        try {
            const apiKey = geminiApiKey || localStorage.getItem('gemini_key');

            // Managed (paid) users get the Gemini key resolved server-side;
            // only BYOK/self-host needs a local key.
            if (!apiKey && !isManaged) {
                throw new Error("Gemini API Key is missing. Please set it in Settings.");
            }
            const geminiHeaders = apiKey ? { 'X-Gemini-Key': apiKey } : {};

            // Try Remotion effects endpoint first
            const effectsRes = hasServerBurns ? null : await apiFetch('/api/effects/generate', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    ...geminiHeaders
                },
                body: JSON.stringify({
                    job_id: jobId,
                    clip_index: index,
                    input_filename: serverVideoFile
                })
            });

            if (effectsRes && effectsRes.ok) {
                const data = await effectsRes.json();
                if (data.effects && data.effects.segments) {
                    const newLayers = { ...activeLayers, effects: data.effects };
                    setActiveLayers(newLayers);
                    const blobUrl = await renderInBrowser({
                        videoUrl: originalVideoUrl,
                        durationInSeconds: clipDuration,
                        subtitles: newLayers.subtitles,
                        hook: newLayers.hook,
                        effects: newLayers.effects,
                    });
                    setCurrentVideoUrl(blobUrl);
                    if (videoRef.current) videoRef.current.load();
                    return;
                }
            }

            // Fallback: legacy FFmpeg edit endpoint
            const res = await apiFetch('/api/edit', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    ...geminiHeaders
                },
                body: JSON.stringify({
                    job_id: jobId,
                    clip_index: index,
                    input_filename: serverVideoFile
                })
            });

            if (!res.ok) {
                const errText = await res.text();
                try {
                    const jsonErr = JSON.parse(errText);
                    throw new Error(jsonErr.detail || errText);
                } catch (e) {
                    throw new Error(errText);
                }
            }

            const data = await res.json();
            if (data.new_video_url) {
                setCurrentVideoUrl(getApiUrl(data.new_video_url));
                setServerVideoFile(data.new_video_url.split('/').pop());
                if (videoRef.current) {
                    videoRef.current.load();
                }
            }

        } catch (e) {
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsEditing(false);
        }
    };

    // Clips are captioned by default, so "no captions" has to be reachable.
    // Nothing is re-encoded: the server still holds the clean file next to the
    // captioned one and just points this clip back at it.
    const handleRemoveSubtitles = async () => {
        setIsSubtitling(true);
        setEditError(null);
        try {
            const res = await apiFetch('/api/subtitle/remove', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    job_id: jobId, clip_index: index, input_filename: serverVideoFile,
                }),
            });
            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();
            if (data.new_video_url) {
                const serverUrl = getApiUrl(data.new_video_url);
                setServerVideoFile(data.new_video_url.split('/').pop());
                const remaining = { ...activeLayers, subtitles: null };
                setActiveLayers(remaining);
                if (remaining.hook || remaining.effects) {
                    setCurrentVideoUrl(await renderInBrowser({
                        videoUrl: serverUrl,
                        durationInSeconds: clipDuration,
                        subtitles: null,
                        hook: remaining.hook,
                        effects: remaining.effects,
                    }));
                } else {
                    setCurrentVideoUrl(serverUrl);
                }
                if (videoRef.current) videoRef.current.load();
                setShowSubtitleModal(false);
            }
        } catch (e) {
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsSubtitling(false);
        }
    };

    const handleSubtitle = async (options) => {
        setIsSubtitling(true);
        setEditError(null);
        try {
            // In-browser Remotion preview: only when the caller hands a Remotion
            // config AND the server file has no burned-in content to preserve.
            // The current SubtitleModal never does; captions are burned
            // server-side from a preset. Kept harmless for callers that do.
            if (options.remotion && !hasServerBurns) {
                // Accumulate layer and render all layers together
                const newLayers = { ...activeLayers, subtitles: options.remotion };
                setActiveLayers(newLayers);
                const blobUrl = await renderInBrowser({
                    videoUrl: originalVideoUrl,
                    durationInSeconds: clipDuration,
                    subtitles: newLayers.subtitles,
                    hook: newLayers.hook,
                    effects: newLayers.effects,
                });
                setCurrentVideoUrl(blobUrl);
                if (videoRef.current) videoRef.current.load();
                setShowSubtitleModal(false);
                return;
            }

            // Server-side burn: a named preset plus the user's overrides.
            const res = await apiFetch('/api/subtitle', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    job_id: jobId,
                    clip_index: index,
                    preset: options.preset,
                    overrides: options.overrides || {},
                    input_filename: serverVideoFile,
                    // Edited caption text (clip-relative ms); null = server
                    // regenerates from the transcript as before.
                    words: options.captions || null
                })
            });

            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();
            if (data.new_video_url) {
                const serverUrl = getApiUrl(data.new_video_url);
                setServerVideoFile(data.new_video_url.split('/').pop());
                // Subtitles are burned into the server file now — drop the
                // browser subtitle layer and re-compose any remaining browser
                // layers (hook/effects) over the new file so they aren't lost.
                const remaining = { ...activeLayers, subtitles: null };
                setActiveLayers(remaining);
                if (remaining.hook || remaining.effects) {
                    const blobUrl = await renderInBrowser({
                        videoUrl: serverUrl,
                        durationInSeconds: clipDuration,
                        subtitles: null,
                        hook: remaining.hook,
                        effects: remaining.effects,
                    });
                    setCurrentVideoUrl(blobUrl);
                } else {
                    setCurrentVideoUrl(serverUrl);
                }
                if (videoRef.current) videoRef.current.load();
                setShowSubtitleModal(false);
            }
        } catch (e) {
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsSubtitling(false);
        }
    };

    // Output format and/or cinematic look for this clip. The server re-renders
    // from the source video (a format change) or re-grades the current render
    // and re-applies any captions the clip already had. Only the fields the
    // user changed travel; the modal decides which those are.
    const handleLook = async (options) => {
        setIsLooking(true);
        setEditError(null);
        try {
            const body = { job_id: jobId, clip_index: index, reapply_captions: true };
            if (options.outputFormat != null) body.output_format = options.outputFormat;
            if (options.cinematic != null) body.cinematic = options.cinematic;

            const res = await apiFetch('/api/clip/look', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });

            if (!res.ok) {
                // 409 = the source video is gone (retention sweep), so a format
                // change has nothing to re-render from. Its detail says so.
                const errText = await res.text();
                let detail = errText;
                try { detail = JSON.parse(errText).detail || errText; } catch { /* plain text */ }
                throw new Error(detail);
            }
            const data = await res.json();
            if (data.new_video_url) {
                setServerVideoFile(data.new_video_url.split('/').pop());
                setCurrentVideoUrl(getApiUrl(data.new_video_url));
                if (options.outputFormat) setOutputFormat(options.outputFormat);
                if (videoRef.current) videoRef.current.load();
                setShowLookModal(false);
            }
        } catch (e) {
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsLooking(false);
        }
    };

    // Background music for this clip. `spec` is { track, volume_db, duck,
    // start } or null to remove it; the server mixes it under the current
    // render (ducked under speech) and answers with the new file.
    const handleMusic = async (spec) => {
        setIsApplyingMusic(true);
        setEditError(null);
        try {
            const res = await apiFetch('/api/clip/music', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ job_id: jobId, clip_index: index, music: spec }),
            });

            if (!res.ok) {
                const errText = await res.text();
                let detail = errText;
                try { detail = JSON.parse(errText).detail || errText; } catch { /* plain text */ }
                throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
            }
            const data = await res.json();
            if (data.new_video_url) {
                setServerVideoFile(data.new_video_url.split('/').pop());
                setCurrentVideoUrl(getApiUrl(data.new_video_url));
                setMusic(data.music ?? spec ?? null);
                if (videoRef.current) videoRef.current.load();
                setShowMusicModal(false);
            }
        } catch (e) {
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsApplyingMusic(false);
        }
    };

    // Logo and text overlays for this clip. `items` is the full list (every
    // geometry a fraction of the frame); an empty list removes the layer. The
    // server composites it over the current render and answers with the new
    // file plus the list it kept.
    const handleOverlays = async (items) => {
        setIsApplyingOverlays(true);
        setEditError(null);
        try {
            const res = await apiFetch('/api/clip/overlays', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ job_id: jobId, clip_index: index, overlays: items }),
            });

            if (!res.ok) {
                const errText = await res.text();
                let detail = errText;
                try { detail = JSON.parse(errText).detail || errText; } catch { /* plain text */ }
                throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
            }
            const data = await res.json();
            if (data.new_video_url) {
                setServerVideoFile(data.new_video_url.split('/').pop());
                setCurrentVideoUrl(getApiUrl(data.new_video_url));
                setOverlays(Array.isArray(data.overlays) ? data.overlays : items);
                if (videoRef.current) videoRef.current.load();
                setShowOverlayEditor(false);
            }
        } catch (e) {
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsApplyingOverlays(false);
        }
    };

    const handleTranslate = async (options) => {
        console.log('[Translate] Starting translation with options:', options);
        setIsTranslating(true);
        setEditError(null);
        try {
            const apiKey = elevenLabsKey;
            console.log('[Translate] API Key available:', !!apiKey);

            if (!apiKey) {
                throw new Error("ElevenLabs API Key is missing. Please set it in Settings.");
            }

            const requestBody = {
                job_id: jobId,
                clip_index: index,
                target_language: options.targetLanguage,
                input_filename: serverVideoFile
            };
            console.log('[Translate] Request body:', requestBody);
            console.log('[Translate] Sending request to /api/translate');

            const res = await apiFetch('/api/translate', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-ElevenLabs-Key': apiKey
                },
                body: JSON.stringify(requestBody)
            });

            console.log('[Translate] Response status:', res.status);

            if (!res.ok) {
                const errText = await res.text();
                console.error('[Translate] Error response:', errText);
                try {
                    const jsonErr = JSON.parse(errText);
                    throw new Error(jsonErr.detail || errText);
                } catch (e) {
                    if (e.message !== errText) throw e;
                    throw new Error(errText);
                }
            }

            const data = await res.json();
            console.log('[Translate] Success response:', data);
            if (data.new_video_url) {
                setCurrentVideoUrl(getApiUrl(data.new_video_url));
                setServerVideoFile(data.new_video_url.split('/').pop());
                if (videoRef.current) {
                    videoRef.current.load();
                }
                setShowTranslateModal(false);
            }

        } catch (e) {
            console.error('[Translate] Exception:', e);
            setEditError(e.message);
            setTimeout(() => setEditError(null), 5000);
        } finally {
            setIsTranslating(false);
        }
    };

    // Managed (cloud plan/trial) users post with the server-side key — no BYOK needed
    const canPost = isManaged || (uploadPostKey && uploadUserId);

    const handlePost = async () => {
        if (!canPost) {
            setPostResult({ success: false, msg: "Missing API Key or User ID." });
            return;
        }

        if (noAccountsConnected) {
            setPostResult({ success: false, msg: "Connect a social account first." });
            return;
        }

        const selectedPlatforms = Object.keys(platforms).filter(k => platforms[k]);
        if (selectedPlatforms.length === 0) {
            setPostResult({ success: false, msg: "Select at least one platform." });
            return;
        }

        if (isScheduling && !scheduleDate) {
            setPostResult({ success: false, msg: "Please select a date and time." });
            return;
        }

        setPosting(true);
        setPostResult(null);

        try {
            const payload = {
                job_id: jobId,
                clip_index: index,
                api_key: uploadPostKey,
                user_id: uploadUserId,
                platforms: selectedPlatforms,
                title: postTitle,
                description: postDescription
            };

            if (isScheduling && scheduleDate) {
                // Convert to ISO-8601
                payload.scheduled_date = new Date(scheduleDate).toISOString();
                // Optional: pass timezone if needed, backend defaults to UTC or we can send user's timezone
                payload.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
            }

            const res = await apiFetch('/api/social/post', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (!res.ok) {
                const errText = await res.text();
                try {
                    const jsonErr = JSON.parse(errText);
                    throw new Error(jsonErr.detail || errText);
                } catch (e) {
                    throw new Error(errText);
                }
            }

            setPostResult({ success: true, msg: isScheduling ? "Scheduled successfully!" : "Posted successfully!" });
            setTimeout(() => {
                setShowModal(false);
                setPostResult(null);
            }, 3000);

        } catch (e) {
            setPostResult({ success: false, msg: `Failed: ${e.message}` });
        } finally {
            setPosting(false);
        }
    };

    // Browser-rendered previews (Remotion) live in a blob: URL that exists only
    // in this tab, so they always win over the durable copy.
    const playbackUrl = (durableSrc && !durableFailed && !String(currentVideoUrl || '').startsWith('blob:'))
        ? durableSrc
        : currentVideoUrl;

    const durationReadout = formatDuration(clip);
    // Track name for the badge: the spec stores the file, so strip the
    // extension and keep it short enough not to wrap the badge row.
    const musicLabel = music?.track
        ? (() => {
            const name = String(music.track).replace(/\.[a-z0-9]{2,5}$/i, '');
            return name.length > 18 ? `${name.slice(0, 17)}…` : name;
        })()
        : null;

    // The actions grid is 2 columns on phones and 3 from md up. Download is
    // the last cell, so it stretches to fill whatever the row is short by.
    // Written out in full so the Tailwind scanner finds every class.
    const actionCount = 8 + (onEditClip ? 1 : 0) + (onReframeClip ? 1 : 0);
    const downloadSpan = [
        actionCount % 2 === 1 ? 'col-span-2' : 'col-span-1',
        { 0: 'md:col-span-1', 1: 'md:col-span-3', 2: 'md:col-span-2' }[actionCount % 3],
    ].join(' ');

    const gradeLabel = clip.cinematic?.color_grade && clip.cinematic.color_grade !== 'none'
        ? (GRADE_LABELS[clip.cinematic.color_grade] || clip.cinematic.color_grade)
        : null;

    return (
        <div className="card overflow-hidden flex flex-col md:flex-row group hover:border-rule2 transition-colors animate-fade md:min-h-[420px]" style={{ animationDelay: `${index * 0.1}s` }}>
            {/* Left: Video Preview — a column in the clip's aspect on a phone, a
                fixed width matching the card height from md up (wider for 1:1
                and 16:9 so the preview is not a sliver). */}
            {/* A full-width 9:16 preview on a phone is ~640px tall on its own,
                which pushed the title, captions and every action off-screen.
                Capping the height and centring keeps the whole card scannable
                without letterboxing the clip. */}
            <div className={`w-full max-w-[calc(64vh*0.5625)] md:max-w-none mx-auto md:mx-0 bg-black relative shrink-0 md:aspect-auto group/video ${PREVIEW_ASPECT[outputFormat] || PREVIEW_ASPECT.vertical}`}>
                <video
                    ref={videoRef}
                    src={playbackUrl}
                    controls
                    className="w-full h-full object-contain"
                    playsInline
                    onLoadedMetadata={(e) => {
                        if (e.target.videoWidth) setResolution(`${e.target.videoWidth}×${e.target.videoHeight}`);
                    }}
                    onError={() => {
                        // The durable copy is unreachable (signature expired after an
                        // hour on an idle tab, object purged) → serve from the API for
                        // the rest of this card's life.
                        if (playbackUrl === durableSrc) {
                            setDurableFailed(true);
                            return;
                        }
                        // Local /videos/ file gone (e.g. cleaned up after a reload) →
                        // fall back to the durable R2 copy for managed users. If the
                        // durable URL hasn't loaded yet, the effect above retries.
                        if (durable?.url && currentVideoUrl !== durable.url) setCurrentVideoUrl(durable.url);
                        else setVideoErrored(true);
                    }}
                    onPlay={() => {
                        setHasPlayed(true);
                        const currentTime = videoRef.current ? videoRef.current.currentTime : 0;
                        onPlay && onPlay(clip.start + currentTime);
                    }}
                    onPause={() => onPause && onPause()}
                    onEnded={() => {
                        if (videoRef.current) {
                            videoRef.current.currentTime = 0;
                            videoRef.current.play();
                        }
                    }}
                />
                <div className="absolute top-3 left-3 flex gap-2">
                    {/* Stays the clip's own number, not its rank: the cards are
                        ordered by score, but this is what the downloaded file
                        is called (clip-N.mp4) and what every api call indexes. */}
                    <span className="bg-black/70 text-ink font-mono text-micro uppercase px-2 py-1 rounded-full">
                        Clip {index + 1}
                    </span>
                    {/* A bare number on a thumbnail reads as a duration, a
                        position, anything — it has to name itself and carry
                        its scale, or it is decoration. */}
                    {Number.isFinite(clip.predicted_score) && (
                        <span
                            className="bg-black/70 font-mono text-micro uppercase px-2 py-1 rounded-full flex items-center gap-1"
                            title="openshorts' prediction of how well this clip will perform, from 0 to 100"
                        >
                            <TrendingUp size={11} className="shrink-0 text-muted" />
                            <span className="text-muted">viral</span>
                            <b className={
                                clip.predicted_score >= 80 ? 'text-ok'
                                    : clip.predicted_score >= 65 ? 'text-brass'
                                        : 'text-ink2'
                            }>
                                {clip.predicted_score}
                            </b>
                            <span className="text-muted">/100</span>
                        </span>
                    )}
                </div>

                {/* Auto Edit Overlay if Processing */}
                {isEditing && (
                    <div className="absolute inset-0 bg-black/70 flex flex-col items-center justify-center z-10 p-4 text-center">
                        <Loader2 size={28} className="text-brass animate-spin mb-3" />
                        <span className="text-xs text-ink lowercase">ai magic in progress…</span>
                        <span className="readout mt-1.5">APPLYING VIRAL EDITS · ZOOMS</span>
                    </div>
                )}
            </div>

            {/* Right: Content & Details */}
            <div className="flex-1 p-4 md:p-5 flex flex-col overflow-hidden min-w-0">
                <div className="mb-4">
                    <h3 className="text-base font-medium text-ink leading-tight line-clamp-2 mb-2 break-words" title={clip.video_title_for_youtube_short}>
                        {clip.video_title_for_youtube_short || "Viral Clip Generated"}
                    </h3>
                    <div className="flex flex-wrap gap-1.5">
                        {durationReadout && <span className="readout bg-paper3 px-2 py-0.5 rounded-full shrink-0">{durationReadout}</span>}
                        {resolution && <span className="readout bg-paper3 px-2 py-0.5 rounded-full shrink-0">{resolution}</span>}
                        <span className="readout bg-paper3 px-2 py-0.5 rounded-full shrink-0" title="output format">{FORMAT_LABELS[outputFormat] || FORMAT_LABELS.vertical}</span>
                        {gradeLabel && <span className="readout bg-paper3 px-2 py-0.5 rounded-full shrink-0" title="cinematic look">{gradeLabel}</span>}
                        {musicLabel && (
                            <span className="readout bg-paper3 px-2 py-0.5 rounded-full shrink-0 flex items-center gap-1" title={`background music: ${music.track}`}>
                                <Music size={10} className="shrink-0 text-muted" />
                                {musicLabel}
                            </span>
                        )}
                        {overlays.length > 0 && (
                            <span className="readout bg-paper3 px-2 py-0.5 rounded-full shrink-0 flex items-center gap-1" title="logo and text overlays burned into this clip">
                                <ImagePlus size={10} className="shrink-0 text-muted" />
                                {overlays.length} {overlays.length === 1 ? 'overlay' : 'overlays'}
                            </span>
                        )}
                        <span className="readout bg-paper3 px-2 py-0.5 rounded-full shrink-0">#shorts</span>
                        <span className="readout bg-paper3 px-2 py-0.5 rounded-full shrink-0">#viral</span>
                    </div>
                </div>

                {/* Descriptions (compact) — full text lives in the modal */}
                <div className="flex-1 min-h-0 space-y-2 mb-4">
                    <div className="bg-paper rounded-input px-3 py-2 border border-rule flex items-center gap-2 min-w-0">
                        <span className="eyebrow shrink-0">YOUTUBE</span>
                        <p className="text-xs text-ink2 truncate flex-1 min-w-0">
                            {clip.video_title_for_youtube_short || "Viral Short Video"}
                        </p>
                        <button
                            onClick={() => handleCopy('youtube', clip.video_title_for_youtube_short || "Viral Short Video")}
                            aria-label="copy youtube title"
                            className="p-1 rounded-full text-muted hover:text-brass transition-colors shrink-0"
                        >
                            {copied === 'youtube' ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
                        </button>
                    </div>

                    <div className="bg-paper rounded-input px-3 py-2 border border-rule flex items-center gap-2 min-w-0">
                        <span className="eyebrow shrink-0">TIKTOK · IG</span>
                        <p className="text-xs text-ink2 truncate flex-1 min-w-0">
                            {clip.video_description_for_tiktok || clip.video_description_for_instagram}
                        </p>
                        <button
                            onClick={() => handleCopy('caption', clip.video_description_for_tiktok || clip.video_description_for_instagram)}
                            aria-label="copy caption"
                            className="p-1 rounded-full text-muted hover:text-brass transition-colors shrink-0"
                        >
                            {copied === 'caption' ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
                        </button>
                    </div>

                    <button
                        onClick={() => setShowDescModal(true)}
                        className="w-full flex items-center justify-center gap-2 py-2 rounded-input border border-dashed border-rule text-xs lowercase text-muted hover:text-brass hover:border-rule2 transition-colors"
                    >
                        <FileText size={14} /> view descriptions
                    </button>
                </div>

                {/* Error Message */}
                {editError && (
                    <div className="mb-3 px-3 py-2 rounded-input text-xs text-danger bg-[color-mix(in_oklab,var(--color-danger)_10%,transparent)] flex items-center gap-2">
                        <AlertCircle size={14} className="shrink-0" />
                        {editError}
                    </div>
                )}

                {/* Actions Footer */}
                <div className="grid grid-cols-2 md:grid-cols-3 gap-2 mt-auto pt-4 border-t border-rule">
                    {onEditClip && (
                        <button
                            onClick={() => onEditClip(index)}
                            className={QUIET_BTN}
                        >
                            <Scissors size={16} className="text-muted group-hover:text-brass transition-colors shrink-0" />
                            edit clip
                        </button>
                    )}

                    {onReframeClip && (
                        <button
                            onClick={() => onReframeClip(index)}
                            className={QUIET_BTN}
                        >
                            <Crosshair size={16} className="text-muted group-hover:text-brass transition-colors shrink-0" />
                            reframing
                        </button>
                    )}

                    <button
                        onClick={handleAutoEdit}
                        disabled={isEditing}
                        className={QUIET_BTN}
                    >
                        {isEditing ? <Loader2 size={16} className="animate-spin text-brass shrink-0" /> : <Wand2 size={16} className="text-muted group-hover:text-brass transition-colors shrink-0" />}
                        {isEditing ? 'editing…' : 'auto edit'}
                    </button>

                    <button
                        onClick={() => setShowSubtitleModal(true)}
                        disabled={isSubtitling}
                        className={QUIET_BTN}
                    >
                        {isSubtitling ? <Loader2 size={16} className="animate-spin text-brass shrink-0" /> : <Type size={16} className="text-muted group-hover:text-brass transition-colors shrink-0" />}
                        {isSubtitling ? 'adding…' : 'subtitles'}
                    </button>

                    <button
                        onClick={() => setShowLookModal(true)}
                        disabled={isLooking}
                        className={QUIET_BTN}
                    >
                        {isLooking ? <Loader2 size={16} className="animate-spin text-brass shrink-0" /> : <Clapperboard size={16} className="text-muted group-hover:text-brass transition-colors shrink-0" />}
                        {isLooking ? 'applying…' : 'format & look'}
                    </button>

                    <button
                        onClick={() => setShowMusicModal(true)}
                        disabled={isApplyingMusic}
                        className={QUIET_BTN}
                    >
                        {isApplyingMusic ? <Loader2 size={16} className="animate-spin text-brass shrink-0" /> : <Music size={16} className="text-muted group-hover:text-brass transition-colors shrink-0" />}
                        {isApplyingMusic ? 'applying…' : 'music'}
                    </button>

                    <button
                        onClick={() => setShowOverlayEditor(true)}
                        disabled={isApplyingOverlays}
                        className={QUIET_BTN}
                    >
                        {isApplyingOverlays ? <Loader2 size={16} className="animate-spin text-brass shrink-0" /> : <ImagePlus size={16} className="text-muted group-hover:text-brass transition-colors shrink-0" />}
                        {isApplyingOverlays ? 'applying…' : 'logo & text'}
                    </button>

                    <button
                        onClick={() => setShowTranslateModal(true)}
                        disabled={isTranslating}
                        className={QUIET_BTN}
                    >
                        {isTranslating ? <Loader2 size={16} className="animate-spin text-brass shrink-0" /> : <Languages size={16} className="text-muted group-hover:text-brass transition-colors shrink-0" />}
                        {isTranslating ? 'translating…' : 'dub voice'}
                    </button>

                    <button
                        onClick={() => setShowModal(true)}
                        className="btn-primary flex-col gap-1 py-2.5 sm:py-2 px-1 text-[11px] leading-none rounded-input whitespace-nowrap"
                    >
                        <Share2 size={16} className="shrink-0" /> post
                    </button>
                    <button
                        onClick={(e) => {
                            e.preventDefault();
                            // Free clips are watermarked — surface the upsell once
                            // before the first download, then get out of the way.
                            if (plan === 'free' && !watermarkNoticeDismissed()) {
                                setShowWatermarkModal(true);
                                return;
                            }
                            downloadClip();
                        }}
                        className={`${QUIET_BTN} ${downloadSpan}`}
                    >
                        <Download size={16} className="text-muted group-hover:text-brass transition-colors shrink-0" />
                        {downloadPct === null ? 'download' : `downloading ${downloadPct}%`}
                    </button>
                </div>
            </div>

            {/* Descriptions Modal */}
            <Modal
                isOpen={showDescModal}
                onClose={() => setShowDescModal(false)}
                eyebrow="GENERATED COPY"
                title="descriptions"
                size="md"
            >
                <div className="space-y-4">
                    <div>
                        <div className="flex items-center justify-between gap-2 mb-1.5">
                            <label className="eyebrow">YOUTUBE TITLE</label>
                            <button
                                onClick={() => handleCopy('youtube', clip.video_title_for_youtube_short || "Viral Short Video")}
                                aria-label="copy youtube title"
                                className="p-1 rounded-full text-muted hover:text-brass transition-colors shrink-0"
                            >
                                {copied === 'youtube' ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
                            </button>
                        </div>
                        <p className="text-sm text-ink2 select-all break-words bg-paper rounded-input p-3 border border-rule">
                            {clip.video_title_for_youtube_short || "Viral Short Video"}
                        </p>
                    </div>

                    <div>
                        <div className="flex items-center justify-between gap-2 mb-1.5">
                            <label className="eyebrow">TIKTOK · IG CAPTION</label>
                            <button
                                onClick={() => handleCopy('caption', clip.video_description_for_tiktok || clip.video_description_for_instagram)}
                                aria-label="copy caption"
                                className="p-1 rounded-full text-muted hover:text-brass transition-colors shrink-0"
                            >
                                {copied === 'caption' ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
                            </button>
                        </div>
                        <p className="text-sm text-ink2 select-all break-words bg-paper rounded-input p-3 border border-rule whitespace-pre-wrap">
                            {clip.video_description_for_tiktok || clip.video_description_for_instagram}
                        </p>
                    </div>
                </div>
            </Modal>

            {/* Post Modal */}
            <Modal
                isOpen={showModal}
                onClose={() => setShowModal(false)}
                eyebrow="PUBLISH"
                title="post clip"
                size="md"
                footer={
                    noAccountsConnected ? (
                        <button onClick={handleConnectAccounts} className="btn-primary w-full">
                            <Link2 size={16} /> connect accounts
                        </button>
                    ) : (
                        <button
                            onClick={handlePost}
                            disabled={posting || !canPost}
                            className="btn-primary w-full"
                        >
                            {posting ? <><Loader2 size={16} className="animate-spin" /> {isScheduling ? 'scheduling…' : 'publishing…'}</> : <><Share2 size={16} /> {isScheduling ? 'schedule post' : 'publish now'}</>}
                        </button>
                    )
                }
            >
                {!canPost && (
                    <div className="mb-4 px-3 py-2 rounded-input text-xs text-warn bg-[color-mix(in_oklab,var(--color-warn)_10%,transparent)] flex items-start gap-2">
                        <AlertCircle size={14} className="mt-0.5 shrink-0" />
                        <div className="lowercase">configure api key in settings first.</div>
                    </div>
                )}

                {noAccountsConnected && (
                    <div className="mb-4 px-3 py-2 rounded-input text-xs text-warn bg-[color-mix(in_oklab,var(--color-warn)_10%,transparent)] flex items-start gap-2">
                        <AlertCircle size={14} className="mt-0.5 shrink-0" />
                        <div className="lowercase">no social accounts connected yet — link tiktok, instagram or youtube to publish this clip.</div>
                    </div>
                )}

                {/* Both the title/description fields below and the schedule
                    button are downstream of this: on a tiktok draft neither
                    travels. See TikTokDraftNotice. */}
                {platforms.tiktok && <TikTokDraftNotice />}

                <div className="space-y-4">
                    {/* Title & Description */}
                    <div>
                        <label className="eyebrow block mb-1.5">TITLE</label>
                        <input
                            type="text"
                            value={postTitle}
                            onChange={(e) => setPostTitle(e.target.value)}
                            className="input-field"
                            placeholder="enter a catchy title…"
                        />
                    </div>

                    <div>
                        <label className="eyebrow block mb-1.5">CAPTION</label>
                        <textarea
                            value={postDescription}
                            onChange={(e) => setPostDescription(e.target.value)}
                            rows={4}
                            className="input-field resize-none"
                            placeholder="write a caption for your post…"
                        />
                    </div>

                    {/* Scheduling */}
                    <div className="p-3 bg-paper rounded-input border border-rule">
                        <label className="flex items-center justify-between cursor-pointer">
                            <span className="flex items-center gap-2 text-sm text-ink2 lowercase">
                                <Calendar size={16} className={isScheduling ? 'text-brass' : 'text-muted'} /> schedule post
                            </span>
                            <input
                                type="checkbox"
                                checked={isScheduling}
                                onChange={(e) => setIsScheduling(e.target.checked)}
                                className="w-4 h-4 accent-brass cursor-pointer"
                            />
                        </label>

                        {isScheduling && (
                            <div className="mt-3 animate-fade">
                                <label className="eyebrow block mb-1.5">DATE · TIME</label>
                                <input
                                    type="datetime-local"
                                    value={scheduleDate}
                                    onChange={(e) => setScheduleDate(e.target.value)}
                                    className="input-field [color-scheme:dark]"
                                />
                            </div>
                        )}
                    </div>

                    {/* Platforms */}
                    <div>
                        <label className="eyebrow block mb-2">PLATFORMS</label>
                        <SegmentedControl
                            multi
                            columns={3}
                            options={platformOptions}
                            value={Object.keys(platforms).filter(k => platforms[k])}
                            onChange={(arr) => setPlatforms({
                                tiktok: arr.includes('tiktok'),
                                instagram: arr.includes('instagram'),
                                youtube: arr.includes('youtube'),
                            })}
                        />
                    </div>

                    {postResult && (
                        <div className={postResult.success ? 'badge-ok' : 'badge-danger'}>
                            {postResult.success ? <Check size={12} className="shrink-0" /> : <AlertCircle size={12} className="shrink-0" />}
                            {postResult.msg}
                        </div>
                    )}
                </div>
            </Modal>

            <SubtitleModal
                isOpen={showSubtitleModal}
                onClose={() => setShowSubtitleModal(false)}
                onGenerate={handleSubtitle}
                onApplyAll={onBulkSubtitle ? async (options) => {
                    await onBulkSubtitle(options);
                    setShowSubtitleModal(false);
                } : undefined}
                onRemove={handleRemoveSubtitles}
                bulkCount={clipCount}
                bulkProgress={bulkProgress}
                isProcessing={isSubtitling || (bulkProgress?.running ?? false)}
                videoUrl={originalVideoUrl}
                jobId={jobId}
                clipIndex={index}
                existingHook={activeLayers.hook}
            />

            <LookModal
                isOpen={showLookModal}
                onClose={() => setShowLookModal(false)}
                clip={{ ...clip, output_format: outputFormat }}
                clipCount={clipCount}
                bulkProgress={bulkLookProgress}
                isProcessing={isLooking || (bulkLookProgress?.running ?? false)}
                onApply={handleLook}
                onApplyAll={onBulkLook ? async (options) => {
                    await onBulkLook(options);
                    setShowLookModal(false);
                } : undefined}
            />

            <MusicModal
                isOpen={showMusicModal}
                onClose={() => setShowMusicModal(false)}
                clip={{ ...clip, music }}
                clipCount={clipCount}
                bulkProgress={bulkMusicProgress}
                isProcessing={isApplyingMusic || (bulkMusicProgress?.running ?? false)}
                onApply={handleMusic}
                onApplyAll={onBulkMusic ? async (spec) => {
                    await onBulkMusic(spec);
                    setShowMusicModal(false);
                } : undefined}
                videoUrl={currentVideoUrl}
            />

            <OverlayEditor
                isOpen={showOverlayEditor}
                onClose={() => setShowOverlayEditor(false)}
                clip={{ ...clip, output_format: outputFormat, overlays }}
                clipCount={clipCount}
                bulkProgress={bulkOverlaysProgress}
                isProcessing={isApplyingOverlays || (bulkOverlaysProgress?.running ?? false)}
                onApply={handleOverlays}
                onApplyAll={onBulkOverlays ? async (items) => {
                    await onBulkOverlays(items);
                    setShowOverlayEditor(false);
                } : undefined}
                videoUrl={overlayStageUrl}
            />

            <TranslateModal
                isOpen={showTranslateModal}
                onClose={() => setShowTranslateModal(false)}
                onTranslate={handleTranslate}
                isProcessing={isTranslating}
                videoUrl={currentVideoUrl}
                hasApiKey={!!elevenLabsKey}
            />

            {showWatermarkModal && (
                <WatermarkModal
                    onClose={() => setShowWatermarkModal(false)}
                    onContinue={downloadClip}
                />
            )}

        </div>
    );
}
