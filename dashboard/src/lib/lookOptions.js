import { apiFetch } from './api';

// Shown until GET /api/look-options answers (or if it fails). The server list
// is the source of truth: cinematic.normalize() turns any name it does not
// know into "none", so an option missing here only costs a choice, never a
// broken render.
export const FALLBACK_LOOK_OPTIONS = {
    grades: [
        { value: 'none', label: 'No grade' },
        { value: 'warm', label: 'Warm' },
        { value: 'cool', label: 'Cool' },
        { value: 'teal_orange', label: 'Teal & Orange' },
        { value: 'vintage', label: 'Vintage' },
        { value: 'vibrant', label: 'Vibrant' },
        { value: 'bw', label: 'Black & White' },
    ],
    sharpen: [{ value: 'none', label: 'No sharpening' }],
    luts: [],
};

let cached = null;

export async function fetchLookOptions() {
    if (cached) return cached;
    const res = await apiFetch('/api/look-options');
    if (!res.ok) throw new Error(`look options ${res.status}`);
    const data = await res.json();
    cached = {
        grades: Array.isArray(data.grades) && data.grades.length ? data.grades : FALLBACK_LOOK_OPTIONS.grades,
        sharpen: Array.isArray(data.sharpen) && data.sharpen.length ? data.sharpen : FALLBACK_LOOK_OPTIONS.sharpen,
        luts: Array.isArray(data.luts) ? data.luts : [],
    };
    return cached;
}
