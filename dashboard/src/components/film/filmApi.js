// Film-module API helpers. Every route is self-host only (404 in cloud mode);
// the tabs are hidden there through `filmModules` from /api/config.
import { useEffect, useRef } from 'react';
import { apiFetch } from '../../lib/api';

export async function filmJson(path, options = {}) {
  const opts = { ...options };
  if (opts.json !== undefined) {
    opts.method = opts.method || 'POST';
    opts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  const res = await apiFetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const detail = data && typeof data.detail === 'string' ? data.detail : `request failed (${res.status})`;
    throw new Error(detail);
  }
  return data;
}

// Poll /api/film/status while any render is running.
export function useRenderPoll(sessionId, renders, onRenders, interval = 3000) {
  const timer = useRef(null);
  const running = Object.values(renders || {}).some((r) => r && r.status === 'running');
  useEffect(() => {
    if (!sessionId || !running) return undefined;
    timer.current = setInterval(async () => {
      try {
        const data = await filmJson(`/api/film/status/${sessionId}`);
        onRenders(data.renders || {});
      } catch { /* transient */ }
    }, interval);
    return () => clearInterval(timer.current);
  }, [sessionId, running, onRenders, interval]);
}

export function fmtTime(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`
    : `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function downloadText(name, text) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
