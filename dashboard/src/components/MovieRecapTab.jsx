import { useCallback, useEffect, useState } from 'react';
import { BookOpen, Loader2, AlertCircle, Download, Mic, Sparkles, ShieldCheck, ShieldAlert, ExternalLink, FileText, Copy, Check, Volume2, RefreshCw } from 'lucide-react';
import StepIndicator from './ui/StepIndicator';
import SegmentedControl from './ui/SegmentedControl';
import SubtitleModal from './SubtitleModal';
import OverlayEditor from './OverlayEditor';
import SessionSetup, { Stat } from './film/SessionSetup';
import PromptPastePanel from './film/PromptPastePanel';
import ShortsFlow from './film/ShortsFlow';
import { filmJson, fmtTime, useRenderPoll, copyText, downloadText } from './film/filmApi';
import { getApiUrl } from '../config';

const STEPS = ['Film + SRT', 'Spoiler map', 'Structure', 'Part plans', 'Render', 'Voiceover'];

export default function MovieRecapTab({ onOpenJob }) {
  const [session, setSession] = useState(null);
  const [step, setStep] = useState(0);
  // What the session is for right now: the three-part narrated recap, or
  // 2-5 montage shorts through the clip maker. Same film, same SRT, same
  // offset; only the planning prompt and the render differ.
  const [product, setProduct] = useState('recap');
  const [renders, setRenders] = useState({});
  const [budget, setBudget] = useState(null);
  const [error, setError] = useState('');
  const [exclusion, setExclusion] = useState({ start: '', end: '' });
  // Kokoro (generated voiceover) health + voice list; fetched when the render
  // step opens and refreshable from the panel. Offline is a state, not an error.
  const [kokoro, setKokoro] = useState(null);
  const loadKokoro = useCallback(async () => {
    try { setKokoro(await filmJson('/api/film/voices')); } catch { setKokoro({ online: false, groups: {} }); }
  }, []);
  useEffect(() => { if (step >= 4 && !kokoro) loadKokoro(); }, [step, kokoro, loadKokoro]);
  // Start the Kokoro server from KOKORO_HOME (CPU); waits up to ~90 s.
  const startKokoro = useCallback(async () => {
    const r = await filmJson('/api/film/voices/start', { method: 'POST' });
    setKokoro(r);
    if (!r.online) throw new Error(r.reason || 'Kokoro did not start');
  }, []);

  const onSession = useCallback((s) => {
    setSession(s);
    setRenders(s?.renders || {});
    if (!s) { setStep(0); setBudget(null); return; }
    const rc = s.recap || {};
    setStep(rc.structure ? 3 : rc.spoilers ? 2 : 1);
  }, []);

  useRenderPoll(session?.id, renders, setRenders);

  const refreshSession = useCallback(async () => {
    if (!session) return;
    try {
      const s = await filmJson(`/api/film/session/${session.id}`);
      setSession(s);
      setRenders(s.renders || {});
    } catch { /* ignore */ }
  }, [session]);

  const loadBudget = useCallback(async () => {
    if (!session) return;
    try { setBudget(await filmJson(`/api/movierecap/budget/${session.id}`)); } catch { /* ignore */ }
  }, [session]);

  useEffect(() => { if (step >= 3) loadBudget(); }, [step, session?.updated, loadBudget]);

  const rc = session?.recap || {};
  const parts = rc.structure?.parts || [];
  const protectedRanges = rc.protected || [];
  const plans = rc.plans || {};
  const allPlanned = parts.length === 3 && parts.every((p) => plans[String(p.index)]);

  const addExclusion = async () => {
    const s = Number(exclusion.start); const e = Number(exclusion.end);
    if (Number.isNaN(s) || Number.isNaN(e) || e <= s) return;
    try {
      await filmJson(`/api/movierecap/exclusions/${session.id}`, { json: { ranges: [...(rc.manual_excluded || []), { start: s, end: e }] } });
      setExclusion({ start: '', end: '' });
      refreshSession();
    } catch (err) { setError(err.message); }
  };

  const clearExclusions = async () => {
    try { await filmJson(`/api/movierecap/exclusions/${session.id}`, { json: { ranges: [] } }); refreshSession(); } catch (err) { setError(err.message); }
  };

  const renderSilent = async () => {
    setError('');
    try {
      const r = await filmJson(`/api/movierecap/render/${session.id}`, { json: {} });
      setRenders((prev) => ({ ...prev, ...Object.fromEntries(r.renders.map((k) => [k, { status: 'running', logs: [] }])) }));
      setStep(4);
    } catch (e) { setError(e.message); }
  };

  const uploadVo = async (part, file) => {
    if (!file) return;
    setError('');
    const fd = new FormData();
    fd.append('file', file);
    try {
      await filmJson(`/api/movierecap/voiceover/${session.id}/${part}`, { method: 'POST', body: fd });
      setRenders((prev) => ({ ...prev, [`recap-${part}`]: { status: 'running', logs: [] } }));
    } catch (e) { setError(e.message); }
  };

  return (
    <div className="h-full overflow-y-auto custom-scrollbar p-4 sm:p-6 md:p-10 animate-fade">
      <div className="max-w-5xl mx-auto space-y-6">
        <header>
          <h1 className="text-xl font-semibold text-ink flex items-center gap-2"><BookOpen size={20} className="text-brass" /> Movie Recap &amp; Shorts</h1>
          <p className="text-sm text-muted mt-1 max-w-2xl">
            {product === 'shorts'
              ? 'Two-minute montages: 4-9 scenes from anywhere in the film in the order that tells the best mini-story, the film\'s own lines as captions, a music bed that never stops, the last line a twist. Planned in a chat window, rendered as ordinary clips.'
              : 'A three-part narrated retelling. Planned in a chat window, voiced by Kokoro or by you, rendered here with the original soundtrack muted under the narration.'}
          </p>
        </header>
        {session && (
          <div className="border border-rule rounded-card p-3">
            <SegmentedControl size="sm" value={product} onChange={setProduct}
              options={[
                { value: 'recap', label: 'recap series', hint: 'three narrated parts' },
                { value: 'shorts', label: 'shorts', hint: '2-5 montages through the clip maker' },
              ]} />
          </div>
        )}
        {product === 'shorts' && session ? (
          <ShortsFlow session={session} onSession={onSession} renders={renders} setRenders={setRenders} onOpenJob={onOpenJob} />
        ) : (<>
        <StepIndicator
          steps={(session?.settings?.recap_mode || 'story') === 'story' ? STEPS.map((s) => (s === 'Spoiler map' ? 'Story map' : s)) : STEPS}
          current={step} onStepClick={(i) => { if (session || i === 0) setStep(i); }} />

        {step === 0 && (
          <SessionSetup kind="recap" session={session} onSession={onSession} settings={{}}>
            {session && (
              <div className="border border-rule rounded-card p-4 space-y-2">
                <p className="text-xs uppercase tracking-wider text-muted">series type</p>
                <SegmentedControl size="sm" value={session.settings?.recap_mode || 'story'}
                  onChange={async (v) => {
                    try { setSession(await filmJson(`/api/film/session/${session.id}/settings`, { json: { recap_mode: v } })); } catch (e) { setError(e.message); }
                  }}
                  options={[
                    { value: 'story', label: 'full story', hint: 'whole film in three parts, cliffhangers, the ending told' },
                    { value: 'teaser', label: 'no spoilers', hint: 'appetite-building essay, nothing after 75%' },
                  ]} />
                <p className="readout">pick before pass A: it changes what the chat is asked for and what the validator enforces.</p>
              </div>
            )}
            {session && (
              <div className="border border-rule rounded-card p-4 space-y-2">
                <p className="text-xs uppercase tracking-wider text-muted">narration voice</p>
                <SegmentedControl size="sm" value={session.settings?.narration_style || 'story'}
                  onChange={async (v) => {
                    try { setSession(await filmJson(`/api/film/session/${session.id}/settings`, { json: { narration_style: v } })); } catch (e) { setError(e.message); }
                  }}
                  options={[
                    { value: 'story', label: 'story', hint: 'from inside the moment, in the film\'s mood; no camera talk' },
                    { value: 'essay', label: 'essay', hint: 'video-essay analysis: what to watch for' },
                  ]} />
                <p className="readout">changes the pass C prompt and what the validator warns about. pick before generating the part plans.</p>
              </div>
            )}
            {session && <button type="button" onClick={() => setStep(1)} className="px-4 py-2 rounded-lg bg-ink text-paper text-sm">continue</button>}
          </SessionSetup>
        )}

        {session && step >= 1 && <ProtectedBar duration={session.duration} ranges={protectedRanges} mode={session.settings?.recap_mode || 'story'} />}

        {step === 1 && session && (
          <div className="space-y-4">
            <PromptPastePanel
              title={(session.settings?.recap_mode || 'story') === 'story' ? 'Pass A · story map' : 'Pass A · spoiler map'}
              hint={(session.settings?.recap_mode || 'story') === 'story'
                ? 'Who, what they want, what stops them, where it turns, how it ends. Spoilers wanted.'
                : 'What must never be shown or described. Over-protecting costs nothing.'}
              promptName={`${session.title}-pass-a.txt`}
              loadPrompt={() => filmJson(`/api/movierecap/prompt/${session.id}?pass=a`)}
              submit={(text) => filmJson(`/api/movierecap/spoilers/${session.id}`, { json: { text } })}
              onValid={async () => { await refreshSession(); }}
            />
            <div className="border border-rule rounded-card p-4 space-y-3">
              <p className="text-xs uppercase tracking-wider text-muted">your own exclusions (seconds)</p>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <input type="number" placeholder="start" value={exclusion.start} onChange={(e) => setExclusion({ ...exclusion, start: e.target.value })}
                  className="w-24 bg-paper3 border border-rule rounded-lg px-3 py-1.5 font-mono text-ink" />
                <input type="number" placeholder="end" value={exclusion.end} onChange={(e) => setExclusion({ ...exclusion, end: e.target.value })}
                  className="w-24 bg-paper3 border border-rule rounded-lg px-3 py-1.5 font-mono text-ink" />
                <button type="button" onClick={addExclusion} className="px-3 py-1.5 rounded-lg border border-rule text-ink2 hover:border-brass">add</button>
                {(rc.manual_excluded || []).length > 0 && (
                  <button type="button" onClick={clearExclusions} className="text-xs text-muted hover:text-ink underline">clear mine</button>
                )}
              </div>
              <ul className="text-xs font-mono text-muted space-y-0.5">
                {protectedRanges.map((r, i) => (
                  <li key={i}>{fmtTime(r.start)}–{fmtTime(r.end)} <span className="text-brass">{r.tier}</span> {r.why}</li>
                ))}
              </ul>
            </div>
            {rc.spoilers && <button type="button" onClick={() => setStep(2)} className="px-4 py-2 rounded-lg bg-ink text-paper text-sm">continue to structure</button>}
          </div>
        )}

        {step === 2 && session && (
          <div className="space-y-4">
            <PromptPastePanel
              title="Pass B · series structure"
              hint="Three claims, three titles, what each part withholds. Read it as a reader would."
              promptName={`${session.title}-pass-b.txt`}
              loadPrompt={() => filmJson(`/api/movierecap/prompt/${session.id}?pass=b`)}
              submit={(text) => filmJson(`/api/movierecap/structure/${session.id}`, { json: { text } })}
              onValid={async () => { await refreshSession(); setStep(3); }}
              disabled={!rc.spoilers}
            />
            {parts.length > 0 && <PartCards parts={parts} plans={plans} renders={renders} />}
          </div>
        )}

        {step === 3 && session && (
          <div className="space-y-4">
            <PartCards parts={parts} plans={plans} renders={renders} />
            {parts.map((p) => (
              <div key={p.index} className="space-y-2">
                <PromptPastePanel
                  title={`Pass C · part ${p.index}: ${p.title}`}
                  hint={`Withholds: ${p.withheld}`}
                  promptName={`${session.title}-part${p.index}.txt`}
                  loadPrompt={() => filmJson(`/api/movierecap/prompt/${session.id}?pass=c&part=${p.index}`)}
                  submit={(text, opts) => filmJson(`/api/movierecap/plan/${session.id}/${p.index}`, { json: { text, force: !!opts?.force } })}
                  onValid={async () => { await refreshSession(); loadBudget(); }}
                />
                {plans[String(p.index)] && (
                  <ScriptPanel part={p} plan={plans[String(p.index)]} speed={Number(session.settings?.speed) || 1.25} title={session.title} />
                )}
              </div>
            ))}
            <BudgetPanel budget={budget} />
            <button type="button" onClick={renderSilent} disabled={!allPlanned}
              className="px-4 py-2 rounded-lg bg-ink text-paper text-sm font-medium disabled:opacity-40 flex items-center gap-2">
              <Sparkles size={14} /> render three silent parts
            </button>
            {!allPlanned && <p className="text-xs text-muted">all three parts need an accepted plan first.</p>}
          </div>
        )}

        {step >= 4 && session && (
          <div className="space-y-4">
            <BudgetPanel budget={budget} compact />
            {parts.map((p) => {
              const silent = renders[`recap-${p.index}-silent`];
              const voiced = renders[`recap-${p.index}`];
              return (
                <div key={p.index} className="border border-rule rounded-card p-4 space-y-3">
                  <p className="font-semibold text-ink">Part {p.index} · {p.title}</p>
                  <ScriptPanel part={p} plan={plans[String(p.index)]} speed={Number(session.settings?.speed) || 1.25} title={session.title} />
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <RenderBlock label="silent cut" render={silent} />
                    <div className="space-y-3">
                      <VoicePanel
                        sessionId={session.id} part={p.index} render={voiced} kokoro={kokoro} onRefresh={loadKokoro}
                        onStartServer={startKokoro}
                        settings={session.settings || {}}
                        onStarted={(r) => setRenders((prev) => ({ ...prev, [`recap-${p.index}`]: { status: 'running', logs: [], ...r } }))}
                        onError={setError}
                      />
                      <details className="text-sm">
                        <summary className="cursor-pointer text-ink2 hover:text-ink flex items-center gap-2"><Mic size={14} /> or upload your own recording</summary>
                        <div className="mt-2 space-y-2">
                          <label className="px-3 py-1.5 rounded-lg border border-rule text-sm text-ink2 hover:border-brass inline-flex items-center gap-2 cursor-pointer">
                            <Mic size={14} /> upload voiceover
                            <input type="file" accept="audio/*,.wav,.mp3,.m4a,.flac,.ogg" className="hidden"
                              onChange={(e) => uploadVo(p.index, e.target.files?.[0])} />
                          </label>
                          <p className="readout">the recording is transcribed, each line placed on its words and the cut re-fitted; within ±10% of 150 s, else it is refused with the numbers</p>
                        </div>
                      </details>
                      {voiced?.vo_check && !voiced.generated && (
                        <p className={`text-xs font-mono ${voiced.vo_check.ok ? 'text-ok' : 'text-bad'}`}>
                          voiceover {voiced.vo_check.measured}s against {voiced.vo_check.target}s
                        </p>
                      )}
                      <RenderBlock label="narrated part" render={voiced} />
                      {voiced?.status === 'completed' && voiced.base_file && (
                        <PartTools sessionId={session.id} part={p.index} render={voiced}
                          onUpdated={(patch) => setRenders((prev) => ({ ...prev, [`recap-${p.index}`]: { ...prev[`recap-${p.index}`], ...patch } }))}
                          onError={setError} />
                      )}
                      {voiced?.fit && <FitTable fit={voiced.fit} overruns={voiced.overruns} words={voiced.words} />}
                    </div>
                  </div>
                </div>
              );
            })}
            <p className="text-xs text-muted flex items-center gap-1"><ExternalLink size={12} /> upload each part unlisted, run YouTube Studio&apos;s copyright check, then decide. name the film, year and director and link a legitimate way to watch it.</p>
          </div>
        )}
        </>)}

        {error && <p className="text-bad text-sm flex items-center gap-2"><AlertCircle size={14} /> {error}</p>}
      </div>
    </div>
  );
}

