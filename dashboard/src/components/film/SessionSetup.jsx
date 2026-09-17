import { useEffect, useState } from 'react';
import { Loader2, AlertCircle, Clock, FolderClock, Trash2 } from 'lucide-react';
import LocalPathPicker from './LocalPathPicker';
import { filmJson, fmtTime } from './filmApi';

/**
 * Step 1 of both film tabs: name the film and its subtitle file on the
 * server's disk, create the session, show the detected SRT offset with an
 * override. Also lists earlier sessions of this kind so a film's hook list
 * survives the sitting that produced it.
 */
export default function SessionSetup({ kind, session, onSession, settings, children }) {
  const [video, setVideo] = useState('');
  const [subs, setSubs] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [sessions, setSessions] = useState([]);
  const [offsetInput, setOffsetInput] = useState('');

  const refreshList = async () => {
    try {
      const data = await filmJson('/api/film/sessions');
      setSessions((data.sessions || []).filter((s) => s.kind === kind));
    } catch { /* self-host only; ignore */ }
  };
  useEffect(() => { refreshList(); }, [kind]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (session) setOffsetInput(String(session.offset ?? 0));
  }, [session]);

  // Poll the session while the offset probe is still running.
  useEffect(() => {
    if (!session || session.offset_probe || session.offset_set_by_user) return undefined;
    const t = setInterval(async () => {
      try {
        const s = await filmJson(`/api/film/session/${session.id}`);
        if (s.offset_probe) { onSession(s); clearInterval(t); }
      } catch { clearInterval(t); }
    }, 4000);
    return () => clearInterval(t);
  }, [session, onSession]);

  const create = async () => {
    setBusy(true);
    setError('');
    try {
      const s = await filmJson('/api/film/session', {
        json: { kind, video_path: video, subtitle_path: subs, settings: settings || {}, probe_offset: true },
      });
      onSession(s);
      refreshList();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const open = async (id) => {
    setError('');
    try { onSession(await filmJson(`/api/film/session/${id}`)); } catch (e) { setError(e.message); }
  };

  const remove = async (id) => {
    if (!window.confirm('Delete this session and its rendered parts?')) return;
    try {
      await filmJson(`/api/film/session/${id}`, { method: 'DELETE' });
      if (session?.id === id) onSession(null);
      refreshList();
    } catch (e) { setError(e.message); }
  };

  const saveOffset = async () => {
    const v = parseFloat(offsetInput);
    if (Number.isNaN(v)) return;
    try { onSession(await filmJson(`/api/film/session/${session.id}/offset`, { json: { offset: v } })); } catch (e) { setError(e.message); }
  };

  if (session) {
    const probe = session.offset_probe;
    return (
      <div className="space-y-4">
        <div className="border border-rule rounded-card p-4 grid grid-cols-2 md:grid-cols-4 gap-3">
          <Stat label="film" value={session.title} mono={false} />
          <Stat label="runtime" value={fmtTime(session.duration)} />
          <Stat label="cues" value={session.cue_count?.toLocaleString()} />
          <Stat label="wall (75%)" value={fmtTime(session.duration * 0.75)} />
        </div>
        <div className="border border-rule rounded-card p-4 space-y-2">
          <div className="flex items-center gap-2 text-sm text-ink">
            <Clock size={14} className="text-brass" /> subtitle offset
            {!probe && !session.offset_set_by_user && <Loader2 size={14} className="animate-spin text-muted" />}
          </div>
          {probe && (
            <p className="readout">
              {probe.error ? `probe failed: ${probe.error}`
                : `probe heard ${probe.matches} matching words · offset ${probe.offset >= 0 ? '+' : ''}${probe.offset}s · confidence ${Math.round((probe.confidence || 0) * 100)}%`}
              {probe.confidence < 0.5 && !probe.error && ' · low confidence, check a caption against the picture'}
            </p>
          )}
          <div className="flex items-center gap-2">
            <input type="number" step="0.1" value={offsetInput} onChange={(e) => setOffsetInput(e.target.value)}
              className="w-28 bg-paper3 border border-rule rounded-lg px-3 py-1.5 text-sm font-mono text-ink" />
            <span className="text-xs text-muted">seconds added to the SRT (positive = captions later)</span>
            <button type="button" onClick={saveOffset}
              className="px-3 py-1.5 rounded-lg border border-rule text-sm text-ink2 hover:border-brass">apply</button>
          </div>
        </div>
        {children}
        {error && <p className="text-bad text-sm flex items-center gap-2"><AlertCircle size={14} /> {error}</p>}
        <button type="button" onClick={() => onSession(null)} className="text-xs text-muted hover:text-ink underline">
          start another film
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="border border-rule rounded-card p-4 space-y-4">
        <LocalPathPicker kind="video" label="the film, on this machine" value={video} onChange={setVideo} />
        <LocalPathPicker kind="subtitle" label="its subtitle file (.srt / .vtt)" value={subs} onChange={setSubs}
          startPath={video ? video.replace(/[\\/][^\\/]*$/, '') : ''} />
        <p className="readout">
          nothing is uploaded: the backend reads both files where they are. the SRT is the transcript, so
          no whisper pass over a two-hour film; only a 60 s slice is heard to check the SRT lines up.
        </p>
        {children}
        <button type="button" onClick={create} disabled={busy || !video || !subs}
          className="px-4 py-2 rounded-lg bg-ink text-paper text-sm font-medium disabled:opacity-40 flex items-center gap-2">
          {busy && <Loader2 size={14} className="animate-spin" />} open session
        </button>
        {error && <p className="text-bad text-sm flex items-center gap-2"><AlertCircle size={14} /> {error}</p>}
      </div>
      {sessions.length > 0 && (
        <div className="border border-rule rounded-card p-4">
          <p className="text-xs uppercase tracking-wider text-muted mb-2 flex items-center gap-2"><FolderClock size={14} /> earlier sessions</p>
          <ul className="divide-y divide-rule">
            {sessions.map((s) => (
              <li key={s.id} className="flex items-center gap-3 py-2 text-sm">
                <button type="button" onClick={() => open(s.id)} className="flex-1 text-left text-ink hover:text-brass truncate">
                  {s.title}
                </button>
                <span className="font-mono text-xs text-muted">{fmtTime(s.duration)}</span>
                {s.hooks > 0 && <span className="font-mono text-xs text-muted">{s.hooks} hooks</span>}
                {s.renders > 0 && <span className="font-mono text-xs text-muted">{s.renders} renders</span>}
                <button type="button" onClick={() => remove(s.id)} className="text-muted hover:text-bad" title="delete">
                  <Trash2 size={14} />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function Stat({ label, value, mono = true, tone = '' }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-muted">{label}</p>
      <p className={`${mono ? 'font-mono' : ''} text-sm text-ink truncate ${tone}`}>{value ?? '—'}</p>
    </div>
  );
}
