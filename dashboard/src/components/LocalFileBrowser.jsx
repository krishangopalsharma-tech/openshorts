import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronUp, Clock, Folder, HardDrive, Loader2, Search, Star } from 'lucide-react';
import { apiFetch } from '../lib/api';
import {
  LAST_DIR_KEY,
  SORT_KEY,
  getRecents,
  lastFolder,
  rememberFolder,
  writeStorage,
} from '../lib/localBrowser';

/**
 * Walk the server's own disk to name a file the browser cannot see the path of.
 *
 * `File.path` is Electron, not the web, so "browse for a video" genuinely
 * cannot be done client-side: a drag-and-drop box gives bytes and a name, and
 * the sources this exists for are multi-GB files that must not be copied.
 * That constraint is not negotiable, so everything here is about making the
 * server-side walk behave like a file dialog instead of like `ls`:
 *
 * - it opens where you were last time, and offers the folders you actually use
 * - it filters on the SERVER, because both lists are capped at 500 entries and
 *   a client-side filter would search only the part that survived the cap
 * - it shows a frame from each video, because a folder of episodes named by
 *   their uploader is unreadable otherwise, and picking the wrong one is not
 *   discovered until a render has finished
 *
 * Caller supplies the shell (modal or inline panel); this is the body.
 */

const fmtSize = (bytes) => (bytes / 1048576 >= 1024
  ? `${(bytes / 1073741824).toFixed(1)} GB`
  : `${Math.round(bytes / 1048576)} MB`);

const fmtWhen = (mtime) => {
  if (!mtime) return '';
  const days = (Date.now() / 1000 - mtime) / 86400;
  if (days < 1) return 'today';
  if (days < 2) return 'yesterday';
  if (days < 30) return `${Math.round(days)}d ago`;
  return new Date(mtime * 1000).toLocaleDateString();
};

const fmtDuration = (seconds) => {
  if (!seconds) return '';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m >= 60
    ? `${Math.floor(m / 60)}h ${m % 60}m`
    : `${m}:${String(s).padStart(2, '0')}`;
};

/**
 * One row's thumbnail. Loaded per row rather than as part of the folder
 * listing: decoding a frame costs ~0.2 s per file and a folder of 200 would
 * hold the listing for a minute before anything appeared. The duration rides
 * back on a response header, so the frame and the runtime are one request.
 */