function ProtectedBar({ duration, ranges, mode = 'story' }) {
  return (
    <div>
      <div className="relative h-5 rounded bg-ok/20 overflow-hidden border border-rule">
        {ranges.map((r, i) => (
          <div key={i} title={`${r.tier}: ${fmtTime(r.start)}–${fmtTime(r.end)}`}
            className={`absolute top-0 bottom-0 ${r.tier === 'wall' ? 'bg-bad/40' : 'bg-bad/70'}`}
            style={{ left: `${(r.start / duration) * 100}%`, width: `${((r.end - r.start) / duration) * 100}%` }} />
        ))}
      </div>
      <p className="readout mt-1">
        {mode === 'story'
          ? `full story: the whole film is usable${ranges.length ? ', your exclusions in red' : ''} · ${fmtTime(duration)} total`
          : `usable footage in green · protected ranges and the 75% wall in red · ${fmtTime(duration)} total`}
      </p>
    </div>
  );
}

function PartCards({ parts, plans, renders }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
      {parts.map((p) => {
        const plan = plans[String(p.index)];
        const r = renders[`recap-${p.index}`] || renders[`recap-${p.index}-silent`];
        return (
          <div key={p.index} className="border border-rule rounded-card p-4 space-y-2">
            <p className="font-semibold text-ink text-sm">{p.index}. {p.title}</p>
            <p className="text-xs text-ink2">{p.claim}</p>
            <p className="text-xs text-muted">withholds: {p.withheld}</p>
            <div className="flex flex-wrap gap-1.5">
              <span className="font-mono text-[10px] px-2 py-0.5 rounded-full border border-rule text-muted">{fmtTime(p.evidence_band?.start)}–{fmtTime(p.evidence_band?.end)}</span>
              {plan && <span className="font-mono text-[10px] px-2 py-0.5 rounded-full border border-ok text-ok">{plan.chunks.length} chunks ✓</span>}
              {r?.status === 'completed' && <span className="font-mono text-[10px] px-2 py-0.5 rounded-full border border-ok text-ok">rendered</span>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function BudgetPanel({ budget, compact = false }) {
  if (!budget) return null;
  const ok = (cond) => (cond ? 'text-ok' : 'text-bad');
  return (
    <div className="border border-rule rounded-card p-4 space-y-2">
      <p className="text-xs uppercase tracking-wider text-muted flex items-center gap-2">
        {budget.ok ? <ShieldCheck size={14} className="text-ok" /> : <ShieldAlert size={14} className="text-warn" />} budget · parts planned {budget.parts_planned.join(', ') || 'none'}
      </p>
      <div className={`grid grid-cols-2 ${compact ? 'md:grid-cols-8' : 'md:grid-cols-4'} gap-3`}>
        <Stat label="footage" value={fmtTime(budget.footage_seconds)} tone={ok(budget.footage_seconds < 600)} />
        <Stat label="of runtime" value={`${(budget.footage_share * 100).toFixed(1)}%`} tone={ok(budget.footage_share < 0.1)} />
        <Stat label="longest / mean" value={`${budget.longest_chunk}s / ${budget.mean_chunk}s`} tone={ok(budget.longest_chunk < 15)} />
        <Stat label="words ÷ footage s" value={budget.words_per_footage_second} tone={ok(budget.words_per_footage_second > 2.5)} />
        <Stat label="silent" value={`${(budget.silent_share * 100).toFixed(1)}%`} tone={ok(budget.silent_share < 0.05)} />
        <Stat label="protected / wall" value={`${budget.chunks_in_protected} / ${budget.chunks_past_wall}`} tone={ok(budget.chunks_in_protected === 0 && budget.chunks_past_wall === 0)} />
        <Stat label="resolution hits" value={budget.resolution_hits} tone={budget.resolution_hits ? 'text-warn' : 'text-ok'} />
        <Stat label="withheld distinct" value={budget.withheld_distinct ? 'yes' : 'no'} tone={ok(budget.withheld_distinct)} />
      </div>
    </div>
  );
}

// The voiceover script: every chunk's narration from the accepted Pass C
// plan, in order, with the slot it has to fit (chunk length at the playback
// speed). This is what gets recorded; compilation.py then places each line on
// its spoken words and re-fits the cut.
function buildScript(part, plan, speed = 1.25) {
  const chunks = plan?.chunks || [];
  const lines = chunks.map((c, i) => {
    const slot = (c.end - c.start) / speed;
    return { n: i + 1, seconds: slot, words: (c.narration || '').split(/\s+/).filter(Boolean).length, text: c.narration || '', shows: c.shows || '' };
  });
  const total = lines.reduce((s, l) => s + l.seconds, 0);
  const words = lines.reduce((s, l) => s + l.words, 0);
  const closing = plan?.closing_lines || [];
  const text = [
    `PART ${part.index} - ${part.title}`,
    `Claim: ${part.claim}`,
    `Read at a steady pace: ~${Math.round(words / Math.max(1, total) * 60)} words per minute fills the cut. Total ${fmtTime(total)}, ${words} words.`,
    '',
    ...lines.map((l) => `${String(l.n).padStart(2, '0')}  [${l.seconds.toFixed(1)}s]  ${l.text}`),
    ...(closing.length ? ['', 'CLOSING LINES', ...closing.map((c) => `    ${c}`)] : []),
  ].join('\n');
  return { lines, total, words, closing, text };
}

function ScriptPanel({ part, plan, speed, title }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  if (!plan) return null;
  const script = buildScript(part, plan, speed);
  const wpm = Math.round(script.words / Math.max(1, script.total) * 60);
  return (
    <div className="border border-rule rounded-card p-3 space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={() => setOpen((o) => !o)} className="text-sm text-ink flex items-center gap-2">
          <FileText size={14} className="text-brass" /> voiceover script
          <span className="font-mono text-xs text-muted">{script.lines.length} lines · {script.words} words · {fmtTime(script.total)} · ~{wpm} wpm</span>
        </button>
        <button type="button"
          onClick={async () => { if (await copyText(script.text)) { setCopied(true); setTimeout(() => setCopied(false), 2000); } }}
          className="px-2.5 py-1 rounded-lg border border-rule text-xs text-ink2 hover:border-brass flex items-center gap-1">
          {copied ? <Check size={12} className="text-ok" /> : <Copy size={12} />} copy
        </button>
        <button type="button" onClick={() => downloadText(`${title}-part${part.index}-script.txt`, script.text)}
          className="px-2.5 py-1 rounded-lg border border-rule text-xs text-ink2 hover:border-brass flex items-center gap-1">
          <Download size={12} /> .txt
        </button>
        {(wpm < 120 || wpm > 190) && (
          <span className="text-xs text-warn">{wpm < 120 ? 'thin: the cut will outrun the voice' : 'dense: you will have to rush'} at {wpm} wpm; 140-170 reads naturally</span>
        )}
      </div>
      {open && (
        <ol className="space-y-1.5 text-sm">
          {script.lines.map((l) => (
            <li key={l.n} className="grid grid-cols-[2rem_3.5rem_1fr] gap-2 items-start">
              <span className="font-mono text-xs text-muted pt-0.5">{String(l.n).padStart(2, '0')}</span>
              <span className="font-mono text-xs text-muted pt-0.5">{l.seconds.toFixed(1)}s</span>
              <span className="text-ink">{l.text}{l.shows && <span className="block text-xs text-muted">on screen: {l.shows}</span>}</span>
            </li>
          ))}
          {script.closing.length > 0 && (
            <li className="grid grid-cols-[2rem_3.5rem_1fr] gap-2 border-t border-rule pt-2">
              <span className="font-mono text-xs text-muted">end</span><span />
              <span className="text-ink">{script.closing.join(' ')}</span>
            </li>
          )}
        </ol>
      )}
      <p className="readout">record this in order, one take or many; then upload voiceover. each line is placed on its own words, so pauses between lines are fine.</p>
    </div>
  );
}


// Generated voiceover: pick a Kokoro voice, hear the part's first line, set
// the reading speed, generate. Synthesis runs on the CPU in the backend
// before the render touches ffmpeg or the GPU (docs/film-modules-plan.md §3.8).
function VoicePanel({ sessionId, part, render, kokoro, onRefresh, onStartServer, settings, onStarted, onError }) {
  const [voice, setVoice] = useState(settings.voice || kokoro?.default || 'am_michael');
  const [speed, setSpeed] = useState(Number(settings.tts_speed) || 1.0);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState('');

  const onStart = async () => {
    setBusy('start');
    try { await onStartServer(); } catch (e) { onError(e.message); } finally { setBusy(''); }
  };
  useEffect(() => { if (kokoro?.default && !settings.voice) setVoice(kokoro.default); }, [kokoro, settings.voice]);

  const running = render?.status === 'running';
  const online = !!kokoro?.online;
  const groups = kokoro?.groups || {};

  const doPreview = async () => {
    setBusy('preview');
    setPreview(null);
    try {
      const r = await filmJson(`/api/movierecap/narrate/${sessionId}/${part}/preview`, { json: { voice, speed } });
      setPreview({ ...r, url: `${getApiUrl(r.url)}?t=${Date.now()}` });
    } catch (e) { onError(e.message); } finally { setBusy(''); }
  };

  const doGenerate = async () => {
    setBusy('generate');
    try {
      const r = await filmJson(`/api/movierecap/narrate/${sessionId}/${part}`, { json: { voice, speed } });
      onStarted(r);
    } catch (e) { onError(e.message); } finally { setBusy(''); }
  };

  return (
    <div className="border border-rule rounded-card p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs uppercase tracking-wider text-muted flex items-center gap-2"><Volume2 size={14} className="text-brass" /> generated voiceover · Kokoro</p>
        <button type="button" onClick={onRefresh} className="text-xs text-muted hover:text-ink flex items-center gap-1" title="check the Kokoro server again"><RefreshCw size={12} /> {kokoro ? (online ? `online · ${kokoro.voices} voices · cpu` : 'offline') : 'checking…'}</button>
      </div>
      {kokoro && !online && (
        <div className="text-xs text-warn space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span>Kokoro server is not running.</span>
            <button type="button" onClick={onStart} disabled={busy === 'start'}
              className="px-3 py-1 rounded-lg bg-ink text-paper text-xs font-medium disabled:opacity-40 flex items-center gap-2">
              {busy === 'start' ? <Loader2 size={12} className="animate-spin" /> : <Volume2 size={12} />} start Kokoro (cpu)
            </button>
            {kokoro.reason && <span className="text-muted">{kokoro.reason}</span>}
          </div>
          <p className="text-muted">or by hand in PowerShell, then check again:</p>
          <pre className="font-mono text-[11px] bg-paper3 rounded p-2 text-ink">{kokoro.start_command}</pre>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <select value={voice} onChange={(e) => setVoice(e.target.value)} disabled={!online || running}
          className="bg-paper3 border border-rule rounded-lg px-2 py-1.5 text-sm text-ink min-w-[12rem]">
          {!online && <option value={voice}>{voice}</option>}
          {Object.entries(groups).map(([lang, vs]) => (
            <optgroup key={lang} label={lang}>
              {vs.filter((v) => !v.legacy).map((v) => (
                <option key={v.id} value={v.id}>{v.name} · {v.gender}</option>
              ))}
            </optgroup>
          ))}
        </select>
        <label className="text-xs text-ink2 flex items-center gap-2">
          speed
          <input type="range" min={0.9} max={1.2} step={0.05} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} disabled={running} />
          <span className="font-mono">{speed.toFixed(2)}x</span>
        </label>
        <button type="button" onClick={doPreview} disabled={!online || busy || running}
          className="px-3 py-1.5 rounded-lg border border-rule text-sm text-ink2 hover:border-brass disabled:opacity-40 flex items-center gap-2">
          {busy === 'preview' ? <Loader2 size={14} className="animate-spin" /> : <Volume2 size={14} />} preview line 1
        </button>
        <button type="button" onClick={doGenerate} disabled={!online || busy || running}
          className="px-4 py-1.5 rounded-lg bg-ink text-paper text-sm font-medium disabled:opacity-40 flex items-center gap-2">
          {busy === 'generate' || running ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />} generate voiceover and render
        </button>
      </div>
      {preview && (
        <div className="space-y-1">
          <audio src={preview.url} controls autoPlay className="w-full h-8" />
          <p className="readout">{preview.voice} · {preview.duration}s · &ldquo;{preview.text}&rdquo;</p>
        </div>
      )}
      <p className="readout">each line is read separately and placed on its chunk; a long line first borrows head/tail footage, then reads up to 1.25x faster, and past that is flagged below. the picture is never stretched.</p>
    </div>
  );
}

function FitTable({ fit, overruns, words }) {
  const tone = { fit: 'text-ok', room: 'text-ink2', speed: 'text-warn', overrun: 'text-bad', silent: 'text-muted' };
  return (
    <div className="border border-rule rounded-card p-3 space-y-2">
      <p className="text-xs uppercase tracking-wider text-muted">fit · {fit.length} lines · {words} words · <span className={overruns ? 'text-bad' : 'text-ok'}>{overruns} overrun{overruns === 1 ? '' : 's'}</span></p>
      <table className="w-full text-xs font-mono">
        <thead className="text-muted">
          <tr><th className="text-left">#</th><th className="text-right">slot</th><th className="text-right">voice</th><th className="text-right">room</th><th className="text-right">speed</th><th className="text-left pl-2">action</th></tr>
        </thead>
        <tbody>
          {fit.map((r) => (
            <tr key={r.chunk} className={tone[r.action] || ''}>
              <td>{r.chunk}</td>
              <td className="text-right">{r.slot?.toFixed ? r.slot.toFixed(1) : r.slot}s</td>
              <td className="text-right">{r.voice_seconds}s</td>
              <td className="text-right">{r.room_used ? `+${r.room_used}s` : '—'}</td>
              <td className="text-right">{r.speed}x</td>
              <td className="pl-2">{r.action}{r.action === 'overrun' ? ` by ${r.overrun}s: shorten this line` : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


// The same caption and logo tools a clip card has, on a narrated part. Both
// modals are the clip editor's own; they post to the part endpoints, which
// re-derive overlays under captions over the narrated base render.
function PartTools({ sessionId, part, render, onUpdated, onError }) {
  const [openCaptions, setOpenCaptions] = useState(false);
  const [openOverlays, setOpenOverlays] = useState(false);
  const [busy, setBusy] = useState(false);
  const videoUrl = render?.output ? `${getApiUrl(render.output)}?t=${render.finished || ''}` : '';

  const post = async (path, body) => {
    setBusy(true);
    try {
      const r = await filmJson(path, { json: body });
      onUpdated({ file: r.file, output: r.output, caption_style: r.caption_style ?? render.caption_style, overlays: r.overlays ?? render.overlays });
      return true;
    } catch (e) {
      onError(e.message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const captions = async (options) => {
    if (await post(`/api/movierecap/part/${sessionId}/${part}/captions`,
      { preset: options.preset, overrides: options.overrides || {}, words: options.captions || null })) setOpenCaptions(false);
  };
  const removeCaptions = async () => {
    if (await post(`/api/movierecap/part/${sessionId}/${part}/captions`, { preset: null })) setOpenCaptions(false);
  };
  const overlays = async (items) => {
    if (await post(`/api/movierecap/part/${sessionId}/${part}/overlays`, { overlays: items })) setOpenOverlays(false);
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button type="button" onClick={() => setOpenCaptions(true)} disabled={busy}
        className="px-3 py-1.5 rounded-lg border border-rule text-sm text-ink2 hover:border-brass disabled:opacity-40 flex items-center gap-2">
        <FileText size={14} /> captions{render.caption_style ? ` · ${render.caption_style.preset}` : ''}
      </button>
      <button type="button" onClick={() => setOpenOverlays(true)} disabled={busy}
        className="px-3 py-1.5 rounded-lg border border-rule text-sm text-ink2 hover:border-brass disabled:opacity-40 flex items-center gap-2">
        <Sparkles size={14} /> logo &amp; text{render.overlays?.length ? ` · ${render.overlays.length}` : ''}
      </button>
      {busy && <Loader2 size={14} className="animate-spin text-brass" />}
      <SubtitleModal
        isOpen={openCaptions}
        onClose={() => setOpenCaptions(false)}
        onGenerate={captions}
        onRemove={render.caption_style ? removeCaptions : undefined}
        isProcessing={busy}
        videoUrl={videoUrl}
        jobId={null}
        clipIndex={part}
        transcriptPath={`/api/movierecap/part/${sessionId}/${part}/transcript`}
      />
      <OverlayEditor
        isOpen={openOverlays}
        onClose={() => setOpenOverlays(false)}
        clip={{ overlays: render.overlays || [], output_format: 'vertical' }}
        onApply={overlays}
        isProcessing={busy}
        videoUrl={videoUrl}
      />
    </div>
  );
}


function RenderBlock({ label, render }) {
  return (
    <div className="space-y-2">
      <p className="text-xs uppercase tracking-wider text-muted">{label}</p>
      {!render && <p className="text-xs text-muted">—</p>}
      {render?.status === 'running' && <p className="text-sm flex items-center gap-2"><Loader2 size={14} className="animate-spin text-brass" /> rendering…</p>}
      {render?.status === 'failed' && <p className="text-sm text-bad flex items-center gap-2"><AlertCircle size={14} /> {render.error}</p>}
      {render?.logs?.length > 0 && render.status !== 'completed' && (
        <pre className="text-[11px] font-mono text-muted bg-paper3 rounded p-2 max-h-24 overflow-y-auto">{render.logs.slice(-6).join('\n')}</pre>
      )}
      {render?.status === 'completed' && (
        <div className="space-y-2">
          <video src={getApiUrl(render.output)} controls className="bg-black rounded w-44 h-[312px]" />
          <a href={getApiUrl(render.output)} download className="text-xs text-ink2 hover:text-brass inline-flex items-center gap-1"><Download size={12} /> download</a>
        </div>
      )}
    </div>
  );
}
