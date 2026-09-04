import { useState, useEffect, useRef } from 'react';
import { Loader2, RotateCcw, Pencil } from 'lucide-react';
import { apiFetch } from '../lib/api';
import { getApiUrl } from '../config';
import Modal from './ui/Modal';
import SegmentedControl from './ui/SegmentedControl';

/**
 * Caption studio (ClipForge port). Captions are burned per clip, after
 * generation, from `{ preset, overrides }`: the preset is one of the server's
 * looks (`GET /api/caption-styles`), the overrides are the user's tweaks on
 * top. The preview mirrors the merged config in CSS; the ASS renderer is the
 * source of truth, so the preview only has to be close, not identical.
 */

const STORAGE_KEY = 'os_caption_style_v1';

const TABS = [
    { id: 'presets', label: 'presets' },
    { id: 'text', label: 'text' },
    { id: 'color', label: 'color' },
    { id: 'effects', label: 'effects' },
    { id: 'position', label: 'position' },
];

const ANIMATION_LABELS = {
    none: 'none',
    highlight: 'highlight',
    word_reveal: 'reveal',
    one_word: 'one word',
    karaoke: 'karaoke',
};

const POSITION_OPTIONS = [
    { value: 'top', label: 'top' },
    { value: 'center', label: 'center' },
    { value: 'bottom', label: 'bottom' },
];

const BACKGROUND_OPTIONS = [
    { value: 'none', label: 'none' },
    { value: 'semi', label: 'semi' },
    { value: 'solid', label: 'solid' },
];

const LINES_OPTIONS = [
    { value: 1, label: '1 line' },
    { value: 2, label: '2 lines' },
];

// What the modal falls back to when /api/caption-styles is unreachable: the
// server's default preset, so a user can still apply captions offline-ish.
const FALLBACK_STYLES = {
    presets: [{
        id: 'bold_white', label: 'Bold White', trending: false,
        font_family: 'Roboto', bold: true, font_size: 96,
        primary_color: '#FFFFFF', highlight_color: '#FFFFFF', outline_color: '#000000',
        outline: 5, shadow: 1, position: 'bottom', karaoke: false, uppercase: true,
        animation: 'none', tracking: 0, underline: false, strikethrough: false,
        max_lines: 2, max_chars: 20, background_enabled: false, background_color: '#000000',
    }],
    themes: [],
    default: 'bold_white',
    swatches: ['#FFFFFF', '#FFD400', '#FFB020', '#FF3B30', '#FF2D78', '#27E36B', '#22D3EE', '#3B82F6', '#7C4DFF', '#000000'],
    animations: ['none', 'highlight', 'word_reveal', 'one_word', 'karaoke'],
    positions: ['top', 'center', 'bottom'],
    position_grid: [[8, 14], [50, 14], [92, 14], [8, 50], [50, 50], [92, 50], [8, 88], [50, 88], [92, 88]],
    fonts: [],
};

// ---------------------------------------------------------------------------
// Module-level cache: the style library is static per deploy, fetch it once.
// ---------------------------------------------------------------------------
let stylesCache = null;
let stylesPromise = null;
let fontsRegistered = false;

function registerFonts(fonts) {
    if (fontsRegistered || !Array.isArray(fonts) || fonts.length === 0 || typeof document === 'undefined') return;
    fontsRegistered = true;
    const css = fonts
        .filter((f) => f && f.family && f.file)
        .map((f) => `@font-face{font-family:${JSON.stringify(f.family)};src:url(${JSON.stringify(getApiUrl('/fonts/' + encodeURIComponent(f.file)))});font-display:swap;}`)
        .join('\n');
    const tag = document.createElement('style');
    tag.id = 'os-caption-fonts';
    tag.textContent = css;
    document.head.appendChild(tag);
}

function loadStyles() {
    if (stylesCache) return Promise.resolve(stylesCache);
    if (!stylesPromise) {
        stylesPromise = apiFetch('/api/caption-styles')
            .then((res) => (res.ok ? res.json() : null))
            .catch(() => null)
            .then((data) => {
                if (data && Array.isArray(data.presets) && data.presets.length > 0) {
                    stylesCache = data;
                    registerFonts(data.fonts);
                    return data;
                }
                // Not cached: the next open retries the request.
                stylesPromise = null;
                return FALLBACK_STYLES;
            });
    }
    return stylesPromise;
}

function loadSaved() {
    try {
        const s = JSON.parse(localStorage.getItem(STORAGE_KEY));
        if (s && typeof s === 'object') {
            return {
                presetId: typeof s.presetId === 'string' ? s.presetId : null,
                overrides: s.overrides && typeof s.overrides === 'object' && !Array.isArray(s.overrides) ? s.overrides : {},
            };
        }
    } catch { /* ignore */ }
    return { presetId: null, overrides: {} };
}

// ---------------------------------------------------------------------------
// CSS helpers for the preview
// ---------------------------------------------------------------------------
const hexToRgba = (hex, alpha) => {
    let h = String(hex || '#000000').replace('#', '');
    if (h.length === 3) h = h.split('').map((c) => c + c).join('');
    const n = parseInt(h, 16);
    if (Number.isNaN(n)) return `rgba(0,0,0,${alpha})`;
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
};

// A fake stroke: eight copies of the text pushed out at 45 degree steps.
const outlineShadows = (width, color) => {
    if (width <= 0) return [];
    const out = [];
    for (let i = 0; i < 8; i++) {
        const a = (i * Math.PI) / 4;
        out.push(`${(Math.cos(a) * width).toFixed(2)}px ${(Math.sin(a) * width).toFixed(2)}px 0 ${color}`);
    }
    return out;
};

