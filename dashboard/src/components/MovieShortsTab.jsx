import { useCallback, useEffect, useState } from 'react';
import { Film, Loader2, AlertCircle, Music, RefreshCw, Download, Sparkles, ExternalLink, Upload } from 'lucide-react';
import StepIndicator from './ui/StepIndicator';
import SegmentedControl from './ui/SegmentedControl';
import SessionSetup, { Stat } from './film/SessionSetup';
import PromptPastePanel from './film/PromptPastePanel';
import { filmJson, fmtTime, useRenderPoll } from './film/filmApi';
import { getApiUrl } from '../config';

const STEPS = ['Film + SRT', 'Settings', 'Hooks', 'Beats', 'Shots', 'Render'];
const RATIOS = [{ value: '1:1', label: '1:1', hint: 'keeps 42% of a 2.39:1 frame' }, { value: '9:16', label: '9:16', hint: 'keeps 23%' }];
const GRADES = [{ value: 'washed', label: 'washed' }, { value: 'warm', label: 'warm' }, { value: 'cold', label: 'cold' }, { value: 'none', label: 'neutral' }];
const SYNC = [{ value: 'beat', label: 'beat', hint: 'cuts on the bed\'s grid' }, { value: 'energy', label: 'energy' }, { value: 'free', label: 'free', hint: 'the reference look' }];
const LICENSES = [{ value: 'owned', label: 'owned' }, { value: 'licensed', label: 'licensed' }, { value: 'affiliate_program', label: 'affiliate' }, { value: 'unlicensed', label: 'unlicensed' }];

const SCALE_TONE = { 1: 'bg-rule2', 1.15: 'bg-rule2', 1.25: 'bg-brass/40', 1.45: 'bg-brass/60', 1.55: 'bg-brass/80', 1.7: 'bg-brass' };

