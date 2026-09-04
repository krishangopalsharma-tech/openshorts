import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ImagePlus, Loader2, Trash2, Type, Upload } from 'lucide-react';
import Modal from './ui/Modal';
import SegmentedControl from './ui/SegmentedControl';
import { getApiUrl } from '../config';
import { apiFetch } from '../lib/api';

/**
 * Logo and text overlays for one clip (or every clip of the job), placed with
 * the mouse over the clip itself. Every geometry value handed to onApply /
 * onApplyAll is a FRACTION of the frame (centre x/y, width, text height), so
 * the same list lands in the same place on a 9:16, a 1:1 and a 16:9 render,
 * which is what makes "apply to all" meaningful.
 *
 * The preview mirrors the server renderer: a text item is a box `w` wide
 * whose font is `size` of the frame height, line-height 1.2, padded
 * 0.35em/0.25em, stroked with eight text-shadows; an image is `w` wide with
 * its aspect kept. Close is enough: the server is the source of truth.
 */

const STORAGE_KEY = 'os_overlays_last_v1';
const MAX_ITEMS = 8;
const MAX_TEXT_CHARS = 200;
const MAX_TEXT_LINES = 4;
const SNAP = 0.015;

const ASPECT = { vertical: 9 / 16, square: 1, horizontal: 16 / 9 };

const SWATCHES = ['#FFFFFF', '#FFD400', '#FFB020', '#FF3B30', '#FF2D78', '#27E36B', '#22D3EE', '#3B82F6', '#7C4DFF', '#000000'];

const BG_OPTIONS = [
    { value: 'none', label: 'none' },
    { value: 'semi', label: 'semi' },
    { value: 'solid', label: 'solid' },
];

const ALIGN_OPTIONS = [
    { value: 'left', label: 'left' },
    { value: 'center', label: 'center' },
    { value: 'right', label: 'right' },
];

const TEXT_DEFAULTS = {
    text: 'Your text here', size: 0.05, font_family: 'Roboto', color: '#FFFFFF', bold: true,
    uppercase: false, outline_color: '#000000', outline: 4, bg_color: '#000000', bg_opacity: 0, align: 'center',
};

// ---------------------------------------------------------------------------
// Module-level caches: the asset list and the font list are static per deploy
// (assets refetched after an upload), and every card would otherwise hit both
// endpoints each time its editor opens.
// ---------------------------------------------------------------------------
let assetCache = null;
let assetFetch = null;
let fontCache = null;
let fontFetch = null;
let fontsRegistered = false;

async function fetchAssets(force = false) {
    if (!force && assetCache) return assetCache;
    if (!force && assetFetch) return assetFetch;
    assetFetch = apiFetch('/api/overlays/assets')
        .then(async (res) => {
            if (!res.ok) throw new Error(await res.text());
            const data = await res.json();
            assetCache = Array.isArray(data.assets) ? data.assets : [];
            return assetCache;
        })
        .finally(() => { assetFetch = null; });
    return assetFetch;
}

// Same @font-face injection SubtitleModal does for its own preview, under a
// different tag id so neither one skips the other. A duplicate @font-face for
// a family already declared is harmless.
function registerFonts(fonts) {
    if (fontsRegistered || !Array.isArray(fonts) || fonts.length === 0 || typeof document === 'undefined') return;
    fontsRegistered = true;
    const css = fonts
        .filter((f) => f && f.family && f.file)
        .map((f) => `@font-face{font-family:${JSON.stringify(f.family)};src:url(${JSON.stringify(getApiUrl('/fonts/' + encodeURIComponent(f.file)))});font-display:swap;}`)
        .join('\n');
    const tag = document.createElement('style');
    tag.id = 'os-overlay-fonts';
    tag.textContent = css;
    document.head.appendChild(tag);
}

function fetchFonts() {
    if (fontCache) return Promise.resolve(fontCache);
    if (!fontFetch) {
        fontFetch = apiFetch('/api/caption-styles')
            .then((res) => (res.ok ? res.json() : null))
            .catch(() => null)
            .then((data) => {
                const fonts = Array.isArray(data?.fonts) ? data.fonts.filter((f) => f && f.family) : [];
                if (fonts.length) {
                    fontCache = fonts;
                    registerFonts(fonts);
                } else {
                    fontFetch = null;
                }
                return fonts;
            });
    }
    return fontFetch;
}

