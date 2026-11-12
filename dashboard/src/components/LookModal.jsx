import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import Modal from './ui/Modal';
import SegmentedControl from './ui/SegmentedControl';
import { FALLBACK_LOOK_OPTIONS, fetchLookOptions } from '../lib/lookOptions';

/**
 * Per-clip output format + cinematic look. Clips render plain 9:16 with no
 * look at all; this is where the user picks both, for one clip or for every
 * clip of the job. Only what changed travels: `outputFormat` when it differs
 * from the clip's current format, `cinematic` when any of its keys differs
 * from the clip's current look. Grades, sharpening levels and LUTs come from
 * GET /api/look-options.
 */

// Not exported: the react-refresh lint rule wants component files to export
// only components. ResultCard keeps its own copy of the two label maps.
const FORMAT_OPTIONS = [
    { value: 'vertical', label: '9:16', hint: 'Shorts · Reels · TikTok' },
    { value: 'square', label: '1:1', hint: 'Feed posts' },
    { value: 'horizontal', label: '16:9', hint: 'Keep landscape · YouTube' },
];


const TOGGLES = [
    { key: 'glow', label: 'glow' },
    { key: 'grain', label: 'grain' },
    { key: 'vignette', label: 'vignette' },
    { key: 'letterbox', label: 'cinema bars' },
    { key: 'bottom_gradient', label: 'caption scrim' },
];

// The clip's stored `cinematic` may be null or partial; normalise to the
// keys the server understands so comparisons are key-by-key.
function normaliseLook(cinematic) {
    return {
        color_grade: cinematic?.color_grade || 'none',
        sharpen: cinematic?.sharpen || 'none',
        lut: cinematic?.lut || 'none',
        glow: !!cinematic?.glow,
        grain: !!cinematic?.grain,
        vignette: !!cinematic?.vignette,
        letterbox: !!cinematic?.letterbox,
        bottom_gradient: !!cinematic?.bottom_gradient,
    };
}

function isPlainLook(look) {
    return look.color_grade === 'none' && look.sharpen === 'none' && look.lut === 'none' && !look.glow && !look.grain && !look.vignette && !look.letterbox && !look.bottom_gradient;
}

function sameLook(a, b) {
    return Object.keys(a).every((k) => a[k] === b[k]);
}

