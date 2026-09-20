import { useEffect, useState } from 'react';
import { Folder, FolderOpen } from 'lucide-react';
import LocalFileBrowser from '../LocalFileBrowser';

/**
 * Walk the server's own disk to name a file the browser cannot see the path
 * of (File.path is Electron, not the web). Same endpoint — and now the same
 * listing component — as the clip generator's local panel, so the remembered
 * folder, the filter and the frame previews are shared rather than written
 * twice and drifting; `kind` picks the extension filter server-side.
 */
export default function LocalPathPicker({ kind = 'video', value, onChange, label }) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState(value || '');

  useEffect(() => { setTyped(value || ''); }, [value]);

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
        <button type="button" onClick={() => setOpen(!open)}
          className="px-3 py-2 rounded-lg border border-rule text-sm text-ink2 hover:border-brass flex items-center gap-2">
          {open ? <FolderOpen size={16} /> : <Folder size={16} />} browse
        </button>
      </div>
      {open && (
        <div className="border border-rule rounded-lg bg-paper3 p-2 h-80 flex flex-col">
          <LocalFileBrowser
            kind={kind}
            onPick={(path) => { setTyped(path); onChange(path); setOpen(false); }}
          />
        </div>
      )}
    </div>
  );
}