// ---------------------------------------------------------------------------
// Item helpers
// ---------------------------------------------------------------------------
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const num = (v, fallback) => (Number.isFinite(Number(v)) ? Number(v) : fallback);
const isHex = (v) => /^#[0-9a-fA-F]{6}$/.test(v || '');

let nextId = 1;
const newId = () => `ov${nextId++}`;

// A server/stored item into a full local item with every default filled in.
function hydrate(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const type = raw.type === 'image' ? 'image' : raw.type === 'text' ? 'text' : null;
    if (!type) return null;
    const base = {
        id: newId(),
        type,
        x: clamp(num(raw.x, 0.5), 0, 1),
        y: clamp(num(raw.y, 0.5), 0, 1),
        w: clamp(num(raw.w, 0.3), 0.02, 1),
        opacity: clamp(num(raw.opacity, 1), 0, 1),
        rotation: clamp(num(raw.rotation, 0), -180, 180),
    };
    if (type === 'image') {
        if (!raw.asset) return null;
        return { ...base, asset: String(raw.asset) };
    }
    return {
        ...base,
        text: limitText(typeof raw.text === 'string' ? raw.text : TEXT_DEFAULTS.text),
        size: clamp(num(raw.size, TEXT_DEFAULTS.size), 0.01, 0.3),
        font_family: typeof raw.font_family === 'string' && raw.font_family ? raw.font_family : TEXT_DEFAULTS.font_family,
        color: isHex(raw.color) ? raw.color.toUpperCase() : TEXT_DEFAULTS.color,
        bold: raw.bold ?? TEXT_DEFAULTS.bold,
        uppercase: !!raw.uppercase,
        outline_color: isHex(raw.outline_color) ? raw.outline_color.toUpperCase() : TEXT_DEFAULTS.outline_color,
        outline: clamp(num(raw.outline, TEXT_DEFAULTS.outline), 0, 40),
        bg_color: isHex(raw.bg_color) ? raw.bg_color.toUpperCase() : TEXT_DEFAULTS.bg_color,
        bg_opacity: clamp(num(raw.bg_opacity, 0), 0, 1),
        align: ['left', 'center', 'right'].includes(raw.align) ? raw.align : TEXT_DEFAULTS.align,
    };
}

function hydrateList(list) {
    return (Array.isArray(list) ? list : []).map(hydrate).filter(Boolean).slice(0, MAX_ITEMS);
}

// A local item into exactly the keys the server accepts.
function serialize(item) {
    const round = (v) => Math.round(v * 10000) / 10000;
    const common = {
        type: item.type,
        x: round(clamp(item.x, 0, 1)),
        y: round(clamp(item.y, 0, 1)),
        w: round(clamp(item.w, 0.02, 1)),
        opacity: round(clamp(item.opacity, 0, 1)),
        rotation: Math.round(clamp(item.rotation, -180, 180) * 10) / 10,
    };
    if (item.type === 'image') return { ...common, asset: item.asset };
    return {
        ...common,
        text: limitText(item.text),
        size: round(clamp(item.size, 0.01, 0.3)),
        font_family: item.font_family,
        color: item.color,
        bold: !!item.bold,
        uppercase: !!item.uppercase,
        outline_color: item.outline_color,
        outline: Math.round(clamp(item.outline, 0, 40)),
        bg_color: item.bg_color,
        bg_opacity: round(clamp(item.bg_opacity, 0, 1)),
        align: item.align,
    };
}

function limitText(text) {
    return String(text ?? '')
        .split('\n')
        .slice(0, MAX_TEXT_LINES)
        .join('\n')
        .slice(0, MAX_TEXT_CHARS);
}

function loadLast() {
    try {
        const s = JSON.parse(localStorage.getItem(STORAGE_KEY));
        return Array.isArray(s) && s.length ? s : null;
    } catch { return null; }
}

function saveLast(items) {
    try {
        if (items.length) localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
    } catch { /* ignore */ }
}

const hexToRgba = (hex, alpha) => {
    const h = (hex || '#000000').replace('#', '');
    const n = parseInt(h, 16);
    if (Number.isNaN(n)) return `rgba(0,0,0,${alpha})`;
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
};

// A fake stroke: eight copies of the text pushed out at 45 degree steps.
const outlineShadows = (width, color) => {
    if (width <= 0) return 'none';
    const out = [];
    for (let i = 0; i < 8; i++) {
        const a = (i * Math.PI) / 4;
        out.push(`${(Math.cos(a) * width).toFixed(2)}px ${(Math.sin(a) * width).toFixed(2)}px 0 ${color}`);
    }
    return out.join(', ');
};

