import { useCallback, useEffect, useState } from 'react';
import { BookOpen, Loader2, AlertCircle, Download, Mic, Sparkles, ShieldCheck, ShieldAlert, ExternalLink } from 'lucide-react';
import StepIndicator from './ui/StepIndicator';
import SessionSetup, { Stat } from './film/SessionSetup';
import PromptPastePanel from './film/PromptPastePanel';
import { filmJson, fmtTime, useRenderPoll } from './film/filmApi';
import { getApiUrl } from '../config';

const STEPS = ['Film + SRT', 'Spoiler map', 'Structure', 'Part plans', 'Render', 'Voiceover'];

export default function MovieRecapTab() {
  const [session, setSession] = useState(null);
  const [step, setStep] = useState(0);
  const [renders, setRenders] = useState({});
  const [budget, setBudget] = useState(null);
  const [error, setError] = useState('');
  const [exclusion, setExclusion] = useState({ start: '', end: '' });

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
          <h1 className="text-xl font-semibold text-ink flex items-center gap-2"><BookOpen size={20} className="text-brass" /> Movie Recap</h1>
          <p className="text-sm text-muted mt-1 max-w-2xl">
            Three parts that make the viewer want to watch the film <em>more</em>. Each argues one claim about how the film
            is built; every payoff is withheld and nothing after 75% of the runtime is ever used. Planned in a chat window,
            narrated by you, rendered here with the original soundtrack muted.
          </p>
        </header>
        <StepIndicator steps={STEPS} current={step} onStepClick={(i) => { if (session || i === 0) setStep(i); }} />

        {step === 0 && (
          <SessionSetup kind="recap" session={session} onSession={onSession} settings={{}}>
            {session && <button type="button" onClick={() => setStep(1)} className="px-4 py-2 rounded-lg bg-ink text-paper text-sm">continue</button>}
          </SessionSetup>
        )}

        {session && step >= 1 && <ProtectedBar duration={session.duration} ranges={protectedRanges} />}

        {step === 1 && session && (
          <div className="space-y-4">
            <PromptPastePanel
              title="Pass A · spoiler map"
              hint="What must never be shown or described. Over-protecting costs nothing."
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
              <PromptPastePanel
                key={p.index}
                title={`Pass C · part ${p.index}: ${p.title}`}
                hint={`Withholds: ${p.withheld}`}
                promptName={`${session.title}-part${p.index}.txt`}
                loadPrompt={() => filmJson(`/api/movierecap/prompt/${session.id}?pass=c&part=${p.index}`)}
                submit={(text, opts) => filmJson(`/api/movierecap/plan/${session.id}/${p.index}`, { json: { text, force: !!opts?.force } })}
                onValid={async () => { await refreshSession(); loadBudget(); }}
              />
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
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <RenderBlock label="silent cut" render={silent} />
                    <div className="space-y-2">
                      <label className="px-3 py-1.5 rounded-lg border border-rule text-sm text-ink2 hover:border-brass inline-flex items-center gap-2 cursor-pointer">
                        <Mic size={14} /> upload voiceover
                        <input type="file" accept="audio/*,.wav,.mp3,.m4a,.flac,.ogg" className="hidden"
                          onChange={(e) => uploadVo(p.index, e.target.files?.[0])} />
                      </label>
                      <p className="readout">the recording is transcribed, each line placed on its words and the cut re-fitted; within ±10% of 150 s, else it is refused with the numbers</p>
                      {voiced?.vo_check && (
                        <p className={`text-xs font-mono ${voiced.vo_check.ok ? 'text-ok' : 'text-bad'}`}>
                          voiceover {voiced.vo_check.measured}s against {voiced.vo_check.target}s
                        </p>
                      )}
                      <RenderBlock label="narrated part" render={voiced} />
                    </div>
                  </div>
                </div>
              );
            })}
            <p className="text-xs text-muted flex items-center gap-1"><ExternalLink size={12} /> upload each part unlisted, run YouTube Studio&apos;s copyright check, then decide. name the film, year and director and link a legitimate way to watch it.</p>
          </div>
        )}

        {error && <p className="text-bad text-sm flex items-center gap-2"><AlertCircle size={14} /> {error}</p>}
      </div>
    </div>
  );
}

function ProtectedBar({ duration, ranges }) {
  return (
    <div>
      <div className="relative h-5 rounded bg-ok/20 overflow-hidden border border-rule">
        {ranges.map((r, i) => (
          <div key={i} title={`${r.tier}: ${fmtTime(r.start)}–${fmtTime(r.end)}`}
            className={`absolute top-0 bottom-0 ${r.tier === 'wall' ? 'bg-bad/40' : 'bg-bad/70'}`}
            style={{ left: `${(r.start / duration) * 100}%`, width: `${((r.end - r.start) / duration) * 100}%` }} />
        ))}
      </div>
      <p className="readout mt-1">usable footage in green · protected ranges and the 75% wall in red · {fmtTime(duration)} total</p>
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