const fontStack = (family) => `"${family}", "Roboto", Arial, sans-serif`;

// Greedy line packing that mirrors the renderer's max_chars / max_lines cut.
function packLines(words, maxChars, maxLines) {
    const lines = [];
    let current = [];
    let len = 0;
    words.forEach((w) => {
        const add = w.text.length + (current.length ? 1 : 0);
        if (current.length && len + add > maxChars) {
            lines.push(current);
            current = [w];
            len = w.text.length;
        } else {
            current.push(w);
            len += add;
        }
    });
    if (current.length) lines.push(current);
    return lines.slice(0, Math.max(1, maxLines));
}

const clampNum = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// Fine-position nudge in percent of the frame, clamped like the server does.
const offsetOf = (v) => clampNum(Number(v) || 0, -50, 50);
const formatOffset = (v) => (v > 0 ? `+${v}%` : `${v}%`);

// ---------------------------------------------------------------------------
// Small controls
// ---------------------------------------------------------------------------
function Slider({ label, value, min, max, step = 1, onChange, format }) {
    return (
        <div>
            <div className="flex items-center justify-between mb-1">
                <span className="readout">{label}</span>
                <span className="readout">{format ? format(value) : value}</span>
            </div>
            <input
                type="range"
                min={min}
                max={max}
                step={step}
                value={value}
                onChange={(e) => onChange(parseFloat(e.target.value))}
                className="w-full accent-[var(--color-accent)]"
            />
        </div>
    );
}

function Toggle({ label, checked, onChange }) {
    return (
        <label className="flex items-center justify-between cursor-pointer">
            <span className="readout">{label}</span>
            <span className="relative inline-flex items-center">
                <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="sr-only peer" />
                <span className="block w-8 h-4 rounded-full bg-paper3 peer-checked:bg-brass transition-colors after:content-[''] after:absolute after:top-0 after:left-0 after:h-4 after:w-4 after:rounded-full after:bg-ink after:transition-all peer-checked:after:translate-x-full" />
            </span>
        </label>
    );
}

function Chip({ active, onClick, children, style, title, className = '' }) {
    return (
        <button
            type="button"
            onClick={onClick}
            title={title}
            className={`px-2.5 py-1.5 rounded-input border text-xs transition-colors flex items-center gap-1.5
                ${active
                    ? 'border-[color:var(--color-accent)] bg-paper3 text-ink'
                    : 'border-rule2 text-muted hover:text-ink2 hover:border-[color:var(--color-accent)]'} ${className}`}
            style={style}
        >
            {children}
        </button>
    );
}

const swatchClass = (selected) =>
    `w-6 h-6 rounded-full transition-all shrink-0 ${selected
        ? 'ring-2 ring-[color:var(--color-accent)] ring-offset-2 ring-offset-[color:var(--color-paper-2)]'
        : 'ring-1 ring-[color:var(--color-rule-2)] hover:ring-[color:var(--color-accent)]'}`;