export default function MovieShortsTab() {
  const [session, setSession] = useState(null);
  const [step, setStep] = useState(0);
  const [settings, setSettings] = useState({ ratio: '1:1', grade: 'washed', beat_sync: 'beat', hook_count: 8, target_seconds: 120, source_license: 'unlicensed' });
  const [hook, setHook] = useState(null);
  const [shots, setShots] = useState(null);
  const [shotsBusy, setShotsBusy] = useState(false);
  const [renders, setRenders] = useState({});
  const [error, setError] = useState('');
  const [selectedShot, setSelectedShot] = useState(null);

  const onSession = useCallback((s) => {
    setSession(s);
    setRenders(s?.renders || {});
    if (s) {
      setSettings((prev) => ({ ...prev, ...(s.settings || {}) }));
      setStep(s.hooks ? 2 : 1);
    } else {
      setStep(0);
      setHook(null);
      setShots(null);
    }
  }, []);

  useRenderPoll(session?.id, renders, setRenders);

  const saveSettings = async (patch) => {
    const next = { ...settings, ...patch };
    setSettings(next);
    if (session) {
      try { setSession(await filmJson(`/api/film/session/${session.id}/settings`, { json: patch })); } catch (e) { setError(e.message); }
    }
  };

  const hooks = session?.hooks?.hooks || [];
  const beatsFor = (h) => session?.beats?.[String(h.index)];

  const loadShots = async (rebuild = false) => {
    if (!session || !hook) return;
    setShotsBusy(true);
    setError('');
    try {
      setShots(await filmJson(`/api/movieshorts/shots/${session.id}/${hook.index}${rebuild ? '?rebuild=true' : ''}`));
    } catch (e) { setError(e.message); } finally { setShotsBusy(false); }
  };

  useEffect(() => { if (step === 4 && hook && !shots) loadShots(); }, [step, hook]); // eslint-disable-line react-hooks/exhaustive-deps

  const editShot = async (id, patch) => {
    if (!shots) return;
    try {
      const data = await filmJson(`/api/movieshorts/shots/${session.id}/${hook.index}`, { json: { shots: [{ id, ...patch }] } });
      setShots(data);
    } catch (e) { setError(e.message); }
  };

  const render = async () => {
    setError('');
    try {
      await filmJson(`/api/movieshorts/render/${session.id}/${hook.index}`, { json: {} });
      setRenders((r) => ({ ...r, [`short-${hook.index}`]: { status: 'running', logs: [] } }));
      setStep(5);
    } catch (e) { setError(e.message); }
  };

  const refreshSession = async () => { try { onSession(await filmJson(`/api/film/session/${session.id}`)); } catch { /* ignore */ } };

  const renderFor = hook ? renders[`short-${hook.index}`] : null;

  return (
    <div className="h-full overflow-y-auto custom-scrollbar p-4 sm:p-6 md:p-10 animate-fade">
      <div className="max-w-5xl mx-auto space-y-6">
        <header className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold text-ink flex items-center gap-2"><Film size={20} className="text-brass" /> Movie Shorts</h1>
            <p className="text-sm text-muted mt-1 max-w-2xl">
              One film, many standalone two-minute shorts: the model picks angles and story beats in a chat window,
              this machine cuts ~86 fast shots with the film&apos;s own dialogue as captions and a music bed under it.
            </p>
          </div>
        </header>
        <StepIndicator steps={STEPS} current={step} onStepClick={(i) => { if (session || i === 0) setStep(i); }} />

        {step === 0 && (
          <SessionSetup kind="shorts" session={session} onSession={onSession} settings={settings}>
            {session && <button type="button" onClick={() => setStep(1)} className="px-4 py-2 rounded-lg bg-ink text-paper text-sm">continue</button>}
          </SessionSetup>
        )}

        {step === 1 && session && (
          <div className="border border-rule rounded-card p-4 space-y-5">
            <Field label="output ratio"><SegmentedControl options={RATIOS} value={settings.ratio} onChange={(v) => saveSettings({ ratio: v })} size="sm" /></Field>
            <Field label="grade"><SegmentedControl options={GRADES} value={settings.grade} onChange={(v) => saveSettings({ grade: v })} size="sm" /></Field>
            <Field label="cut rhythm"><SegmentedControl options={SYNC} value={settings.beat_sync} onChange={(v) => saveSettings({ beat_sync: v })} size="sm" /></Field>
            <Field label="source licence (recorded on the checklist, changes nothing in the render)">
              <SegmentedControl options={LICENSES} value={settings.source_license} onChange={(v) => saveSettings({ source_license: v })} size="sm" />
            </Field>
            <div className="grid grid-cols-2 gap-4">
              <Field label="hooks to ask for">
                <input type="number" min={3} max={12} value={settings.hook_count}
                  onChange={(e) => saveSettings({ hook_count: Number(e.target.value) })}
                  className="w-24 bg-paper3 border border-rule rounded-lg px-3 py-1.5 text-sm font-mono text-ink" />
              </Field>
              <Field label="finished length (s)">
                <input type="number" min={90} max={150} step={10} value={settings.target_seconds}
                  onChange={(e) => saveSettings({ target_seconds: Number(e.target.value) })}
                  className="w-24 bg-paper3 border border-rule rounded-lg px-3 py-1.5 text-sm font-mono text-ink" />
              </Field>
            </div>
            <button type="button" onClick={() => setStep(2)} className="px-4 py-2 rounded-lg bg-ink text-paper text-sm">continue</button>
          </div>
        )}

        {step === 2 && session && (
          <div className="space-y-4">
            <PromptPastePanel
              title="Pass 1 · hooks"
              hint="One angle on the whole film per hook. Copy the prompt, paste the JSON back."
              promptName={`${session.title}-hooks-prompt.txt`}
              loadPrompt={() => filmJson(`/api/movieshorts/prompt/${session.id}?pass=hooks`)}
              submit={(text) => filmJson(`/api/movieshorts/hooks/${session.id}`, { json: { text } })}
              onValid={refreshSession}
            />
            {hooks.length > 0 && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {hooks.map((h) => {
                  const done = !!beatsFor(h);
                  const r = renders[`short-${h.index}`];
                  return (
                    <button key={h.index} type="button" onClick={() => { setHook(h); setShots(null); setStep(done ? 4 : 3); }}
                      className="text-left border border-rule rounded-card p-4 hover:border-brass transition-colors space-y-2">
                      <div className="flex items-center justify-between gap-2">
                        <p className="font-semibold text-ink">{h.title}</p>
                        <span className="font-mono text-[10px] text-muted">#{h.strength}</span>
                      </div>
                      <p className="text-xs text-ink2">{h.angle}</p>
                      <div className="flex flex-wrap gap-1.5">
                        <Pill tone="brass">{h.mood}</Pill>
                        {done && <Pill tone="ok">beats ✓</Pill>}
                        {r?.status === 'completed' && <Pill tone="ok">rendered</Pill>}
                        {r?.status === 'running' && <Pill>rendering…</Pill>}
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {step === 3 && session && hook && (
          <div className="space-y-4">
            <HookHeader hook={hook} />
            <PromptPastePanel
              key={hook.index}
              title={`Pass 2 · beats for "${hook.title}"`}
              hint="14-18 story beats, each quoting real subtitle lines. Invented dialogue is refused."
              promptName={`${session.title}-hook${hook.index}-beats-prompt.txt`}
              loadPrompt={() => filmJson(`/api/movieshorts/prompt/${session.id}?pass=beats&hook=${hook.index}`)}
              submit={(text, opts) => filmJson(`/api/movieshorts/beats/${session.id}/${hook.index}`, { json: { text, force: !!opts?.force } })}
              onValid={async () => { await refreshSession(); setShots(null); setStep(4); }}
            />
          </div>
        )}

        {step === 4 && session && hook && (
          <div className="space-y-4">
            <HookHeader hook={hook} />
            {shotsBusy && <p className="text-sm text-muted flex items-center gap-2"><Loader2 size={14} className="animate-spin" /> expanding beats into shots…</p>}
            {shots && (
              <>
                <div className="border border-rule rounded-card p-4 grid grid-cols-2 md:grid-cols-6 gap-3">
                  <Stat label="shots" value={shots.summary.shots} />
                  <Stat label="mean shot" value={`${shots.summary.mean_shot_finished}s`} tone={shots.summary.mean_shot_finished >= 1.3 && shots.summary.mean_shot_finished <= 1.7 ? 'text-ok' : 'text-warn'} />
                  <Stat label="footage" value={fmtTime(shots.summary.source_seconds)} />
                  <Stat label="composites" value={shots.summary.composites} />
                  <Stat label="rhythm tier" value={shots.rhythm?.tier} tone={shots.rhythm?.tier === 'beat' ? 'text-ok' : ''} />
                  <Stat label="nudged" value={shots.rhythm?.boundaries ? `${shots.rhythm.nudged + (shots.rhythm.moved_a_beat || 0)} / ${shots.rhythm.boundaries}` : '—'}
                    tone={shots.rhythm?.tempo_fights_dialogue ? 'text-warn' : ''} />
                </div>
                <MusicLine music={shots.music} mood={hook.mood} onAdded={() => loadShots(true)} />
                <div className="border border-rule rounded-card p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <p className="text-xs uppercase tracking-wider text-muted">shot strip · click a shot to change its crop</p>
                    <button type="button" onClick={() => loadShots(true)} className="text-xs text-muted hover:text-ink flex items-center gap-1"><RefreshCw size={12} /> rebuild</button>
                  </div>
                  <div className="flex gap-px h-14 rounded overflow-hidden bg-paper3">
                    {shots.shots.map((s) => (
                      <button key={s.id} type="button" onClick={() => setSelectedShot(s)} title={`${fmtTime(s.start)} · ×${s.scale}`}
                        style={{ flex: s.end - s.start }}
                        className={`${s.composite ? 'bg-ok/70' : SCALE_TONE[s.scale] || 'bg-rule2'} ${selectedShot?.id === s.id ? 'ring-2 ring-ink' : ''} ${s.key_line ? 'border-b-2 border-ink' : ''} hover:opacity-80`} />
                    ))}
                  </div>
                  <p className="readout">lighter = wider, brass = punched in, green = composite, underline = the beat&apos;s key line</p>
                  {selectedShot && (
                    <div className="flex flex-wrap items-center gap-3 text-sm border-t border-rule pt-3">
                      <span className="font-mono text-xs text-muted">shot {selectedShot.id} · beat {selectedShot.beat + 1} · {fmtTime(selectedShot.start)} · {(selectedShot.end - selectedShot.start).toFixed(2)}s src</span>
                      <label className="flex items-center gap-2">scale
                        <input type="range" min={1} max={1.8} step={0.05} value={selectedShot.scale}
                          onChange={(e) => { const v = Number(e.target.value); setSelectedShot({ ...selectedShot, scale: v }); }}
                          onMouseUp={(e) => editShot(selectedShot.id, { scale: Number(e.target.value) })}
                          onTouchEnd={() => editShot(selectedShot.id, { scale: selectedShot.scale })} />
                        <span className="font-mono text-xs">×{Number(selectedShot.scale).toFixed(2)}</span>
                      </label>
                      <label className="flex items-center gap-2">
                        <input type="checkbox" checked={!!selectedShot.composite}
                          onChange={(e) => { setSelectedShot({ ...selectedShot, composite: e.target.checked }); editShot(selectedShot.id, { composite: e.target.checked }); }} />
                        composite
                      </label>
                      <label className="flex items-center gap-2">
                        <input type="checkbox" checked={!!selectedShot.blur}
                          onChange={(e) => { const blur = e.target.checked ? { x: 0.3, y: 0.3, w: 0.4, h: 0.4 } : null; setSelectedShot({ ...selectedShot, blur }); editShot(selectedShot.id, { blur }); }} />
                        blur centre (advertiser safety)
                      </label>
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-3">
                  <button type="button" onClick={render} disabled={renderFor?.status === 'running'}
                    className="px-4 py-2 rounded-lg bg-ink text-paper text-sm font-medium disabled:opacity-40 flex items-center gap-2">
                    <Sparkles size={14} /> render short
                  </button>
                  <span className="text-xs text-muted">{settings.ratio} · {settings.grade} · film_pop captions · 60 fps</span>
                </div>
              </>
            )}
          </div>
        )}

        {step === 5 && session && hook && (
          <div className="space-y-4">
            <HookHeader hook={hook} />
            <RenderCard render={renderFor} settings={settings} />
            <button type="button" onClick={() => setStep(2)} className="text-sm text-muted hover:text-ink underline">back to hooks</button>
          </div>
        )}

        {error && <p className="text-bad text-sm flex items-center gap-2"><AlertCircle size={14} /> {error}</p>}
      </div>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wider text-muted mb-2">{label}</p>
      {children}
    </div>
  );
}

function Pill({ children, tone = '' }) {
  const cls = tone === 'ok' ? 'border-ok text-ok' : tone === 'brass' ? 'border-brass text-brass' : 'border-rule text-muted';
  return <span className={`font-mono text-[10px] px-2 py-0.5 rounded-full border ${cls}`}>{children}</span>;
}

function HookHeader({ hook }) {
  return (
    <div className="border-l-2 border-brass pl-3">
      <p className="font-semibold text-ink">{hook.title}</p>
      <p className="text-xs text-ink2">{hook.angle} <span className="text-muted">· pays off: {hook.payoff}</span></p>
    </div>
  );
}

const MOODS = ['tense', 'ominous', 'sad', 'epic', 'romantic', 'eerie', 'driving'];

function MusicLine({ music, mood, onAdded }) {
  const [hint, setHint] = useState(null);
  const [open, setOpen] = useState(!music);
  const [file, setFile] = useState(null);
  const [form, setForm] = useState({ mood: mood || 'any', source: '', licence: '', url: '', attribution: '', attribution_required: false });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    if (!mood) return;
    filmJson(`/api/film/music-hint?mood=${encodeURIComponent(mood)}`).then(setHint).catch(() => {});
    setForm((f) => ({ ...f, mood }));
  }, [mood]);

  const upload = async () => {
    if (!file) return;
    setBusy(true);
    setMsg('');
    const fd = new FormData();
    fd.append('file', file);
    Object.entries(form).forEach(([k, v]) => fd.append(k, typeof v === 'boolean' ? (v ? 'true' : 'false') : v));
    try {
      const r = await filmJson('/api/film/library/upload', { method: 'POST', body: fd });
      const t = r.track || {};
      setMsg(`added ${t.file}: ${Math.round(t.bpm || 0)} BPM, confidence ${t.beat_confidence}${t.flags?.length ? ` · ${t.flags.join('; ')}` : ''}`);
      setFile(null);
      onAdded?.();
    } catch (e) {
      setMsg(e.message);
    } finally {
      setBusy(false);
    }
  };

  const t = music?.track || {};
  return (
    <div className="text-sm border border-rule rounded-card p-3 space-y-2">
      {music ? (
        <p className="flex items-center gap-2 text-ink"><Music size={14} className="text-brass" /> {t.file} <span className="font-mono text-xs text-muted">{Math.round(t.bpm || 0)} BPM · conf {t.beat_confidence} · {music.mood_used}{music.loop ? ' · looped' : ''}</span></p>
      ) : (
        <p className="flex items-center gap-2 text-warn"><Music size={14} /> no track for this short yet: the library is empty. the short renders without a bed until you add one.</p>
      )}
      {music?.fallback && (
        <p className="text-xs text-warn">used <b>{music.mood_used}</b>: the library has nothing usable for <b>{mood}</b>.</p>
      )}
      {t.attribution_required && <p className="text-xs text-muted">credit required in the description: {t.attribution || t.url}</p>}

      <button type="button" onClick={() => setOpen((o) => !o)} className="text-xs text-ink2 hover:text-ink underline">
        {open ? 'hide' : 'find or add music'} for <b>{mood}</b>
      </button>

      {open && (
        <div className="space-y-3 border-t border-rule pt-3">
          {hint && (
            <div className="space-y-1">
              <p className="text-xs uppercase tracking-wider text-muted">what to search for ({mood})</p>
              <p className="text-xs text-ink2">
                terms: {hint.primary?.join(', ')}{hint.alternates?.length ? `; also ${hint.alternates.join(', ')}` : ''}.
                {' '}sound: {hint.instruments}{hint.bpm ? `, ${hint.bpm[0]}-${hint.bpm[1]} BPM` : ', no fixed pulse'}.
                {' '}add <i>{hint.bed_terms?.slice(0, 3).join(' / ')}</i>; avoid <i>{hint.avoid_terms?.join(', ')}</i>.
              </p>
              <ul className="text-xs space-y-0.5">
                {(hint.sources || []).map((s) => (
                  <li key={s.name} className="flex flex-wrap gap-x-2">
                    <a href={s.search || s.url} target="_blank" rel="noreferrer" className="text-brass underline">{s.name}</a>
                    <span className="text-muted">{s.licence}{s.attribution_required === true ? ' · credit required' : s.attribution_required === false ? ' · no credit needed' : ''} · {s.note}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="space-y-2">
            <p className="text-xs uppercase tracking-wider text-muted">add a track to the library</p>
            <div className="flex flex-wrap items-center gap-2">
              <input type="file" accept="audio/*,.mp3,.m4a,.wav,.aac,.ogg,.flac,.mp4,.webm,.mkv"
                onChange={(e) => setFile(e.target.files?.[0] || null)} className="text-xs" />
              <select value={form.mood} onChange={(e) => setForm({ ...form, mood: e.target.value })}
                className="bg-paper3 border border-rule rounded-lg px-2 py-1 text-xs text-ink">
                {MOODS.map((m) => <option key={m} value={m}>{m}</option>)}
                <option value="any">any mood</option>
              </select>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              <input placeholder="source (pixabay, fma…)" value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} className="bg-paper3 border border-rule rounded-lg px-2 py-1 text-xs text-ink" />
              <input placeholder="licence" value={form.licence} onChange={(e) => setForm({ ...form, licence: e.target.value })} className="bg-paper3 border border-rule rounded-lg px-2 py-1 text-xs text-ink" />
              <input placeholder="track url" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} className="bg-paper3 border border-rule rounded-lg px-2 py-1 text-xs text-ink" />
              <input placeholder="credit line, if required" value={form.attribution} onChange={(e) => setForm({ ...form, attribution: e.target.value })} className="bg-paper3 border border-rule rounded-lg px-2 py-1 text-xs text-ink" />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <label className="text-xs flex items-center gap-1">
                <input type="checkbox" checked={form.attribution_required} onChange={(e) => setForm({ ...form, attribution_required: e.target.checked })} /> credit required
              </label>
              <button type="button" onClick={upload} disabled={!file || busy}
                className="px-3 py-1.5 rounded-lg bg-ink text-paper text-xs font-medium disabled:opacity-40 flex items-center gap-2">
                {busy ? <Loader2 size={12} className="animate-spin" /> : <Upload size={12} />} add to library and rebuild shots
              </button>
              {msg && <span className="text-xs text-ink2">{msg}</span>}
            </div>
            <p className="readout">saved to assets/music/&lt;mood&gt;/ with a licence sidecar; analysed for BPM and loudness on the spot. no vocals, steady level, 2 min or a clean loop.</p>
          </div>
        </div>
      )}
    </div>
  );
}

function RenderCard({ render, settings }) {
  if (!render) return <p className="text-sm text-muted">not rendered yet.</p>;
  return (
    <div className="border border-rule rounded-card p-4 space-y-3">
      <div className="flex items-center gap-2 text-sm">
        {render.status === 'running' && <><Loader2 size={14} className="animate-spin text-brass" /> rendering…</>}
        {render.status === 'completed' && <span className="text-ok">rendered</span>}
        {render.status === 'failed' && <span className="text-bad flex items-center gap-2"><AlertCircle size={14} /> {render.error}</span>}
      </div>
      {render.logs?.length > 0 && (
        <pre className="text-[11px] font-mono text-muted bg-paper3 rounded p-2 max-h-32 overflow-y-auto">{render.logs.slice(-12).join('\n')}</pre>
      )}
      {render.status === 'completed' && (
        <>
          <video src={getApiUrl(render.output)} controls className={`bg-black rounded ${settings.ratio === '1:1' ? 'w-72 h-72' : 'w-56 h-[398px]'}`} />
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Stat label="cuts detected" value={render.rhythm?.cuts} />
            <Stat label="mean gap" value={render.rhythm?.mean_gap ? `${render.rhythm.mean_gap.toFixed(2)}s` : '—'}
              tone={render.rhythm?.mean_gap >= 1.3 && render.rhythm?.mean_gap <= 1.7 ? 'text-ok' : 'text-warn'} />
            <Stat label="phase lock" value={render.rhythm?.phase_lock ?? '—'} tone={render.rhythm?.phase_lock > 0.8 ? 'text-ok' : ''} />
            <Stat label="bed" value={render.music?.track?.file || 'none'} mono={false} />
          </div>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <a href={getApiUrl(render.output)} download className="px-3 py-1.5 rounded-lg border border-rule text-ink2 hover:border-brass flex items-center gap-2"><Download size={14} /> download</a>
            <span className="text-xs text-muted flex items-center gap-1"><ExternalLink size={12} /> upload unlisted first and run YouTube Studio&apos;s copyright check; licence: {settings.source_license}</span>
          </div>
          {render.attribution && <p className="text-xs text-muted">music credit for the description: {render.attribution}</p>}
        </>
      )}
    </div>
  );
}
