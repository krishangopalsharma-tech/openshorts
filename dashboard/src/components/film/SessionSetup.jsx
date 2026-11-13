import { useEffect, useState } from 'react';
import { Loader2, AlertCircle, Clock, FolderClock, Trash2, Mic, RefreshCw, CheckCircle2, XCircle } from 'lucide-react';
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

  // Poll the session while whisper transcribes the film or the sync probes run.
  const transcribing = ['queued', 'running'].includes(session?.transcribing?.status);
  const syncing = session?.sync?.status === 'running';
  useEffect(() => {
    if (!session) return undefined;
    const waiting = transcribing || syncing
      || (!session.offset_probe && !session.offset_set_by_user && session.transcribing?.status !== 'failed');
    if (!waiting) return undefined;
    const t = setInterval(async () => {
      try {
        const s = await filmJson(`/api/film/session/${session.id}`);
        onSession(s);
      } catch { clearInterval(t); }
    }, 4000);
    return () => clearInterval(t);
  }, [session, onSession, transcribing, syncing]);

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

  const resync = async () => {
    setError('');
    try { onSession(await filmJson(`/api/film/session/${session.id}/sync`, { method: 'POST' })); } catch (e) { setError(e.message); }
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
          {kind === 'recap' && <Stat label="wall (75%)" value={fmtTime(session.duration * 0.75)} />}
        </div>
        {session.transcript_source === 'whisper' ? (
          <WhisperStatus session={session} />
        ) : (
        <div className="border border-rule rounded-card p-4 space-y-3">
          <div className="flex items-center gap-2 text-sm text-ink flex-wrap">
            <Clock size={14} className="text-brass" /> subtitle sync · checked against the audio by whisper at five points
            {(syncing || (!probe && !session.offset_set_by_user)) && <Loader2 size={14} className="animate-spin text-muted" />}
            <button type="button" onClick={resync} disabled={syncing}
              className="ml-auto px-2.5 py-1 rounded-lg border border-rule text-xs text-ink2 hover:border-brass disabled:opacity-40 flex items-center gap-1">
              <RefreshCw size={12} /> check again
            </button>
          </div>
          <SyncTable sync={session.sync} duration={session.duration} />
          {probe && (
            <p className="readout">
              {probe.error ? `probe failed: ${probe.error}` : syncSummary(session.sync?.model, probe)}
              {session.offset_set_by_user && ' · OVERRIDDEN by the offset typed below'}
            </p>
          )}
          <div className="flex items-center gap-2 flex-wrap">
            <input type="number" step="0.1" value={offsetInput} onChange={(e) => setOffsetInput(e.target.value)}
              className="w-28 bg-paper3 border border-rule rounded-lg px-3 py-1.5 text-sm font-mono text-ink" />
            <span className="text-xs text-muted">or type one offset for the whole film (seconds, positive = captions later)</span>
            <button type="button" onClick={saveOffset}
              className="px-3 py-1.5 rounded-lg border border-rule text-sm text-ink2 hover:border-brass">apply</button>
          </div>
        </div>
        )}
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
        <LocalPathPicker kind="subtitle" label="its subtitle file (.srt / .vtt) · optional" value={subs} onChange={setSubs}
          startPath={video ? video.replace(/[\\/][^\\/]*$/, '') : ''} />
        <p className="readout">
          nothing is uploaded: the backend reads the files where they are. with an SRT, whisper checks it
          against the audio at five points across the film and corrects a constant delay, a drift (wrong
          frame rate) or a jump (another cut of the film). without one, whisper transcribes the whole film
          first, which takes a while on a two-hour film.
        </p>
        {children}
        <button type="button" onClick={create} disabled={busy || !video}
          className="px-4 py-2 rounded-lg bg-ink text-paper text-sm font-medium disabled:opacity-40 flex items-center gap-2">
          {busy && <Loader2 size={14} className="animate-spin" />} {subs ? 'open session' : 'open session · transcribe with whisper'}
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


function syncSummary(model, probe) {
  const kind = model?.kind || probe?.kind;
  if (!kind || kind === 'none') return 'no probe heard enough matching words: the SRT is used as it is; check a caption against the picture';
  if (kind === 'constant') return `constant delay · ${model.offset >= 0 ? '+' : ''}${model.offset}s applied to every line`;
  if (kind === 'linear') {
    const drift = model.rate * 3600;
    return `drift · ${model.offset >= 0 ? '+' : ''}${model.offset}s at the start, ${drift >= 0 ? '+' : ''}${drift.toFixed(2)}s more every hour (frame-rate mismatch); each line moved by its own amount`;
  }
  return 'jump · the delay changes partway through (an SRT from another cut); each stretch moved by its own offset';
}

function SyncTable({ sync, duration }) {
  const points = sync?.points || [];
  if (!points.length) return null;
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-muted text-left">
          <th className="py-1 pr-2 font-normal">at</th>
          <th className="py-1 pr-2 font-normal text-right">delay</th>
          <th className="py-1 pr-2 font-normal text-right">words heard · agreeing</th>
          <th className="py-1 font-normal text-right">used</th>
        </tr>
      </thead>
      <tbody>
        {points.map((p, i) => (
          <tr key={i} className="border-t border-rule/60">
            <td className="py-1 pr-2 font-mono text-ink2">{fmtTime(p.at)} <span className="text-muted">({Math.round((p.at / (duration || 1)) * 100)}%)</span></td>
            <td className="py-1 pr-2 font-mono text-ink text-right">{p.offset >= 0 ? '+' : ''}{p.offset}s</td>
            <td className="py-1 pr-2 font-mono text-muted text-right">{p.matches} · {Math.round((p.support ?? p.confidence ?? 0) * 100)}% agree</td>
            <td className="py-1 text-right">
              {p.ok ? <CheckCircle2 size={13} className="inline text-ok" /> : <span title={p.error || 'too few matching words (music or silence?)'}><XCircle size={13} className="inline text-muted" /></span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function WhisperStatus({ session }) {
  const tr = session.transcribing || {};
  const running = tr.status === 'queued' || tr.status === 'running';
  const elapsed = tr.started ? (tr.finished || Date.now() / 1000) - tr.started : 0;
  return (
    <div className="border border-rule rounded-card p-4 space-y-1">
      <div className="flex items-center gap-2 text-sm text-ink">
        <Mic size={14} className="text-brass" /> transcript from whisper (no SRT)
        {running && <Loader2 size={14} className="animate-spin text-muted" />}
      </div>
      {running && <p className="readout">transcribing the whole film · {fmtTime(elapsed)} so far · roughly a tenth of the runtime on an 8 GB card; the prompt unlocks when it is done</p>}
      {tr.status === 'done' && <p className="readout">{session.cue_count} lines heard in {fmtTime(elapsed)} · timings come from the audio, nothing to sync</p>}
      {tr.status === 'failed' && <p className="text-bad text-sm flex items-center gap-2"><AlertCircle size={14} /> transcription failed: {tr.error}</p>}
    </div>
  );
}