const fontStack = (family) => `"${family}", "Roboto", Arial, sans-serif`;

const assetUrl = (file) => getApiUrl('/overlays/' + encodeURIComponent(file));

const isEditableTarget = (el) => {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
};

// ---------------------------------------------------------------------------
// Small controls (same look as the other modals)
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
                aria-label={label}
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

function ColorField({ label, value, onChange }) {
    const current = (value || '').toUpperCase();
    return (
        <div>
            <span className="readout block mb-1.5">{label}</span>
            <div className="flex flex-wrap gap-1.5 items-center">
                {SWATCHES.map((c) => (
                    <button
                        key={c}
                        type="button"
                        onClick={() => onChange(c)}
                        className={`w-6 h-6 rounded-full border transition-colors shrink-0 ${current === c ? 'border-brass ring-2 ring-[var(--color-accent)] ring-offset-1 ring-offset-[var(--color-paper)]' : 'border-rule2 hover:border-brass'}`}
                        style={{ backgroundColor: c }}
                        title={c}
                        aria-label={`${label} ${c}`}
                    />
                ))}
                <label
                    className="w-6 h-6 rounded-full border border-dashed border-rule2 cursor-pointer flex items-center justify-center hover:border-brass transition-colors overflow-hidden relative shrink-0"
                    title="Custom color"
                >
                    <span className="text-xs text-muted leading-none">+</span>
                    <input
                        type="color"
                        value={isHex(value) ? value : '#FFFFFF'}
                        onChange={(e) => onChange(e.target.value.toUpperCase())}
                        className="absolute inset-0 opacity-0 cursor-pointer"
                    />
                </label>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// The editor
// ---------------------------------------------------------------------------
export default function OverlayEditor({ isOpen, onClose, clip, clipCount = 1, bulkProgress, isProcessing, onApply, onApplyAll, videoUrl }) {
    const existing = clip?.overlays;
    const hasExisting = Array.isArray(existing) && existing.length > 0;
    const format = clip?.output_format || 'vertical';
    const aspect = ASPECT[format] || ASPECT.vertical;

    const [items, setItems] = useState([]);
    const [selectedId, setSelectedId] = useState(null);
    const [last, setLast] = useState(null);

    const [assets, setAssets] = useState(assetCache || []);
    const [fonts, setFonts] = useState(fontCache || []);
    const [showPicker, setShowPicker] = useState(false);
    const [loadingAssets, setLoadingAssets] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState(null);

    // Pixel size of the frame on screen; every fractional value is projected
    // through this, and font sizes need it in px.
    const stageRef = useRef(null);
    const columnRef = useRef(null);
    const fileInputRef = useRef(null);
    const [frame, setFrame] = useState({ w: 0, h: 0 });
    const [guides, setGuides] = useState({ v: false, h: false });
    const dragRef = useRef(null);

    // Re-seed from the clip each time the editor opens.
    useEffect(() => {
        if (!isOpen) return;
        const seeded = hydrateList(existing);
        setItems(seeded);
        setSelectedId(seeded.length ? seeded[seeded.length - 1].id : null);
        setLast(loadLast());
        setShowPicker(false);
        setError(null);
        // Only the open matters: a refresh of clip.overlays while the user is
        // mid-edit must not wipe their work.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isOpen]);

    useEffect(() => {
        if (!isOpen) return;
        let cancelled = false;
        setLoadingAssets(!assetCache);
        fetchAssets()
            .then((list) => { if (!cancelled) setAssets(list); })
            .catch(() => { if (!cancelled) setAssets([]); })
            .finally(() => { if (!cancelled) setLoadingAssets(false); });
        fetchFonts().then((list) => { if (!cancelled && list.length) setFonts(list); });
        return () => { cancelled = true; };
    }, [isOpen]);

    // Fit the stage: as tall as ~60vh allows, never wider than the column.
    useEffect(() => {
        if (!isOpen) return;
        const el = columnRef.current;
        if (!el) return;
        const measure = () => {
            const maxH = Math.max(160, window.innerHeight * 0.6);
            const maxW = Math.max(120, el.clientWidth);
            let h = maxH;
            let w = h * aspect;
            if (w > maxW) { w = maxW; h = w / aspect; }
            setFrame({ w: Math.floor(w), h: Math.floor(h) });
        };
        measure();
        const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null;
        ro?.observe(el);
        window.addEventListener('resize', measure);
        return () => { ro?.disconnect(); window.removeEventListener('resize', measure); };
    }, [isOpen, aspect]);

    const selected = items.find((i) => i.id === selectedId) || null;

    const updateItem = useCallback((id, patch) => {
        setItems((list) => list.map((i) => (i.id === id ? { ...i, ...(typeof patch === 'function' ? patch(i) : patch) } : i)));
    }, []);

    const removeItem = useCallback((id) => {
        setItems((list) => list.filter((i) => i.id !== id));
        setSelectedId((cur) => (cur === id ? null : cur));
    }, []);

    const moveItem = (id, dir) => {
        setItems((list) => {
            const idx = list.findIndex((i) => i.id === id);
            const to = idx + dir;
            if (idx < 0 || to < 0 || to >= list.length) return list;
            const next = list.slice();
            [next[idx], next[to]] = [next[to], next[idx]];
            return next;
        });
    };

    const addItem = (item) => {
        if (items.length >= MAX_ITEMS) {
            setError(`up to ${MAX_ITEMS} overlays per clip`);
            return;
        }
        const full = { ...item, id: newId() };
        setItems((list) => [...list, full]);
        setSelectedId(full.id);
        setError(null);
    };

    const addLogo = (asset) => {
        addItem({ type: 'image', asset: asset.file, x: 0.85, y: 0.08, w: 0.22, opacity: 1, rotation: 0 });
        setShowPicker(false);
    };

    const addText = () => {
        addItem({ type: 'text', x: 0.5, y: 0.85, w: 0.8, opacity: 1, rotation: 0, ...TEXT_DEFAULTS });
    };

    const reuseLast = () => {
        const seeded = hydrateList(last);
        setItems(seeded);
        setSelectedId(seeded.length ? seeded[seeded.length - 1].id : null);
    };

    // Keyboard: delete the selection, nudge it with the arrows. Only when the
    // focus is not in a field, or typing into the textarea would move things.
    useEffect(() => {
        if (!isOpen) return;
        const onKey = (e) => {
            if (isEditableTarget(document.activeElement) || isEditableTarget(e.target)) return;
            if (!selectedId) return;
            if (e.key === 'Delete' || e.key === 'Backspace') {
                e.preventDefault();
                removeItem(selectedId);
                return;
            }
            const step = e.shiftKey ? 0.05 : 0.01;
            const nudge = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
            if (!nudge) return;
            e.preventDefault();
            updateItem(selectedId, (i) => ({ x: clamp(i.x + nudge[0], 0, 1), y: clamp(i.y + nudge[1], 0, 1) }));
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [isOpen, selectedId, removeItem, updateItem]);

    // -- pointer: move --------------------------------------------------------
    const startMove = (e, item) => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        setSelectedId(item.id);
        dragRef.current = { kind: 'move', id: item.id, px: e.clientX, py: e.clientY, x: item.x, y: item.y };
        e.currentTarget.setPointerCapture?.(e.pointerId);
    };

    // -- pointer: resize from the bottom-right corner ------------------------
    const startResize = (e, item) => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        setSelectedId(item.id);
        dragRef.current = { kind: 'resize', id: item.id, px: e.clientX, py: e.clientY, w: item.w, size: item.size };
        e.currentTarget.setPointerCapture?.(e.pointerId);
    };

    const onPointerMove = (e) => {
        const d = dragRef.current;
        if (!d || !frame.w || !frame.h) return;
        const dx = (e.clientX - d.px) / frame.w;
        const dy = (e.clientY - d.py) / frame.h;
        if (d.kind === 'move') {
            let x = clamp(d.x + dx, 0, 1);
            let y = clamp(d.y + dy, 0, 1);
            const snapV = Math.abs(x - 0.5) < SNAP;
            const snapH = Math.abs(y - 0.5) < SNAP;
            if (snapV) x = 0.5;
            if (snapH) y = 0.5;
            setGuides({ v: snapV, h: snapH });
            updateItem(d.id, { x, y });
        } else {
            // The item stays centred, so the corner moves half as fast as the
            // width grows: a corner drag of dx widens the box by 2·dx.
            const w = clamp(d.w + 2 * dx, 0.02, 1);
            const factor = w / d.w;
            updateItem(d.id, (i) => (i.type === 'text'
                ? { w, size: clamp(d.size * factor, 0.01, 0.3) }
                : { w }));
        }
    };

    const endDrag = (e) => {
        if (!dragRef.current) return;
        dragRef.current = null;
        setGuides({ v: false, h: false });
        e.currentTarget.releasePointerCapture?.(e.pointerId);
    };

    // -- upload ----------------------------------------------------------------
    const handleUpload = async (e) => {
        const file = e.target.files?.[0];
        e.target.value = '';
        if (!file) return;
        setUploading(true);
        setError(null);
        try {
            const fd = new FormData();
            fd.append('file', file);
            const res = await apiFetch('/api/overlays/upload', { method: 'POST', body: fd });
            if (!res.ok) {
                const text = await res.text();
                let detail = text;
                try { detail = JSON.parse(text).detail || text; } catch { /* plain text */ }
                throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
            }
            const data = await res.json();
            const list = await fetchAssets(true);
            setAssets(list);
            if (data.asset?.file) addLogo(data.asset);
        } catch (err) {
            setError(err.message || 'upload failed');
        } finally {
            setUploading(false);
        }
    };

    // -- apply ----------------------------------------------------------------
    const bulkRunning = !!bulkProgress?.running;
    const busy = isProcessing || bulkRunning || uploading;
    // An empty list on a clip that has nothing is a no-op; everything else applies.
    const nothingToApply = items.length === 0 && !hasExisting;

    const apply = (fn) => {
        const payload = items.map(serialize);
        saveLast(payload);
        fn(payload);
    };

    const setSel = (patch) => selected && updateItem(selected.id, patch);

    const bgMode = !selected || selected.bg_opacity <= 0 ? 'none' : selected.bg_opacity >= 1 ? 'solid' : 'semi';
    const fontOptions = selected?.type === 'text' && !fonts.some((f) => f.family === selected.font_family)
        ? [{ family: selected.font_family, file: null }, ...fonts]
        : fonts;

    const renderItem = (item) => {
        const isSel = item.id === selectedId;
        const common = {
            position: 'absolute',
            left: `${item.x * 100}%`,
            top: `${item.y * 100}%`,
            width: `${item.w * 100}%`,
            transform: `translate(-50%, -50%) rotate(${item.rotation}deg)`,
            opacity: item.opacity,
            touchAction: 'none',
        };
        const asset = item.type === 'image' ? assets.find((a) => a.file === item.asset) : null;
        return (
            <div
                key={item.id}
                style={common}
                className={`group/ov cursor-move select-none ${isSel ? 'outline outline-2 outline-[var(--color-accent)] outline-offset-1' : 'hover:outline hover:outline-1 hover:outline-dashed hover:outline-[color:var(--color-rule-2)]'}`}
                onPointerDown={(e) => startMove(e, item)}
                onPointerMove={onPointerMove}
                onPointerUp={endDrag}
                onPointerCancel={endDrag}
                role="button"
                tabIndex={-1}
                aria-label={item.type === 'image' ? `logo ${item.asset}` : `text ${item.text}`}
            >
                {item.type === 'image' ? (
                    <img
                        src={assetUrl(item.asset)}
                        alt=""
                        draggable={false}
                        className="block w-full h-auto pointer-events-none"
                        style={asset?.width && asset?.height ? { aspectRatio: `${asset.width} / ${asset.height}` } : undefined}
                    />
                ) : (
                    <div
                        className="pointer-events-none"
                        style={{
                            fontSize: `${Math.max(1, item.size * frame.h)}px`,
                            lineHeight: 1.2,
                            padding: '0.25em 0.35em',
                            fontFamily: fontStack(item.font_family),
                            fontWeight: item.bold ? 700 : 400,
                            color: item.color,
                            textAlign: item.align,
                            textTransform: item.uppercase ? 'uppercase' : 'none',
                            textShadow: outlineShadows((item.outline * frame.h) / 1920, item.outline_color),
                            background: item.bg_opacity > 0 ? hexToRgba(item.bg_color, item.bg_opacity) : 'transparent',
                            borderRadius: '0.25em',
                            whiteSpace: 'pre-wrap',
                            overflowWrap: 'break-word',
                            wordBreak: 'break-word',
                        }}
                    >
                        {item.text || ' '}
                    </div>
                )}
                {isSel && (
                    <span
                        onPointerDown={(e) => startResize(e, item)}
                        onPointerMove={onPointerMove}
                        onPointerUp={endDrag}
                        onPointerCancel={endDrag}
                        className="absolute -right-1.5 -bottom-1.5 w-3 h-3 bg-[var(--color-accent)] border border-[var(--color-paper)] cursor-nwse-resize"
                        aria-label="resize"
                        role="button"
                        tabIndex={-1}
                    />
                )}
            </div>
        );
    };

    return (
        <Modal
            isOpen={isOpen}
            onClose={busy ? undefined : onClose}
            size="xl"
            eyebrow="LOGO & TEXT"
            title="logo & text"
            footer={
                <div className="space-y-2">
                    <div className="flex gap-2">
                        <button type="button" onClick={onClose} className="btn-ghost" disabled={busy}>
                            cancel
                        </button>
                        <button
                            type="button"
                            onClick={() => apply(onApply)}
                            disabled={busy || nothingToApply}
                            className="btn-primary flex-1"
                        >
                            {(isProcessing && !bulkRunning) && <Loader2 size={16} className="animate-spin" />}
                            {(isProcessing && !bulkRunning) ? 'applying…' : 'apply to this clip'}
                        </button>
                    </div>
                    {onApplyAll && clipCount > 1 && (
                        <button
                            type="button"
                            onClick={() => apply(onApplyAll)}
                            disabled={busy || items.length === 0}
                            className="btn-ghost w-full flex items-center justify-center gap-2"
                        >
                            {bulkRunning
                                ? <><Loader2 size={16} className="animate-spin" />applying {bulkProgress.current} / {bulkProgress.total}</>
                                : `apply to all ${clipCount} clips`}
                        </button>
                    )}
                    {hasExisting && (
                        <button
                            type="button"
                            onClick={() => { setItems([]); setSelectedId(null); onApply([]); }}
                            disabled={busy}
                            className="w-full text-[11px] lowercase text-muted hover:text-danger transition-colors py-1 disabled:opacity-45 disabled:cursor-not-allowed"
                        >
                            remove all overlays
                        </button>
                    )}
                </div>
            }
        >
            <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_320px] gap-5">
                {/* Editing surface */}
                <div ref={columnRef} className="min-w-0">
                    <div
                        ref={stageRef}
                        className="relative bg-black rounded-input overflow-hidden mx-auto"
                        style={{ width: frame.w || undefined, height: frame.h || undefined, aspectRatio: `${aspect}` }}
                        onPointerDown={(e) => { if (e.target === e.currentTarget || e.target.tagName === 'VIDEO') setSelectedId(null); }}
                    >
                        {videoUrl && (
                            <video
                                src={videoUrl}
                                muted
                                loop
                                autoPlay
                                playsInline
                                preload="metadata"
                                draggable={false}
                                className="absolute inset-0 w-full h-full object-contain pointer-events-none"
                            />
                        )}
                        <div className="absolute inset-0">
                            {guides.v && <div className="absolute top-0 bottom-0 left-1/2 w-px bg-[var(--color-accent)] pointer-events-none" />}
                            {guides.h && <div className="absolute left-0 right-0 top-1/2 h-px bg-[var(--color-accent)] pointer-events-none" />}
                            {frame.h > 0 && items.map(renderItem)}
                        </div>
                    </div>
                    <p className="readout text-center mt-2 normal-case">drag to move · corner to resize · arrows to nudge</p>
                    {items.length === 0 && (
                        <p className="text-[11px] text-muted text-center mt-1">
                            add a logo or a text on the right, then place it on the clip.
                            {last && (
                                <>
                                    {' '}
                                    <button type="button" onClick={reuseLast} className="text-ink2 hover:text-brass underline underline-offset-2 lowercase">
                                        reuse last
                                    </button>
                                </>
                            )}
                        </p>
                    )}
                </div>

                {/* Panel */}
                <div className="space-y-4 min-w-0">
                    <div className="grid grid-cols-2 gap-2">
                        <button
                            type="button"
                            onClick={() => setShowPicker((v) => !v)}
                            disabled={busy || items.length >= MAX_ITEMS}
                            className={`btn-ghost w-full flex items-center justify-center gap-1.5 ${showPicker ? 'border-[var(--color-accent)]' : ''}`}
                        >
                            <ImagePlus size={14} /> add logo
                        </button>
                        <button
                            type="button"
                            onClick={addText}
                            disabled={busy || items.length >= MAX_ITEMS}
                            className="btn-ghost w-full flex items-center justify-center gap-1.5"
                        >
                            <Type size={14} /> add text
                        </button>
                    </div>

                    {showPicker && (
                        <div className="p-3 rounded-input border border-rule bg-paper space-y-2 animate-fade">
                            <div className="flex items-center justify-between gap-3">
                                <p className="eyebrow">Logos</p>
                                <button
                                    type="button"
                                    onClick={() => fileInputRef.current?.click()}
                                    disabled={uploading}
                                    className="flex items-center gap-1.5 text-[11px] lowercase text-ink2 hover:text-brass transition-colors disabled:opacity-45"
                                >
                                    {uploading ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
                                    {uploading ? 'uploading…' : 'upload png / jpg / webp'}
                                </button>
                                <input
                                    ref={fileInputRef}
                                    type="file"
                                    accept="image/png,image/jpeg,image/webp"
                                    className="hidden"
                                    onChange={handleUpload}
                                />
                            </div>
                            {loadingAssets && (
                                <div className="flex items-center gap-2 text-xs text-muted">
                                    <Loader2 size={14} className="animate-spin" /> loading…
                                </div>
                            )}
                            {!loadingAssets && assets.length === 0 && (
                                <p className="text-[11px] text-muted">no logos yet. upload a png with transparency for the best result.</p>
                            )}
                            {assets.length > 0 && (
                                <div className="grid grid-cols-4 gap-2 max-h-40 overflow-y-auto custom-scrollbar pr-0.5">
                                    {assets.map((a) => (
                                        <button
                                            key={a.file}
                                            type="button"
                                            onClick={() => addLogo(a)}
                                            title={a.name || a.file}
                                            className="aspect-square rounded-input border border-rule hover:border-brass bg-[repeating-conic-gradient(var(--color-paper-3)_0%_25%,transparent_0%_50%)] bg-[length:12px_12px] p-1.5 flex items-center justify-center transition-colors"
                                        >
                                            <img src={assetUrl(a.file)} alt={a.name || a.file} className="max-w-full max-h-full object-contain" draggable={false} />
                                        </button>
                                    ))}
                                </div>
                            )}
                        </div>
                    )}

                    {/* Layers */}
                    {items.length > 0 && (
                        <div>
                            <p className="eyebrow mb-2">Layers</p>
                            <div className="space-y-1.5">
                                {items.slice().reverse().map((item) => {
                                    const idx = items.indexOf(item);
                                    const isSel = item.id === selectedId;
                                    return (
                                        <div
                                            key={item.id}
                                            role="button"
                                            tabIndex={0}
                                            onClick={() => setSelectedId(item.id)}
                                            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedId(item.id); } }}
                                            className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-input border text-left cursor-pointer transition-colors ${
                                                isSel ? 'border-[var(--color-accent)] bg-paper3' : 'border-rule bg-paper hover:bg-paper3'
                                            }`}
                                        >
                                            <span className="w-7 h-7 rounded-input bg-paper3 flex items-center justify-center overflow-hidden shrink-0">
                                                {item.type === 'image'
                                                    ? <img src={assetUrl(item.asset)} alt="" className="max-w-full max-h-full object-contain" draggable={false} />
                                                    : <Type size={13} className="text-muted" />}
                                            </span>
                                            <span className="text-xs text-ink2 truncate flex-1 min-w-0" title={item.type === 'image' ? item.asset : item.text}>
                                                {item.type === 'image' ? item.asset : (item.text.split('\n')[0] || 'text')}
                                            </span>
                                            <button
                                                type="button"
                                                onClick={(e) => { e.stopPropagation(); moveItem(item.id, +1); }}
                                                disabled={idx === items.length - 1}
                                                aria-label="bring forward"
                                                title="bring forward"
                                                className="p-1 rounded-full text-muted hover:text-ink disabled:opacity-30 disabled:cursor-not-allowed"
                                            >
                                                <ArrowUp size={13} />
                                            </button>
                                            <button
                                                type="button"
                                                onClick={(e) => { e.stopPropagation(); moveItem(item.id, -1); }}
                                                disabled={idx === 0}
                                                aria-label="send backward"
                                                title="send backward"
                                                className="p-1 rounded-full text-muted hover:text-ink disabled:opacity-30 disabled:cursor-not-allowed"
                                            >
                                                <ArrowDown size={13} />
                                            </button>
                                            <button
                                                type="button"
                                                onClick={(e) => { e.stopPropagation(); removeItem(item.id); }}
                                                aria-label="delete layer"
                                                title="delete"
                                                className="p-1 rounded-full text-muted hover:text-danger"
                                            >
                                                <Trash2 size={13} />
                                            </button>
                                        </div>
                                    );
                                })}
                            </div>
                        </div>
                    )}

                    {/* Properties */}
                    {selected && (
                        <div className="pt-4 border-t border-rule space-y-4">
                            <p className="eyebrow">{selected.type === 'image' ? 'Logo' : 'Text'}</p>

                            {selected.type === 'text' && (
                                <>
                                    <div>
                                        <span className="readout block mb-1">TEXT</span>
                                        <textarea
                                            value={selected.text}
                                            onChange={(e) => setSel({ text: limitText(e.target.value) })}
                                            rows={2}
                                            maxLength={MAX_TEXT_CHARS}
                                            className="input-field resize-none py-2 text-xs"
                                            placeholder="your text…"
                                        />
                                        <p className="text-[10px] text-muted mt-0.5">up to {MAX_TEXT_LINES} lines · {selected.text.length}/{MAX_TEXT_CHARS}</p>
                                    </div>
                                    <div>
                                        <span className="readout block mb-1">FONT</span>
                                        <select
                                            value={selected.font_family}
                                            onChange={(e) => setSel({ font_family: e.target.value })}
                                            className="input-field py-2 text-xs"
                                            style={{ fontFamily: fontStack(selected.font_family) }}
                                        >
                                            {fontOptions.map((f) => (
                                                <option key={f.family} value={f.family} style={{ fontFamily: fontStack(f.family) }}>{f.family}</option>
                                            ))}
                                        </select>
                                    </div>
                                    <Slider label="SIZE" value={Math.round(selected.size * 1000) / 10} min={1} max={30} step={0.5} format={(v) => `${v}%`} onChange={(v) => setSel({ size: v / 100 })} />
                                    <Slider label="WIDTH" value={Math.round(selected.w * 100)} min={2} max={100} format={(v) => `${v}%`} onChange={(v) => setSel({ w: v / 100 })} />
                                    <div className="grid grid-cols-2 gap-3">
                                        <Toggle label="BOLD" checked={!!selected.bold} onChange={(v) => setSel({ bold: v })} />
                                        <Toggle label="UPPERCASE" checked={!!selected.uppercase} onChange={(v) => setSel({ uppercase: v })} />
                                    </div>
                                    <div>
                                        <span className="readout block mb-1.5">ALIGN</span>
                                        <SegmentedControl size="sm" options={ALIGN_OPTIONS} value={selected.align} onChange={(v) => setSel({ align: v })} />
                                    </div>
                                    <ColorField label="TEXT COLOR" value={selected.color} onChange={(c) => setSel({ color: c })} />
                                    <Slider label="OUTLINE" value={Math.round(selected.outline)} min={0} max={40} format={(v) => `${v}px`} onChange={(v) => setSel({ outline: v })} />
                                    {selected.outline > 0 && (
                                        <ColorField label="OUTLINE COLOR" value={selected.outline_color} onChange={(c) => setSel({ outline_color: c })} />
                                    )}
                                    <div>
                                        <span className="readout block mb-1.5">BACKGROUND</span>
                                        <SegmentedControl
                                            size="sm"
                                            options={BG_OPTIONS}
                                            value={bgMode}
                                            onChange={(v) => setSel({ bg_opacity: v === 'none' ? 0 : v === 'solid' ? 1 : 0.6 })}
                                        />
                                    </div>
                                    {selected.bg_opacity > 0 && (
                                        <ColorField label="BACKGROUND COLOR" value={selected.bg_color} onChange={(c) => setSel({ bg_color: c })} />
                                    )}
                                </>
                            )}

                            {selected.type === 'image' && (
                                <Slider label="WIDTH" value={Math.round(selected.w * 100)} min={2} max={100} format={(v) => `${v}%`} onChange={(v) => setSel({ w: v / 100 })} />
                            )}

                            <Slider label="OPACITY" value={Math.round(selected.opacity * 100)} min={0} max={100} format={(v) => `${v}%`} onChange={(v) => setSel({ opacity: v / 100 })} />
                            <Slider label="ROTATION" value={Math.round(clamp(selected.rotation, -45, 45))} min={-45} max={45} format={(v) => `${v}°`} onChange={(v) => setSel({ rotation: v })} />
                        </div>
                    )}

                    {error && <p className="text-[11px] text-danger break-words">{error}</p>}
                </div>
            </div>
        </Modal>
    );
}