export default function LookModal({ isOpen, onClose, clip, clipCount = 1, bulkProgress, isProcessing, onApply, onApplyAll }) {
    const currentFormat = clip?.output_format || 'vertical';
    const currentLook = normaliseLook(clip?.cinematic);

    const [format, setFormat] = useState(currentFormat);
    const [look, setLook] = useState(currentLook);
    const [options, setOptions] = useState(FALLBACK_LOOK_OPTIONS);

    useEffect(() => {
        if (!isOpen) return undefined;
        let alive = true;
        fetchLookOptions().then((o) => { if (alive) setOptions(o); }).catch(() => {});
        return () => { alive = false; };
    }, [isOpen]);

    // Re-seed from the clip each time the modal opens (or the clip is
    // re-rendered under it) so it never shows a stale choice.
    useEffect(() => {
        if (!isOpen) return;
        setFormat(clip?.output_format || 'vertical');
        setLook(normaliseLook(clip?.cinematic));
    }, [isOpen, clip?.output_format, clip?.cinematic]);

    const formatChanged = format !== currentFormat;
    const lookChanged = !sameLook(look, currentLook);
    const nothingChanged = !formatChanged && !lookChanged;

    const buildOptions = () => {
        const options = {};
        if (formatChanged) options.outputFormat = format;
        if (lookChanged) {
            // Everything back to none/off is a removal: it only needs sending
            // when the clip had a look to remove. Otherwise the field is omitted.
            if (isPlainLook(look)) {
                if (!isPlainLook(currentLook)) options.cinematic = { color_grade: 'none' };
            } else {
                options.cinematic = { ...look };
            }
        }
        return options;
    };

    const bulkRunning = !!bulkProgress?.running;
    const busy = isProcessing || bulkRunning;
    const disabled = busy || nothingChanged;

    return (
        <Modal
            isOpen={isOpen}
            onClose={onClose}
            size="md"
            eyebrow="FORMAT & LOOK"
            title="format & look"
            footer={
                <div className="space-y-2">
                    <div className="flex gap-2">
                        <button type="button" onClick={onClose} className="btn-ghost" disabled={busy}>
                            cancel
                        </button>
                        <button
                            type="button"
                            onClick={() => onApply(buildOptions())}
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
                            onClick={() => onApplyAll(buildOptions())}
                            disabled={disabled}
                            className="btn-ghost w-full flex items-center justify-center gap-2"
                        >
                            {bulkRunning
                                ? <><Loader2 size={16} className="animate-spin" />applying {bulkProgress.current} / {bulkProgress.total}</>
                                : `apply to all ${clipCount} clips`}
                        </button>
                    )}
                </div>
            }
        >
            <div className="space-y-5">
                <div>
                    <p className="eyebrow mb-2">Output format</p>
                    <SegmentedControl
                        options={FORMAT_OPTIONS}
                        value={format}
                        onChange={setFormat}
                        columns={3}
                    />
                    <p className="text-[11px] leading-relaxed text-muted mt-2">
                        Changing the format re-renders the clip from the source video and takes a moment.
                    </p>
                </div>

                <div className="pt-4 border-t border-rule">
                    <span className="eyebrow">Cinematic look</span>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-2">
                        <label className="flex flex-col gap-1 text-[11px] text-muted">
                            color grade
                            <select
                                value={look.color_grade}
                                onChange={(e) => setLook({ ...look, color_grade: e.target.value })}
                                className="input-field text-xs py-1.5"
                                aria-label="cinematic color grade"
                            >
                                {options.grades.map((g) => (
                                    <option key={g.value} value={g.value}>{g.label}</option>
                                ))}
                            </select>
                        </label>
                        <label className="flex flex-col gap-1 text-[11px] text-muted">
                            sharpening
                            <select
                                value={look.sharpen}
                                onChange={(e) => setLook({ ...look, sharpen: e.target.value })}
                                className="input-field text-xs py-1.5"
                                aria-label="sharpening"
                            >
                                {options.sharpen.map((g) => (
                                    <option key={g.value} value={g.value}>{g.label}</option>
                                ))}
                            </select>
                        </label>
                        {(options.luts.length > 0 || look.lut !== 'none') && (
                            <label className="flex flex-col gap-1 text-[11px] text-muted sm:col-span-2">
                                LUT (.cube from assets/luts)
                                <select
                                    value={look.lut}
                                    onChange={(e) => setLook({ ...look, lut: e.target.value })}
                                    className="input-field text-xs py-1.5"
                                    aria-label="3D LUT"
                                >
                                    <option value="none">No LUT</option>
                                    {look.lut !== 'none' && !options.luts.includes(look.lut) && (
                                        <option value={look.lut}>{look.lut} (missing)</option>
                                    )}
                                    {options.luts.map((name) => (
                                        <option key={name} value={name}>{name}</option>
                                    ))}
                                </select>
                            </label>
                        )}
                    </div>
                    <div className="flex flex-wrap gap-x-4 gap-y-2 mt-3">
                        {TOGGLES.map((t) => (
                            <label key={t.key} className="flex items-center gap-1.5 text-xs text-ink2 cursor-pointer select-none">
                                <input
                                    type="checkbox"
                                    checked={look[t.key]}
                                    onChange={(e) => setLook({ ...look, [t.key]: e.target.checked })}
                                    className="w-4 h-4 shrink-0 accent-[var(--color-accent)] cursor-pointer"
                                />
                                {t.label}
                            </label>
                        ))}
                    </div>
                </div>

                {nothingChanged && (
                    <p className="text-[11px] text-muted">
                        This is what the clip has now. Change something to apply.
                    </p>
                )}
            </div>
        </Modal>
    );
}