function Thumb({ path, onDuration }) {
  const [state, setState] = useState('loading');
  const [src, setSrc] = useState('');

  useEffect(() => {
    let alive = true;
    let objectUrl = '';
    (async () => {
      try {
        const res = await apiFetch(`/api/local/thumb?path=${encodeURIComponent(path)}`);
        if (!res.ok) throw new Error('no frame');
        const seconds = Number(res.headers.get('X-Duration-Seconds')) || 0;
        const blob = await res.blob();
        if (!alive) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
        setState('ok');
        if (seconds) onDuration?.(seconds);
      } catch {
        if (alive) setState('failed');
      }
    })();
    return () => { alive = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [path, onDuration]);

  return (
    <span className="shrink-0 w-[68px] h-[38px] rounded bg-paper3 border border-rule overflow-hidden flex items-center justify-center">
      {state === 'ok'
        ? <img src={src} alt="" className="w-full h-full object-cover" />
        : <span className="text-[10px] text-muted">{state === 'loading' ? '…' : '—'}</span>}
    </span>
  );
}

export default function LocalFileBrowser({ kind = 'video', onPick, autoFocus = true }) {
  const [tree, setTree] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState(() => {
    try { return localStorage.getItem(SORT_KEY) || 'modified'; } catch { return 'modified'; }
  });
  const [durations, setDurations] = useState({});
  const [recents, setRecents] = useState(getRecents);
  const searchRef = useRef(null);
  // The listing is re-requested on every keystroke; only the newest answer may
  // paint, or a slow folder overwrites the one the user is now looking at.
  const requestId = useRef(0);

  const load = useCallback(async (path, opts = {}) => {
    const mine = ++requestId.current;
    const q = opts.query ?? '';
    const order = opts.sort ?? sort;
    setBusy(true);
    setError('');
    try {
      const params = new URLSearchParams();
      if (path) params.set('path', path);
      params.set('kind', kind);
      params.set('sort', order);
      if (q.trim()) params.set('q', q.trim());
      const res = await apiFetch(`/api/local/browse?${params.toString()}`);
      const data = await res.json();
      if (mine !== requestId.current) return;
      if (!res.ok) { setError(data.detail || 'Could not read that folder.'); return; }
      setTree(data);
      if (data.path) writeStorage(LAST_DIR_KEY, data.path);
    } catch {
      if (mine === requestId.current) setError('Could not reach the backend.');
    } finally {
      if (mine === requestId.current) setBusy(false);
    }
  }, [kind, sort]);

  // Open where they left off. A folder that has since been deleted or
  // unplugged falls back to the drive list rather than showing an error on a
  // screen the user has not interacted with yet.
  useEffect(() => {
    const start = lastFolder();
    (async () => {
      if (!start) { load(''); return; }
      try {
        const res = await apiFetch(
          `/api/local/browse?path=${encodeURIComponent(start)}&kind=${kind}&sort=${sort}`);
        if (res.ok) { setTree(await res.json()); return; }
      } catch { /* fall through */ }
      load('');
    })();
    // Deliberately once, on mount: re-running this would yank the user back
    // to the remembered folder every time they changed the sort.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (autoFocus) searchRef.current?.focus();
  }, [autoFocus]);

  // Debounced so typing eight characters is one listing, not eight.
  useEffect(() => {
    if (!tree?.path) return undefined;
    const t = setTimeout(() => load(tree.path, { query }), 180);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  const join = (dir, name) => {
    const sep = tree?.sep || '/';
    return dir.endsWith(sep) ? dir + name : dir + sep + name;
  };

  const enter = (path) => { setQuery(''); load(path, { query: '' }); };

  const choose = (name) => {
    const full = join(tree.path, name);
    rememberFolder(tree.path);
    setRecents(getRecents());
    onPick?.(full);
  };

  const changeSort = (next) => {
    setSort(next);
    writeStorage(SORT_KEY, next);
    load(tree?.path || '', { query, sort: next });
  };

  const dirs = tree?.dir_entries
    ? tree.dir_entries.map((d) => d.name)
    : (tree?.dirs || []);

  return (
    <div className="flex flex-col min-h-0 gap-2">
      <div className="flex items-center gap-2">
        <div className="flex-1 min-w-0 flex items-center gap-2 rounded-input border border-rule bg-paper3 px-2.5 py-1.5">
          <Search size={14} className="text-muted shrink-0" />
          <input
            ref={searchRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="filter this folder"
            spellCheck={false}
            className="flex-1 min-w-0 bg-transparent text-[13px] text-ink outline-none"
            aria-label="filter files in this folder"
          />
        </div>
        <button
          type="button"
          onClick={() => changeSort(sort === 'modified' ? 'name' : 'modified')}
          className="btn-quiet shrink-0 text-[12px] flex items-center gap-1.5"
          title="Sort order"
        >
          <Clock size={13} />
          {sort === 'modified' ? 'newest' : 'name'}
        </button>
      </div>

      {recents.length > 0 && (
        <div className="flex items-center gap-1.5 flex-wrap">
          <Star size={12} className="text-muted shrink-0" />
          {recents.map((dir) => (
            <button
              key={dir}
              type="button"
              onClick={() => enter(dir)}
              title={dir}
              className="px-2 py-0.5 rounded border border-rule text-[11px] text-ink2 hover:border-brass max-w-[190px] truncate"
            >
              {dir.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || dir}
            </button>
          ))}
        </div>
      )}

      <div className="flex items-center gap-2 text-[11px] font-mono text-muted">
        {busy ? <Loader2 size={13} className="animate-spin shrink-0" /> : <HardDrive size={13} className="shrink-0" />}
        <span className="truncate flex-1">{tree?.path || 'this computer'}</span>
        {tree?.parent && (
          <button type="button" onClick={() => enter(tree.parent)}
                  className="hover:text-ink flex items-center gap-1 shrink-0">
            <ChevronUp size={13} /> up
          </button>
        )}
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar text-[13px] -mx-1 px-1">
        {error && <p className="px-2 py-1.5 text-bad">{error}</p>}

        {tree && !tree.path && (tree.drives || []).map((d) => (
          <button key={d} type="button" onClick={() => enter(d)}
                  className="flex w-full items-center gap-2 px-2 py-1.5 rounded hover:bg-paper3 font-mono text-ink2">
            <HardDrive size={14} className="text-muted shrink-0" /> {d}
          </button>
        ))}

        {tree?.path && dirs.map((d) => (
          <button key={d} type="button" onClick={() => enter(join(tree.path, d))}
                  className="flex w-full items-center gap-2 px-2 py-1.5 rounded hover:bg-paper3 text-ink2">
            <Folder size={14} className="text-muted shrink-0" />
            <span className="truncate text-left">{d}</span>
          </button>
        ))}

        {tree?.path && (tree.files || []).map((f) => (
          <button key={f.name} type="button" onClick={() => choose(f.name)}
                  className="flex w-full items-center gap-2.5 px-2 py-1.5 rounded hover:bg-paper3 text-ink">
            {kind === 'video' && (
              <Thumb
                path={join(tree.path, f.name)}
                onDuration={(s) => setDurations((prev) => (
                  prev[f.name] === s ? prev : { ...prev, [f.name]: s }))}
              />
            )}
            <span className="flex-1 min-w-0 text-left">
              <span className="block truncate">{f.name}</span>
              <span className="block text-[11px] text-muted font-mono">
                {[fmtDuration(durations[f.name]), fmtSize(f.size), fmtWhen(f.mtime)]
                  .filter(Boolean).join(' · ')}
              </span>
            </span>
          </button>
        ))}

        {tree?.path && !busy && !dirs.length && !(tree.files || []).length && (
          <p className="px-2 py-1.5 text-muted">
            {query ? `Nothing here matches “${query}”.` : 'No folders or videos here.'}
          </p>
        )}

        {tree?.truncated && (
          <p className="px-2 py-1.5 text-[11px] text-muted">
            Showing the first 500 — type above to narrow it.
          </p>
        )}
      </div>
    </div>
  );
}
