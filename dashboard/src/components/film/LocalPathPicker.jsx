import { useEffect, useState } from 'react';
import { Folder, FolderOpen, FileVideo, FileText, ChevronUp, HardDrive, Loader2 } from 'lucide-react';
import { apiFetch } from '../../lib/api';

/**
 * Walk the server's own disk to name a file the browser cannot see the path
 * of (File.path is Electron, not the web). Same endpoint the clip generator's
 * local panel uses; `kind` picks the extension filter server-side.
 */
export default function LocalPathPicker({ kind = 'video', value, onChange, label, startPath }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [tree, setTree] = useState(null);
  const [typed, setTyped] = useState(value || '');

  useEffect(() => { setTyped(value || ''); }, [value]);

  const load = async (path) => {
    setBusy(true);
    setError('');
    try {
      const q = new URLSearchParams();
      if (path) q.set('path', path);
      q.set('kind', kind);
      const res = await apiFetch(`/api/local/browse?${q.toString()}`);
      const data = await res.json();
      if (!res.ok) { setError(data.detail || 'Could not read that folder.'); return; }
      setTree(data);
    } catch {
      setError('Could not reach the backend.');
    } finally {
      setBusy(false);
    }
  };

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && !tree) load(startPath || (value ? value.replace(/[\\/][^\\/]*$/, '') : ''));
  };

  const join = (dir, name) => {
    const sep = tree?.sep || '/';
    return dir.endsWith(sep) ? dir + name : dir + sep + name;
  };

  const Icon = kind === 'video' ? FileVideo : FileText;

  return (
    <div className="space-y-2">
      {label && <p className="text-xs uppercase tracking-wider text-muted">{label}</p>}
      <div className="flex gap-2">
        <input
          type="text"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          onBlur={() => typed !== value && onChange(typed.trim())}
          onKeyDown={(e) => { if (e.key === 'Enter') onChange(typed.trim()); }}
          placeholder={kind === 'video' ? 'D:\\films\\movie.mkv' : 'D:\\films\\movie.srt'}
          className="flex-1 min-w-0 bg-paper3 border border-rule rounded-lg px-3 py-2 text-sm font-mono text-ink"
        />
        <button type="button" onClick={toggle}
          className="px-3 py-2 rounded-lg border border-rule text-sm text-ink2 hover:border-brass flex items-center gap-2">
          {open ? <FolderOpen size={16} /> : <Folder size={16} />} browse
        </button>
      </div>
      {open && (
        <div className="border border-rule rounded-lg bg-paper3 text-sm max-h-72 overflow-y-auto custom-scrollbar">
          <div className="flex items-center gap-2 px-3 py-2 border-b border-rule text-xs font-mono text-muted">
            {busy ? <Loader2 size={14} className="animate-spin" /> : <HardDrive size={14} />}
            <span className="truncate flex-1">{tree?.path || 'drives'}</span>
            {tree?.parent && (
              <button type="button" onClick={() => load(tree.parent)} className="hover:text-ink flex items-center gap-1">
                <ChevronUp size={14} /> up
              </button>
            )}
          </div>
          {error && <p className="px-3 py-2 text-bad">{error}</p>}
          {tree && !tree.path && (tree.drives || []).map((d) => (
            <button key={d} type="button" onClick={() => load(d)}
              className="w-full text-left px-3 py-1.5 hover:bg-paper2 flex items-center gap-2 text-ink2">
              <HardDrive size={14} /> {d}
            </button>
          ))}
          {tree && tree.path && (tree.dirs || []).map((d) => (
            <button key={d} type="button" onClick={() => load(join(tree.path, d))}
              className="w-full text-left px-3 py-1.5 hover:bg-paper2 flex items-center gap-2 text-ink2">
              <Folder size={14} className="text-muted" /> {d}
            </button>
          ))}
          {tree && tree.path && (tree.files || []).map((f) => (
            <button key={f.name} type="button"
              onClick={() => { const p = join(tree.path, f.name); setTyped(p); onChange(p); setOpen(false); }}
              className="w-full text-left px-3 py-1.5 hover:bg-paper2 flex items-center gap-2 text-ink">
              <Icon size={14} className="text-brass" />
              <span className="truncate flex-1">{f.name}</span>
              <span className="font-mono text-xs text-muted">{(f.size / 1048576).toFixed(0)} MB</span>
            </button>
          ))}
          {tree && tree.path && !tree.dirs?.length && !tree.files?.length && !busy && (
            <p className="px-3 py-2 text-muted">nothing here of that type</p>
          )}
        </div>
      )}
    </div>
  );
}
