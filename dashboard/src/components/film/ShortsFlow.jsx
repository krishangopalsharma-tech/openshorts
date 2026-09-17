import { useCallback, useEffect, useState } from 'react';
import { Loader2, AlertCircle, AlertTriangle, Download, Sparkles, Scissors, ExternalLink, Music, Film } from 'lucide-react';
import SegmentedControl from '../ui/SegmentedControl';
import PromptPastePanel from './PromptPastePanel';
import { filmJson, fmtTime } from './filmApi';
import { getApiUrl } from '../../config';

/**
 * Movie Shorts: a montage over the film, planned in a chat window
 * (film_montage) and rendered as an ordinary clip job through the clip
 * maker, so the finished short opens in the clip editor with every tool.
 *
 * One prompt lists the film's subtitle lines with ids; the chat answers
 * with 2-5 shorts as ordered lists of line ids; the server turns each into
 * an EDL (one cut per run of lines, silences gone by construction) and
 * shows it here as a scene table before anything renders.
 */
export default function ShortsFlow({ session, onSession, renders, setRenders, onOpenJob }) {
  const [error, setError] = useState('');
  const [tracks, setTracks] = useState([]);
  const [presets, setPresets] = useState([]);
  const settings = session?.settings || {};
  const previews = session?.shorts?.previews || [];

  useEffect(() => {
    filmJson('/api/music').then((d) => setTracks(d.tracks || [])).catch(() => setTracks([]));
    filmJson('/api/caption-styles').then((d) => setPresets(Object.keys(d.presets || {}))).catch(() => setPresets(['film_pop']));
  }, []);

  const saveSetting = useCallback(async (patch) => {
    setError('');
    try { onSession(await filmJson(`/api/film/session/${session.id}/settings`, { json: patch })); } catch (e) { setError(e.message); }
  }, [session?.id, onSession]);

  const refresh = useCallback(async () => {
    try {
      const s = await filmJson(`/api/film/session/${session.id}`);
      onSession(s);
    } catch { /* ignore */ }
  }, [session?.id, onSession]);

  const render = async (index) => {
    setError('');
    try {
      const body = index === undefined ? {} : { index };
      const r = await filmJson(`/api/movieshorts/render/${session.id}`, { json: body });
      setRenders((prev) => ({ ...prev, ...Object.fromEntries(r.renders.map((k) => [k, { status: 'running', logs: [] }])) }));
    } catch (e) { setError(e.message); }
  };

  if (!session) return null;

  return (
    <div className="space-y-4">
      <div className="border border-rule rounded-card p-4 space-y-4">
        <p className="text-xs uppercase tracking-wider text-muted flex items-center gap-2"><Film size={12} /> shorts settings</p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <label className="block space-y-1">
            <span className="text-xs text-muted">shorts to ask the chat for</span>
            <SegmentedControl size="sm" value={Number(settings.shorts_count) || 3}
              onChange={(v) => saveSetting({ shorts_count: v })}
              options={[2, 3, 4, 5].map((n) => ({ value: n, label: String(n) }))} />
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-muted">frame</span>
            <SegmentedControl size="sm" value={settings.shorts_ratio || '9:16'}
              onChange={(v) => saveSetting({ shorts_ratio: v })}
              options={[{ value: '9:16', label: '9:16', hint: 'shorts / reels' }, { value: '1:1', label: '1:1', hint: 'square' }]} />
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-muted flex items-center gap-1"><Music size={12} /> music bed</span>
            <select value={settings.music_track || ''} onChange={(e) => saveSetting({ music_track: e.target.value || null })}
              className="w-full bg-paper3 border border-rule rounded-lg px-3 py-1.5 text-sm text-ink">
              <option value="">auto · picked by each short&apos;s mood</option>
              {tracks.map((t) => <option key={t.file} value={t.file}>{t.name}{t.duration ? ` · ${fmtTime(t.duration)}` : ''}</option>)}
            </select>
            <span className="readout">library under assets/music; upload more from any clip&apos;s music dialog. the bed dips under the film&apos;s lines and never stops.</span>
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-muted">music level (dB) and duck (%)</span>
            <div className="flex items-center gap-2">
              <input type="number" min={-40} max={0} step={1} value={settings.music_db ?? -16}
                onChange={(e) => saveSetting({ music_db: Number(e.target.value) })}
                className="w-24 bg-paper3 border border-rule rounded-lg px-3 py-1.5 font-mono text-sm text-ink" />
              <input type="number" min={0} max={100} step={5} value={settings.music_duck ?? 70}
                onChange={(e) => saveSetting({ music_duck: Number(e.target.value) })}
                className="w-24 bg-paper3 border border-rule rounded-lg px-3 py-1.5 font-mono text-sm text-ink" />
            </div>
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-muted">watermark text (bottom centre; empty = none)</span>
            <input type="text" value={settings.watermark_text || ''} placeholder="made by …" maxLength={60}
              onChange={(e) => saveSetting({ watermark_text: e.target.value })}
              className="w-full bg-paper3 border border-rule rounded-lg px-3 py-1.5 text-sm text-ink" />
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-muted">caption preset (one centred line; restyle later from the clip card)</span>
            <select value={settings.caption_preset || 'film_pop'} onChange={(e) => saveSetting({ caption_preset: e.target.value })}
              className="w-full bg-paper3 border border-rule rounded-lg px-3 py-1.5 text-sm text-ink">
              {(presets.length ? presets : ['film_pop']).map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </label>
        </div>
      </div>

      <PromptPastePanel
        title="Shorts plan · scenes as subtitle lines"
        hint="The chat picks 4-9 scenes per short from anywhere in the film, in the order that tells the best mini-story, ending on the line that lands. It names LINES, never times."
        promptName={`${session.title}-shorts.txt`}
        loadPrompt={() => filmJson(`/api/movieshorts/prompt/${session.id}`)}
        submit={(text, opts) => filmJson(`/api/movieshorts/plan/${session.id}`, { json: { text, force: !!opts?.force } })}
        onValid={refresh}
      />

      {previews.length > 0 && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm text-ink2">{previews.length} short{previews.length === 1 ? '' : 's'} planned · each renders as its own clip job</p>
            <button type="button" onClick={() => render()} className="px-4 py-2 rounded-lg bg-ink text-paper text-sm font-medium flex items-center gap-2">
              <Sparkles size={14} /> render all
            </button>
          </div>
          {previews.map((p, i) => (
            <ShortCard key={i} index={i} preview={p} render={renders[`shorts-${i}`]} onRender={() => render(i)} onOpenJob={onOpenJob} />
          ))}
          <p className="text-xs text-muted flex items-center gap-1"><ExternalLink size={12} /> a finished short is an ordinary clip: open it in the clip editor for captions, look, music, logos, trims and export.</p>
        </div>
      )}

      {error && <p className="text-bad text-sm flex items-center gap-2"><AlertCircle size={14} /> {error}</p>}
    </div>
  );
}