function Swatches({ label, value, swatches, onChange }) {
    const current = String(value || '').toUpperCase();
    return (
        <div>
            {label && <p className="readout mb-1.5">{label}</p>}
            <div className="flex flex-wrap items-center gap-2">
                {swatches.map((c) => (
                    <button
                        key={c}
                        type="button"
                        onClick={() => onChange(c)}
                        className={swatchClass(current === c.toUpperCase())}
                        style={{ backgroundColor: c }}
                        title={c}
                    />
                ))}
                <label
                    className="w-6 h-6 rounded-full border border-dashed border-rule2 cursor-pointer flex items-center justify-center hover:border-brass transition-colors overflow-hidden relative shrink-0"
                    title="Custom color"
                >
                    <span className="text-xs text-muted leading-none">+</span>
                    <input
                        type="color"
                        value={/^#[0-9a-fA-F]{6}$/.test(value || '') ? value : '#FFFFFF'}
                        onChange={(e) => onChange(e.target.value.toUpperCase())}
                        className="absolute inset-0 opacity-0 cursor-pointer"
                    />
                </label>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// The modal
// ---------------------------------------------------------------------------
export default function SubtitleModal({ isOpen, onClose, onGenerate, onApplyAll, onRemove, isProcessing, videoUrl, jobId, clipIndex, existingHook, bulkCount = 0, bulkProgress }) {
    const [saved] = useState(loadSaved);
    const [styles, setStyles] = useState(stylesCache);
    const [presetId, setPresetId] = useState(saved.presetId);
    const [overrides, setOverrides] = useState(saved.overrides);
    const [activeTheme, setActiveTheme] = useState(null);
    const [tab, setTab] = useState('presets');

    // Transcript (word-level) for the text editor and the preview words
    const [captions, setCaptions] = useState([]);
    const [originalCaptions, setOriginalCaptions] = useState([]);
    const [editableText, setEditableText] = useState('');
    const [captionsLoading, setCaptionsLoading] = useState(false);
    const [showTextEditor, setShowTextEditor] = useState(false);

    // Preview
    const boxRef = useRef(null);
    const [boxH, setBoxH] = useState(0);
    const [cursor, setCursor] = useState(0);
    // Mouse placement: the caption block's centre in percent of the box while
    // a drag is in flight; committed as a pin on pointerup, never before.
    const [drag, setDrag] = useState(null);
    const dragRef = useRef(null);

    // Style library: fetched once per page, then served from the module cache.
    useEffect(() => {
        if (!isOpen) return;
        let cancelled = false;
        loadStyles().then((data) => {
            if (cancelled) return;
            setStyles(data);
            setPresetId((cur) => (cur && data.presets.some((p) => p.id === cur)
                ? cur
                : (data.default || data.presets[0].id)));
        });
        return () => { cancelled = true; };
    }, [isOpen]);

    // Word-level captions for this clip
    useEffect(() => {
        if (!isOpen || !jobId || clipIndex === undefined) return;
        let cancelled = false;
        setCaptionsLoading(true);
        apiFetch(`/api/clip/${jobId}/${clipIndex}/transcript`)
            .then((res) => (res.ok ? res.json() : null))
            .then((data) => {
                if (cancelled) return;
                if (data && Array.isArray(data.captions) && data.captions.length > 0) {
                    setCaptions(data.captions);
                    setOriginalCaptions(data.captions);
                    setEditableText(data.captions.map((c) => c.text).join(' '));
                } else {
                    setCaptions([]);
                    setOriginalCaptions([]);
                    setEditableText('');
                }
            })
            .catch(() => {
                if (cancelled) return;
                setCaptions([]);
                setOriginalCaptions([]);
                setEditableText('');
            })
            .finally(() => { if (!cancelled) setCaptionsLoading(false); });
        return () => { cancelled = true; };
    }, [isOpen, jobId, clipIndex]);

    // Remember the look for the next clip
    useEffect(() => {
        if (!presetId) return;
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify({ presetId, overrides }));
        } catch { /* ignore */ }
    }, [presetId, overrides]);

    // Preview box height drives the font scale (presets are tuned for 1920px tall)
    useEffect(() => {
        if (!isOpen) return;
        const el = boxRef.current;
        if (!el) return;
        const measure = () => setBoxH(el.getBoundingClientRect().height);
        measure();
        if (typeof ResizeObserver === 'undefined') return;
        const ro = new ResizeObserver(measure);
        ro.observe(el);
        return () => ro.disconnect();
    }, [isOpen]);

    // Word cursor that fakes the timed animations
    useEffect(() => {
        if (!isOpen) return;
        const id = setInterval(() => setCursor((c) => c + 1), 520);
        return () => clearInterval(id);
    }, [isOpen]);

    // When user edits text, redistribute words across original timestamps
    const handleTextEdit = (newText) => {
        setEditableText(newText);
        const newWords = newText.split(/\s+/).filter((w) => w.length > 0);
        if (newWords.length === 0 || originalCaptions.length === 0) {
            setCaptions([]);
            return;
        }
        const totalDurationMs = originalCaptions[originalCaptions.length - 1].endMs - originalCaptions[0].startMs;
        const startMs = originalCaptions[0].startMs;
        const wordDurationMs = totalDurationMs / newWords.length;
        setCaptions(newWords.map((word, i) => ({
            text: word,
            startMs: Math.round(startMs + i * wordDurationMs),
            endMs: Math.round(startMs + (i + 1) * wordDurationMs),
        })));
    };

    if (!isOpen) return null;

    // ----- derived config ---------------------------------------------------
    const lib = styles || FALLBACK_STYLES;
    const presets = lib.presets;
    const preset = presets.find((p) => p.id === presetId) || presets[0];
    const cfg = { ...preset, ...overrides };
    const sortedPresets = [...presets].sort((a, b) => (b.trending ? 1 : 0) - (a.trending ? 1 : 0));
    const swatches = lib.swatches || FALLBACK_STYLES.swatches;
    const animations = lib.animations || FALLBACK_STYLES.animations;
    const positionGrid = lib.position_grid || FALLBACK_STYLES.position_grid;
    const fonts = Array.isArray(lib.fonts) ? lib.fonts : [];
    const fontOptions = fonts.some((f) => f.family === cfg.font_family)
        ? fonts
        : [{ family: cfg.font_family, file: null }, ...fonts];

    const hasOverrides = Object.keys(overrides).length > 0;
    const effectiveAnimation = cfg.animation && cfg.animation !== 'none'
        ? cfg.animation
        : (cfg.karaoke ? 'karaoke' : 'none');
    const pinned = cfg.pos_x !== undefined && cfg.pos_x !== null && cfg.pos_y !== undefined && cfg.pos_y !== null;
    const offX = offsetOf(cfg.offset_x);
    const offY = offsetOf(cfg.offset_y);
    const bgOn = !!cfg.background_enabled;
    const bgOpacity = cfg.background_opacity ?? 100;
    const bgMode = !bgOn ? 'none' : (bgOpacity < 80 ? 'semi' : 'solid');
    const shadowOn = cfg.shadow_enabled ?? ((cfg.shadow ?? 0) > 0);
    const glowOn = !!cfg.glow_enabled;
    const glowColor = cfg.glow_color || cfg.highlight_color;

    // ----- setters -----------------------------------------------------------
    const setOv = (patch) => {
        setActiveTheme(null);
        setOverrides((o) => ({ ...o, ...patch }));
    };
    const clearOv = (...keys) => {
        setActiveTheme(null);
        setOverrides((o) => {
            const next = { ...o };
            keys.forEach((k) => { delete next[k]; });
            return next;
        });
    };
    // A preset is a whole look, so picking one drops the tweaks layered on
    // the previous one; otherwise a font override would mask every chip.
    const choosePreset = (id) => {
        setPresetId(id);
        setOverrides({});
        setActiveTheme(null);
    };
    const applyTheme = (t) => {
        setActiveTheme(t.id);
        setOverrides((o) => ({ ...o, ...(t.overrides || {}) }));
    };
    const resetOverrides = () => {
        setOverrides({});
        setActiveTheme(null);
    };
    const setAnimation = (v) => setOv({ animation: v, karaoke: v === 'karaoke' });
    // Zero is the default, so it is dropped rather than stored: a preset that
    // was only nudged back to centre stays "untouched".
    const setOffset = (key, v) => {
        const n = Math.round(clampNum(v, -50, 50));
        if (n === 0) clearOv(key);
        else setOv({ [key]: n });
    };
    // The pin already encodes the final spot, so a drag replaces any nudge.
    const commitPin = (x, y) => {
        setActiveTheme(null);
        setOverrides((o) => {
            const next = { ...o, pos_x: Math.round(x * 10) / 10, pos_y: Math.round(y * 10) / 10 };
            delete next.offset_x;
            delete next.offset_y;
            return next;
        });
    };
    const setBackground = (mode) => {
        if (mode === 'none') setOv({ background_enabled: false });
        else setOv({ background_enabled: true, background_opacity: mode === 'semi' ? 60 : 100 });
    };

    // ----- preview -----------------------------------------------------------
    const scale = boxH > 0 ? boxH / 1920 : 0;
    const fontPx = Math.max(6, (cfg.font_size || 90) * (cfg.font_scale ?? 1) * scale);
    const outlineW = (cfg.outline_width ?? cfg.outline ?? 0) * scale;
    const shadowDist = (cfg.shadow_distance ?? cfg.shadow ?? 0) * scale;
    const shadowColor = hexToRgba(cfg.shadow_color || '#000000', (cfg.shadow_opacity ?? 100) / 100);
    const glowPx = (cfg.glow_intensity ?? 10) * scale * 3;
    const rotation = cfg.rotation ?? 0;

    const textShadow = [
        ...outlineShadows(outlineW, cfg.outline_color || '#000000'),
        shadowOn && shadowDist > 0 ? `${shadowDist.toFixed(2)}px ${shadowDist.toFixed(2)}px 0 ${shadowColor}` : null,
        glowOn ? `0 0 ${glowPx.toFixed(1)}px ${glowColor}` : null,
        glowOn ? `0 0 ${(glowPx * 2).toFixed(1)}px ${glowColor}` : null,
    ].filter(Boolean).join(', ') || 'none';

    const previewWords = (captions.length > 0 ? captions.slice(0, 5).map((c) => c.text) : ['Make', 'it', 'go', 'viral'])
        .map((text, index) => ({ text, index }));
    const cur = previewWords.length ? cursor % previewWords.length : 0;
    const lines = effectiveAnimation === 'one_word'
        ? [[previewWords[cur]]]
        : packLines(previewWords, cfg.max_chars || 22, cfg.max_lines || 2);

    const wordStyle = (i) => {
        const base = { display: 'inline-block', transition: 'opacity 180ms ease-out, transform 180ms ease-out' };
        switch (effectiveAnimation) {
            case 'highlight':
                return { ...base, color: i === cur ? cfg.highlight_color : cfg.primary_color, transform: i === cur ? 'scale(1.06)' : 'scale(1)' };
            case 'karaoke':
                return { ...base, color: i <= cur ? cfg.highlight_color : cfg.primary_color };
            case 'word_reveal':
                return { ...base, color: i === cur ? cfg.highlight_color : cfg.primary_color, opacity: i <= cur ? 1 : 0, transform: i <= cur ? 'scale(1)' : 'scale(0.6)' };
            case 'one_word':
                return { ...base, color: cfg.highlight_color };
            default:
                return { ...base, color: cfg.primary_color };
        }
    };

    // Placement mirrors the renderer: a pin is a centre anchor (\an5\pos) at
    // pos_x/pos_y; otherwise the anchor sits at x=50% and y=8% margin (top,
    // hanging down) / 50% (centre) / 8% margin from the bottom (standing up).
    // offset_x/offset_y move that point by a percent of the frame, so a
    // bottom caption nudged up still grows upward from its bottom edge. While
    // a drag is in flight the block follows the pointer by its centre.
    const wrapperStyle = drag
        ? { left: `${drag.x}%`, top: `${drag.y}%`, transform: 'translate(-50%, -50%)', maxWidth: '92%' }
        : pinned
            ? { left: `${Number(cfg.pos_x) + offX}%`, top: `${Number(cfg.pos_y) + offY}%`, transform: 'translate(-50%, -50%)', maxWidth: '92%' }
            : cfg.position === 'top'
                ? { top: `${8 + offY}%`, left: `${offX}%`, right: `${-offX}%` }
                : cfg.position === 'center'
                    ? { top: `${50 + offY}%`, left: `${offX}%`, right: `${-offX}%`, transform: 'translateY(-50%)' }
                    : { bottom: `${8 - offY}%`, left: `${offX}%`, right: `${-offX}%` };

    // ----- drag to place ------------------------------------------------------
    // Pointer capture keeps move/up on the block even when the pointer leaves
    // it, so no window listeners to clean up. The video underneath never gets
    // the pointerdown: the wrapper is pointer-events-none, only the block opts in.
    const dragPoint = (d, e) => ({
        x: clampNum(d.cx + ((e.clientX - d.px) / d.w) * 100, 0, 100),
        y: clampNum(d.cy + ((e.clientY - d.py) / d.h) * 100, 0, 100),
    });
    const startDrag = (e) => {
        if (isProcessing || e.button !== 0) return;
        const box = boxRef.current;
        if (!box) return;
        const b = box.getBoundingClientRect();
        const r = e.currentTarget.getBoundingClientRect();
        if (b.width <= 0 || b.height <= 0) return;
        e.preventDefault();
        try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* ignore */ }
        const d = {
            id: e.pointerId,
            px: e.clientX,
            py: e.clientY,
            cx: ((r.left + r.width / 2 - b.left) / b.width) * 100,
            cy: ((r.top + r.height / 2 - b.top) / b.height) * 100,
            w: b.width,
            h: b.height,
        };
        dragRef.current = d;
        setDrag({ x: d.cx, y: d.cy });
    };
    const moveDrag = (e) => {
        const d = dragRef.current;
        if (!d || e.pointerId !== d.id) return;
        setDrag(dragPoint(d, e));
    };
    const endDrag = (e) => {
        const d = dragRef.current;
        if (!d || e.pointerId !== d.id) return;
        dragRef.current = null;
        const p = dragPoint(d, e);
        setDrag(null);
        commitPin(p.x, p.y);
    };
    const cancelDrag = (e) => {
        const d = dragRef.current;
        if (!d || e.pointerId !== d.id) return;
        dragRef.current = null;
        setDrag(null);
    };

    const chipStyle = {
        fontFamily: fontStack(cfg.font_family),
        fontWeight: cfg.bold ? 800 : 600,
        fontSize: `${fontPx}px`,
        lineHeight: 1.15,
        letterSpacing: `${((cfg.tracking ?? 0) * scale).toFixed(2)}px`,
        textTransform: cfg.uppercase ? 'uppercase' : 'none',
        textDecoration: [cfg.underline && 'underline', cfg.strikethrough && 'line-through'].filter(Boolean).join(' ') || 'none',
        textShadow,
        transform: rotation ? `rotate(${rotation}deg)` : undefined,
        backgroundColor: bgOn ? hexToRgba(cfg.background_color || '#000000', bgOpacity / 100) : 'transparent',
        padding: bgOn ? `${(fontPx * 0.15).toFixed(1)}px ${(fontPx * 0.35).toFixed(1)}px` : 0,
        borderRadius: bgOn ? `${(fontPx * 0.12).toFixed(1)}px` : 0,
        textAlign: 'center',
        display: 'inline-block',
        maxWidth: '92%',
        // The block is the drag handle; the wrapper around it stays inert.
        pointerEvents: 'auto',
        touchAction: 'none',
        cursor: isProcessing ? 'default' : (drag ? 'grabbing' : 'grab'),
        outline: drag ? '1px dashed rgba(255,255,255,0.75)' : 'none',
        outlineOffset: '4px',
    };

    const hookText = typeof existingHook === 'string' ? existingHook : existingHook?.text;
    const hookAtBottom = existingHook && typeof existingHook === 'object' && existingHook.position === 'bottom';

    // ----- actions -----------------------------------------------------------
    // Text edits must survive the server render path too (issue #69): send
    // the edited words whenever the text differs from the transcript.
    const textEdited = originalCaptions.length > 0
        && editableText.trim() !== originalCaptions.map((c) => c.text).join(' ').trim();
    const buildOptions = (withCaptions) => ({
        preset: preset.id,
        overrides,
        captions: withCaptions && textEdited ? captions : null,
    });
    const bulkRunning = !!bulkProgress?.running;
    const busy = isProcessing || bulkRunning;

    return (
        <Modal isOpen={isOpen} onClose={onClose} size="xl" eyebrow="EDITOR · CAPTIONS" title="captions">
            <div className="flex flex-col md:flex-row gap-6">
                {/* Left: live preview */}
                <div className="flex flex-col items-center gap-2 md:shrink-0">
                    <div
                        ref={boxRef}
                        className="relative bg-black rounded-card border border-rule overflow-hidden aspect-[9/16] h-[40vh] md:h-[56vh] select-none"
                    >
                        {videoUrl && (
                            <video
                                src={videoUrl}
                                className="absolute inset-0 w-full h-full object-cover"
                                draggable={false}
                                muted
                                loop
                                autoPlay
                                playsInline
                            />
                        )}
                        {hookText && (
                            <div className={`absolute left-0 right-0 flex justify-center pointer-events-none ${hookAtBottom ? 'bottom-[3%]' : 'top-[3%]'}`}>
                                <span className="max-w-[80%] truncate rounded px-2 py-0.5 text-[10px] font-bold bg-white/85 text-black">{hookText}</span>
                            </div>
                        )}
                        <div className="absolute flex justify-center pointer-events-none px-2" style={wrapperStyle}>
                            <span
                                style={chipStyle}
                                onPointerDown={startDrag}
                                onPointerMove={moveDrag}
                                onPointerUp={endDrag}
                                onPointerCancel={cancelDrag}
                                title={isProcessing ? undefined : 'Drag to place'}
                            >
                                {lines.map((line, li) => (
                                    <span key={li} className="block whitespace-nowrap">
                                        {line.map((w, wi) => (
                                            <span key={w.index}>
                                                {wi > 0 && ' '}
                                                <span style={wordStyle(w.index)}>{w.text}</span>
                                            </span>
                                        ))}
                                    </span>
                                ))}
                            </span>
                        </div>
                        {captionsLoading && (
                            <div className="absolute top-2 left-2 flex items-center gap-1.5 text-white/70">
                                <Loader2 size={12} className="animate-spin" />
                                <span className="readout text-white/70">words</span>
                            </div>
                        )}
                    </div>
                    <p className="readout">{preset.label}{hasOverrides ? ' · edited' : ''}</p>
                    <p className="text-[11px] text-muted lowercase -mt-1">drag the caption to place it</p>
                </div>

                {/* Right: controls */}
                <div className="flex-1 min-w-0 flex flex-col md:h-[56vh]">
                    {!styles ? (
                        <div className="flex-1 flex items-center justify-center gap-2 text-muted">
                            <Loader2 size={16} className="animate-spin" />
                            <span className="text-sm lowercase">loading styles...</span>
                        </div>
                    ) : (
                        <>
                            {/* Tab strip */}
                            <div className="flex items-center border-b border-rule shrink-0 -mx-1 px-1">
                                <div className="flex gap-0.5 overflow-x-auto custom-scrollbar">
                                    {TABS.map((t) => (
                                        <button
                                            key={t.id}
                                            type="button"
                                            onClick={() => setTab(t.id)}
                                            className={`px-3 py-2 text-xs lowercase border-b-2 -mb-px whitespace-nowrap transition-colors
                                                ${tab === t.id
                                                    ? 'border-[color:var(--color-accent)] text-ink'
                                                    : 'border-transparent text-muted hover:text-ink2'}`}
                                        >
                                            {t.label}
                                        </button>
                                    ))}
                                </div>
                                {hasOverrides && (
                                    <button
                                        type="button"
                                        onClick={resetOverrides}
                                        className="ml-auto flex items-center gap-1 text-[11px] text-muted hover:text-ink2 lowercase shrink-0 pl-2"
                                        title="Back to the untouched preset"
                                    >
                                        <RotateCcw size={11} /> reset
                                    </button>
                                )}
                            </div>

                            <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar pr-1 pt-4 space-y-5">
                                {/* -------- PRESETS -------- */}
                                {tab === 'presets' && (
                                    <>
                                        <div>
                                            <p className="eyebrow mb-2">Look</p>
                                            <div className="grid grid-cols-2 gap-1.5">
                                                {sortedPresets.map((p) => {
                                                    const active = p.id === preset.id;
                                                    return (
                                                        <button
                                                            key={p.id}
                                                            type="button"
                                                            onClick={() => choosePreset(p.id)}
                                                            className={`px-2.5 py-2 rounded-input border text-left flex items-center gap-2 min-w-0 transition-colors
                                                                ${active
                                                                    ? 'border-[color:var(--color-accent)] bg-paper3 text-ink'
                                                                    : 'border-rule2 text-ink2 hover:border-[color:var(--color-accent)]'}`}
                                                            title={p.label}
                                                        >
                                                            <span
                                                                className="w-2.5 h-2.5 rounded-full shrink-0 ring-1 ring-black/40"
                                                                style={{ backgroundColor: p.highlight_color }}
                                                            />
                                                            <span
                                                                className="truncate text-sm leading-tight"
                                                                style={{
                                                                    fontFamily: fontStack(p.font_family),
                                                                    fontWeight: p.bold ? 700 : 500,
                                                                    textTransform: p.uppercase ? 'uppercase' : 'none',
                                                                }}
                                                            >
                                                                {p.label}
                                                            </span>
                                                            {p.trending && (
                                                                <span className="ml-auto readout text-[9px] text-brass shrink-0">trending</span>
                                                            )}
                                                        </button>
                                                    );
                                                })}
                                            </div>
                                        </div>
                                        {lib.themes?.length > 0 && (
                                            <div>
                                                <p className="eyebrow mb-2">Theme</p>
                                                <div className="flex flex-wrap gap-1.5">
                                                    {lib.themes.map((t) => (
                                                        <Chip
                                                            key={t.id}
                                                            active={activeTheme === t.id}
                                                            onClick={() => applyTheme(t)}
                                                            style={{ fontFamily: fontStack(t.font) }}
                                                            title={t.label}
                                                        >
                                                            <span className="w-2 h-2 rounded-full shrink-0 ring-1 ring-black/40" style={{ backgroundColor: t.color }} />
                                                            {t.label}
                                                        </Chip>
                                                    ))}
                                                </div>
                                                <p className="text-[11px] text-muted mt-1.5 leading-relaxed">
                                                    A theme layers its font, colours and animation over the current look.
                                                </p>
                                            </div>
                                        )}
                                    </>
                                )}

                                {/* -------- TEXT -------- */}
                                {tab === 'text' && (
                                    <>
                                        <div>
                                            <p className="eyebrow mb-2">Font</p>
                                            <select
                                                value={cfg.font_family}
                                                onChange={(e) => setOv({ font_family: e.target.value })}
                                                className="input-field"
                                                style={{ fontFamily: fontStack(cfg.font_family) }}
                                            >
                                                {fontOptions.map((f) => (
                                                    <option key={f.family} value={f.family} style={{ fontFamily: fontStack(f.family) }}>
                                                        {f.family}
                                                    </option>
                                                ))}
                                            </select>
                                        </div>
                                        <Slider
                                            label="Size"
                                            min={40}
                                            max={250}
                                            value={Math.round((cfg.font_scale ?? 1) * 100)}
                                            onChange={(v) => setOv({ font_scale: v / 100 })}
                                            format={(v) => `${v}%`}
                                        />
                                        <div>
                                            <p className="eyebrow mb-2">Style</p>
                                            <div className="flex flex-wrap gap-1.5">
                                                <Chip active={!!cfg.uppercase} onClick={() => setOv({ uppercase: !cfg.uppercase })} title="Uppercase">UPPER</Chip>
                                                <Chip active={!!cfg.bold} onClick={() => setOv({ bold: !cfg.bold })} title="Bold"><span className="font-bold">Bold</span></Chip>
                                                <Chip active={!!cfg.underline} onClick={() => setOv({ underline: !cfg.underline })} title="Underline"><span className="underline">Underline</span></Chip>
                                            </div>
                                        </div>
                                        <Slider
                                            label="Letter spacing"
                                            min={0}
                                            max={40}
                                            value={cfg.tracking ?? 0}
                                            onChange={(v) => setOv({ tracking: v })}
                                        />
                                    </>
                                )}

                                {/* -------- COLOR -------- */}
                                {tab === 'color' && (
                                    <>
                                        <Swatches label="Text" value={cfg.primary_color} swatches={swatches} onChange={(c) => setOv({ primary_color: c })} />
                                        <Swatches label="Highlight" value={cfg.highlight_color} swatches={swatches} onChange={(c) => setOv({ highlight_color: c })} />
                                        <Swatches label="Outline" value={cfg.outline_color} swatches={swatches} onChange={(c) => setOv({ outline_color: c })} />
                                        <div>
                                            <p className="eyebrow mb-2">Animation</p>
                                            <div className="flex flex-wrap gap-1.5">
                                                {animations.map((a) => (
                                                    <Chip key={a} active={effectiveAnimation === a} onClick={() => setAnimation(a)}>
                                                        {ANIMATION_LABELS[a] || a}
                                                    </Chip>
                                                ))}
                                            </div>
                                            <p className="text-[11px] text-muted mt-1.5 leading-relaxed">
                                                Highlight and karaoke paint the word being spoken in the highlight colour.
                                            </p>
                                        </div>
                                    </>
                                )}

                                {/* -------- EFFECTS -------- */}
                                {tab === 'effects' && (
                                    <>
                                        <Slider
                                            label="Outline"
                                            min={0}
                                            max={40}
                                            value={cfg.outline_width ?? cfg.outline ?? 0}
                                            onChange={(v) => setOv({ outline_width: v })}
                                        />
                                        <div className="space-y-3">
                                            <Toggle label="Drop shadow" checked={shadowOn} onChange={(v) => setOv({ shadow_enabled: v })} />
                                            {shadowOn && (
                                                <div className="space-y-3 animate-fade pl-3 border-l border-rule">
                                                    <Slider
                                                        label="Distance"
                                                        min={0}
                                                        max={40}
                                                        value={cfg.shadow_distance ?? cfg.shadow ?? 0}
                                                        onChange={(v) => setOv({ shadow_distance: v })}
                                                    />
                                                    <Slider
                                                        label="Opacity"
                                                        min={0}
                                                        max={100}
                                                        value={cfg.shadow_opacity ?? 100}
                                                        onChange={(v) => setOv({ shadow_opacity: v })}
                                                        format={(v) => `${v}%`}
                                                    />
                                                </div>
                                            )}
                                        </div>
                                        <div className="space-y-3">
                                            <Toggle label="Glow" checked={glowOn} onChange={(v) => setOv({ glow_enabled: v })} />
                                            {glowOn && (
                                                <div className="space-y-3 animate-fade pl-3 border-l border-rule">
                                                    <Swatches value={glowColor} swatches={swatches} onChange={(c) => setOv({ glow_color: c })} />
                                                    <Slider
                                                        label="Intensity"
                                                        min={0}
                                                        max={30}
                                                        value={cfg.glow_intensity ?? 10}
                                                        onChange={(v) => setOv({ glow_intensity: v })}
                                                    />
                                                </div>
                                            )}
                                        </div>
                                        <div className="space-y-3">
                                            <p className="eyebrow">Background</p>
                                            <SegmentedControl options={BACKGROUND_OPTIONS} value={bgMode} onChange={setBackground} size="sm" />
                                            {bgOn && (
                                                <div className="animate-fade pl-3 border-l border-rule">
                                                    <Swatches value={cfg.background_color} swatches={swatches} onChange={(c) => setOv({ background_color: c })} />
                                                </div>
                                            )}
                                        </div>
                                    </>
                                )}

                                {/* -------- POSITION -------- */}
                                {tab === 'position' && (
                                    <>
                                        <div className="flex gap-4">
                                            <div>
                                                <p className="eyebrow mb-2">Pin</p>
                                                <div className="grid grid-cols-3 gap-1 w-[72px] aspect-[9/16] bg-paper3 rounded-input p-1.5 border border-rule">
                                                    {positionGrid.map(([x, y]) => {
                                                        const active = pinned && Number(cfg.pos_x) === x && Number(cfg.pos_y) === y;
                                                        return (
                                                            <button
                                                                key={`${x}-${y}`}
                                                                type="button"
                                                                onClick={() => setOv({ pos_x: x, pos_y: y })}
                                                                className="flex items-center justify-center"
                                                                title={`${x}% · ${y}%`}
                                                            >
                                                                <span className={`w-2.5 h-2.5 rounded-full transition-colors ${active ? 'bg-brass' : 'bg-[color:var(--color-rule-2)] hover:bg-muted'}`} />
                                                            </button>
                                                        );
                                                    })}
                                                </div>
                                            </div>
                                            <div className="flex-1 min-w-0 space-y-3">
                                                <div className={pinned ? 'opacity-50' : ''}>
                                                    <p className="eyebrow mb-2">Anchor</p>
                                                    <SegmentedControl
                                                        options={POSITION_OPTIONS}
                                                        value={cfg.position}
                                                        onChange={(v) => setOv({ position: v })}
                                                        size="sm"
                                                    />
                                                </div>
                                                {pinned ? (
                                                    <div className="space-y-1">
                                                        <p className="readout">pinned at {Number(cfg.pos_x)}% · {Number(cfg.pos_y)}%</p>
                                                        <button
                                                            type="button"
                                                            onClick={() => clearOv('pos_x', 'pos_y')}
                                                            className="text-[11px] text-muted underline underline-offset-2 lowercase hover:text-ink2"
                                                        >
                                                            clear pin, use the anchor
                                                        </button>
                                                    </div>
                                                ) : (
                                                    <p className="text-[11px] text-muted leading-relaxed">
                                                        Pick a dot or drag the caption on the preview to pin it anywhere; the anchor applies otherwise.
                                                    </p>
                                                )}
                                            </div>
                                        </div>
                                        <div>
                                            <div className="flex items-center justify-between mb-2">
                                                <p className="eyebrow">Fine position</p>
                                                <button
                                                    type="button"
                                                    onClick={() => clearOv('offset_x', 'offset_y')}
                                                    disabled={offX === 0 && offY === 0}
                                                    className="text-[11px] text-muted underline underline-offset-2 lowercase hover:text-ink2 disabled:opacity-40 disabled:no-underline"
                                                    title="Back to the anchor or pin itself"
                                                >
                                                    center
                                                </button>
                                            </div>
                                            <div className="space-y-3">
                                                <Slider label="X" min={-50} max={50} value={offX} onChange={(v) => setOffset('offset_x', v)} format={formatOffset} />
                                                <Slider label="Y" min={-50} max={50} value={offY} onChange={(v) => setOffset('offset_y', v)} format={formatOffset} />
                                            </div>
                                            <p className="text-[11px] text-muted mt-1.5 leading-relaxed">
                                                Nudges the {pinned ? 'pin' : 'anchor'} by a percent of the frame.
                                            </p>
                                        </div>
                                        <Slider
                                            label="Rotation"
                                            min={-15}
                                            max={15}
                                            value={rotation}
                                            onChange={(v) => setOv({ rotation: v })}
                                            format={(v) => `${v}°`}
                                        />
                                        <div>
                                            <p className="eyebrow mb-2">Lines</p>
                                            <SegmentedControl
                                                options={LINES_OPTIONS}
                                                value={cfg.max_lines ?? 2}
                                                onChange={(v) => setOv({ max_lines: v })}
                                                size="sm"
                                            />
                                        </div>
                                        <Slider
                                            label="Characters per line"
                                            min={8}
                                            max={48}
                                            value={cfg.max_chars ?? 22}
                                            onChange={(v) => setOv({ max_chars: v })}
                                        />
                                    </>
                                )}
                            </div>
                        </>
                    )}

                    {/* Text editor + actions */}
                    <div className="mt-4 pt-3 border-t border-rule shrink-0 space-y-2">
                        {originalCaptions.length > 0 && (
                            <div>
                                <button
                                    type="button"
                                    onClick={() => setShowTextEditor(!showTextEditor)}
                                    className="w-full flex items-center justify-between"
                                >
                                    <span className="eyebrow flex items-center gap-1.5">
                                        <Pencil size={11} /> Edit text ({captions.length} words){textEdited ? ' · edited' : ''}
                                    </span>
                                    <span className={`text-muted transition-transform ${showTextEditor ? 'rotate-180' : ''}`}>▾</span>
                                </button>
                                {showTextEditor && (
                                    <textarea
                                        value={editableText}
                                        onChange={(e) => handleTextEdit(e.target.value)}
                                        rows={4}
                                        className="input-field resize-none leading-relaxed animate-fade mt-2"
                                        placeholder="Edit caption text..."
                                    />
                                )}
                            </div>
                        )}
                        <div className="flex gap-2">
                            <button onClick={onClose} className="btn-ghost">
                                close
                            </button>
                            <button
                                onClick={() => onGenerate(buildOptions(true))}
                                disabled={busy || !styles}
                                className="btn-primary flex-1"
                            >
                                {(isProcessing && !bulkRunning) && <Loader2 size={16} className="animate-spin text-brassink" />}
                                {(isProcessing && !bulkRunning) ? 'applying...' : 'apply to this clip'}
                            </button>
                        </div>
                        {onApplyAll && bulkCount > 1 && (
                            <button
                                onClick={() => onApplyAll(buildOptions(false))}
                                disabled={busy || !styles}
                                className="btn-ghost w-full flex items-center justify-center gap-2"
                            >
                                {bulkRunning
                                    ? <><Loader2 size={16} className="animate-spin" />applying {bulkProgress.current} / {bulkProgress.total}</>
                                    : `apply to all ${bulkCount} clips`}
                            </button>
                        )}
                        {bulkRunning && bulkProgress.errors > 0 && (
                            <p className="text-[11px] text-warn">{bulkProgress.errors} clip{bulkProgress.errors === 1 ? '' : 's'} failed so far</p>
                        )}
                        {onRemove && (
                            <button
                                onClick={onRemove}
                                disabled={busy}
                                className="text-xs text-muted underline underline-offset-2 lowercase hover:text-ink2 disabled:opacity-50"
                            >
                                remove captions from this clip
                            </button>
                        )}
                    </div>
                </div>
            </div>
        </Modal>
    );
}
