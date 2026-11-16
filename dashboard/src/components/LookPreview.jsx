import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { apiFetch } from '../lib/api';

/**
 * One graded frame of a clip, for the format & look modal. Each change of
 * look, frame position or the original/with-look switch asks
 * POST /api/clip/look/preview for a single JPEG (about a third of a second
 * server side), debounced so stepping through grades or dragging the slider
 * does not queue a render per step. The "original" frame is the same request
 * with no look, from the same bare render the real apply grades.
 */
export default function LookPreview({ jobId, clipIndex, look, isOpen }) {
    const [position, setPosition] = useState(0.35);
    const [showOriginal, setShowOriginal] = useState(false);
    const [frame, setFrame] = useState({ url: null, loading: false, error: '' });

    const lookKey = JSON.stringify(showOriginal ? {} : look);

    useEffect(() => {
        if (!isOpen || !jobId || clipIndex === undefined || clipIndex === null) return undefined;
        const controller = new AbortController();
        setFrame((f) => ({ ...f, loading: true, error: '' }));
        const timer = setTimeout(async () => {
            try {
                const res = await apiFetch('/api/clip/look/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        job_id: jobId,
                        clip_index: clipIndex,
                        cinematic: JSON.parse(lookKey),
                        position,
                    }),
                    signal: controller.signal,
                });
                if (!res.ok) throw new Error(`preview ${res.status}`);
                const url = URL.createObjectURL(await res.blob());
                setFrame((f) => {
                    if (f.url) URL.revokeObjectURL(f.url);
                    return { url, loading: false, error: '' };
                });
            } catch (err) {
                if (err.name === 'AbortError') return;
                setFrame((f) => ({ ...f, loading: false, error: 'preview unavailable' }));
            }
        }, 250);
        return () => {
            clearTimeout(timer);
            controller.abort();
        };
    }, [isOpen, jobId, clipIndex, lookKey, position]);

    // Drop the frame when the modal closes so a reopen never flashes a stale
    // grade.
    useEffect(() => {
        if (isOpen) return;
        setShowOriginal(false);
        setFrame((f) => {
            if (f.url) URL.revokeObjectURL(f.url);
            return { url: null, loading: false, error: '' };
        });
    }, [isOpen]);

    const switchClass = (active) => `text-[11px] py-1 rounded-input border transition-colors ${
        active ? 'border-brass text-ink' : 'border-rule text-muted hover:text-ink2'}`;

    return (
        <div className="space-y-2">
            <div className="relative rounded-input overflow-hidden bg-black flex items-center justify-center aspect-[9/16] max-h-[52vh] mx-auto w-full">
                {frame.url && (
                    <img
                        src={frame.url}
                        alt={showOriginal ? 'original frame' : 'frame with the selected look'}
                        className={`w-full h-full object-contain transition-opacity ${frame.loading ? 'opacity-60' : 'opacity-100'}`}
                    />
                )}
                {!frame.url && !frame.error && <Loader2 size={20} className="animate-spin text-muted" />}
                {frame.error && !frame.url && (
                    <span className="text-[11px] text-muted px-3 text-center">{frame.error}</span>
                )}
                {frame.url && frame.loading && (
                    <Loader2 size={16} className="animate-spin text-white absolute top-2 right-2" />
                )}
                <span className="absolute bottom-2 left-2 bg-black/60 text-white px-2 py-0.5 rounded-full text-[10px]">
                    {showOriginal ? 'original' : 'preview'}
                </span>
            </div>
            <div className="grid grid-cols-2 gap-1">
                <button type="button" onClick={() => setShowOriginal(false)} className={switchClass(!showOriginal)} aria-pressed={!showOriginal}>
                    with look
                </button>
                <button type="button" onClick={() => setShowOriginal(true)} className={switchClass(showOriginal)} aria-pressed={showOriginal}>
                    original
                </button>
            </div>
            <label className="flex flex-col gap-1 text-[11px] text-muted">
                frame
                <input
                    type="range"
                    min="0.02"
                    max="0.98"
                    step="0.01"
                    value={position}
                    onChange={(e) => setPosition(parseFloat(e.target.value))}
                    className="w-full accent-[var(--color-accent)]"
                    aria-label="preview frame position"
                />
            </label>
            <p className="text-[10px] leading-relaxed text-muted">
                The look on the clip's current framing. A format change shows after applying.
            </p>
        </div>
    );
}