function ShortCard({ index, preview, render, onRender, onOpenJob }) {
  const running = render?.status === 'running';
  const longRuns = (preview.scenes || []).flatMap((sc) => (sc.long_runs || []).map((s) => ({ scene: sc.index, s })));
  return (
    <div className="border border-rule rounded-card p-4 space-y-3">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <p className="font-semibold text-ink">{index + 1}. {preview.title}</p>
          {preview.hook && <p className="text-sm text-ink2 mt-0.5">“{preview.hook}”</p>}
          <p className="readout mt-1">
            <span className="text-brass">{preview.mood}</span> · {fmtTime(preview.seconds)} · {preview.cuts} cuts · {(preview.scenes || []).length} scenes
            {preview.edited && <span className="ml-1 text-warn">· hand-trimmed</span>}
          </p>
          {preview.ending && <p className="text-xs text-muted mt-1">ends on: {preview.ending}</p>}
        </div>
        <button type="button" onClick={onRender} disabled={running}
          className="px-3 py-1.5 rounded-lg border border-rule text-sm text-ink2 hover:border-brass disabled:opacity-40 flex items-center gap-2">
          {running ? <Loader2 size={14} className="animate-spin" /> : <Scissors size={14} />} {render?.status === 'completed' ? 'render again' : 'render'}
        </button>
      </div>

      <table className="w-full text-xs">
        <thead>
          <tr className="text-muted text-left">
            <th className="py-1 pr-2 font-normal">#</th>
            <th className="py-1 pr-2 font-normal">scene</th>
            <th className="py-1 pr-2 font-normal text-right">film</th>
            <th className="py-1 pr-2 font-normal text-right">kept</th>
            <th className="py-1 font-normal text-right">hold</th>
          </tr>
        </thead>
        <tbody>
          {(preview.scenes || []).map((sc) => (
            <tr key={sc.index} className="border-t border-rule/60 align-top">
              <td className="py-1 pr-2 font-mono text-muted">{sc.index + 1}</td>
              <td className="py-1 pr-2 text-ink2">
                {sc.note && <span className="text-ink">{sc.note} · </span>}
                <span className="text-muted">{(sc.text || '').slice(0, 140)}{(sc.text || '').length > 140 ? '…' : ''}</span>
              </td>
              <td className="py-1 pr-2 font-mono text-muted text-right whitespace-nowrap">
                {sc.segments?.length ? `${fmtTime(sc.segments[0].start)}–${fmtTime(sc.segments[sc.segments.length - 1].end)}` : '—'}
              </td>
              <td className="py-1 pr-2 font-mono text-ink text-right">{sc.seconds}s</td>
              <td className="py-1 font-mono text-muted text-right">{sc.hold_after ? `+${sc.hold_after}s` : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {longRuns.length > 0 && (
        <p className="text-xs text-warn flex items-start gap-2"><AlertTriangle size={14} className="mt-0.5 shrink-0" />
          {longRuns.map((r) => `scene ${r.scene + 1}: ${r.s} s continuous`).join(' · ')} — over 8 s of one stretch is what fingerprinting catches; ask the chat to drop a line there.
        </p>
      )}

      {render && (
        <div className="space-y-2">
          {render.status === 'running' && <p className="text-sm flex items-center gap-2"><Loader2 size={14} className="animate-spin text-brass" /> rendering… (cut, reframe, music, watermark, captions)</p>}
          {render.status === 'failed' && <p className="text-sm text-bad flex items-center gap-2"><AlertCircle size={14} /> {render.error}</p>}
          {render.logs?.length > 0 && render.status !== 'completed' && (
            <pre className="text-[11px] font-mono text-muted bg-paper3 rounded p-2 max-h-24 overflow-y-auto">{render.logs.slice(-6).join('\n')}</pre>
          )}
          {render.status === 'completed' && render.output && (
            <div className="flex items-start gap-4 flex-wrap">
              <video src={getApiUrl(render.output)} controls className="bg-black rounded w-44 h-[312px]" />
              <div className="space-y-2 text-sm">
                <p className="readout">{fmtTime(render.seconds)} · {render.cuts} cuts · job {String(render.job_id || '').slice(0, 8)}</p>
                {render.job_id && onOpenJob && (
                  <button type="button" onClick={() => onOpenJob(render.job_id)}
                    className="px-3 py-1.5 rounded-lg bg-ink text-paper text-sm flex items-center gap-2">
                    <ExternalLink size={14} /> open in clip editor
                  </button>
                )}
                <a href={getApiUrl(render.output)} download className="text-xs text-ink2 hover:text-brass inline-flex items-center gap-1"><Download size={12} /> download</a>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
