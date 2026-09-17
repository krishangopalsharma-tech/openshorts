import { useState } from 'react';
import { Copy, Check, Download, Loader2, AlertCircle, AlertTriangle } from 'lucide-react';
import { copyText, downloadText } from './filmApi';

/**
 * The chat round-trip, as one panel: fetch a prompt, copy it, paste the
 * model's answer, validate. Errors come back structured; the "copy errors"
 * button turns them into the correction turn the user pastes back. That
 * loop is the whole UX of the film modules.
 *
 * Props:
 *  - title, hint
 *  - loadPrompt(): Promise<{prompt, words}>
 *  - submit(text): Promise<{ok, errors, warnings?, errors_text, warnings_text?}>
 *  - onValid(result)
 *  - promptName: download filename
 */
export default function PromptPastePanel({ title, hint, loadPrompt, submit, onValid, promptName = 'prompt.txt', disabled = false }) {
  const [prompt, setPrompt] = useState(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const [pasted, setPasted] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [errCopied, setErrCopied] = useState(false);

  const fetchPrompt = async () => {
    setLoading(true);
    setError('');
    try {
      const data = await loadPrompt();
      setPrompt(data);
      if (await copyText(data.prompt)) { setCopied(true); setTimeout(() => setCopied(false), 2500); }
    } catch (e) {
      setError(e.message || 'Could not build the prompt.');
    } finally {
      setLoading(false);
    }
  };

  const doSubmit = async (force = false) => {
    if (!pasted.trim()) return;
    setBusy(true);
    setError('');
    try {
      const res = await submit(pasted, { force });
      setResult(res);
      if (res.ok) onValid?.(res);
    } catch (e) {
      setError(e.message || 'Validation failed.');
    } finally {
      setBusy(false);
    }
  };

  const errors = result?.errors || [];
  const warnings = result?.warnings || [];
  const correction = [result?.errors_text, result?.warnings_text].filter(Boolean).join('\n');
  // Rule violations can be accepted on purpose; a plan that did not even
  // parse (schema errors) cannot, there is nothing to render.
  const canForce = result && !result.ok && result.forceable !== false && errors.length > 0
    && !errors.every((e) => e.code === 'schema');

  return (
    <div className={`border border-rule rounded-card p-4 space-y-3 ${disabled ? 'opacity-50 pointer-events-none' : ''}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="font-medium text-ink">{title}</p>
          {hint && <p className="text-xs text-muted mt-0.5">{hint}</p>}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button type="button" onClick={fetchPrompt} disabled={loading}
            className="px-3 py-1.5 rounded-lg border border-rule text-sm text-ink2 hover:border-brass flex items-center gap-2">
            {loading ? <Loader2 size={14} className="animate-spin" /> : copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}
            {copied ? 'copied' : 'copy prompt'}
          </button>
          {prompt && (
            <button type="button" onClick={() => downloadText(promptName, prompt.prompt)}
              className="p-1.5 rounded-lg border border-rule text-ink2 hover:border-brass" title="download prompt">
              <Download size={14} />
            </button>
          )}
        </div>
      </div>
      {prompt && (
        <p className="readout">{prompt.words?.toLocaleString()} words · paste it into Claude or ChatGPT, then paste the JSON answer below</p>
      )}
      <textarea
        value={pasted}
        onChange={(e) => setPasted(e.target.value)}
        placeholder="paste the model's JSON answer here (code fences are fine)"
        rows={5}
        className="w-full bg-paper3 border border-rule rounded-lg px-3 py-2 text-xs font-mono text-ink"
      />
      <div className="flex items-center gap-2 flex-wrap">
        <button type="button" onClick={() => doSubmit(false)} disabled={busy || !pasted.trim()}
          className="px-4 py-2 rounded-lg bg-ink text-paper text-sm font-medium disabled:opacity-40 flex items-center gap-2">
          {busy && <Loader2 size={14} className="animate-spin" />} validate
        </button>
        {result?.ok && errors.length === 0 && (
          <span className="text-ok text-sm flex items-center gap-1"><Check size={14} /> accepted</span>
        )}
        {result?.ok && result?.forced && (
          <span className="text-warn text-sm flex items-center gap-1"><AlertTriangle size={14} /> accepted with {errors.length} ignored error{errors.length === 1 ? '' : 's'}</span>
        )}
        {canForce && (
          <button type="button" onClick={() => doSubmit(true)} disabled={busy}
            title="Keep this plan as it is. Beats that cannot be cut are dropped; captions always come from the real subtitles."
            className="px-3 py-1.5 rounded-lg border border-warn text-warn text-sm hover:bg-warn/10 flex items-center gap-2">
            <AlertTriangle size={14} /> accept anyway
          </button>
        )}
        {correction && (
          <button type="button"
            onClick={async () => { if (await copyText(`Fix these and return the full JSON again:\n${correction}`)) { setErrCopied(true); setTimeout(() => setErrCopied(false), 2500); } }}
            className="px-3 py-1.5 rounded-lg border border-rule text-sm text-ink2 hover:border-brass flex items-center gap-2">
            {errCopied ? <Check size={14} className="text-ok" /> : <Copy size={14} />} copy corrections for the model
          </button>
        )}
      </div>
      {error && <p className="text-bad text-sm flex items-center gap-2"><AlertCircle size={14} /> {error}</p>}
      {errors.length > 0 && (
        <ul className="space-y-1">
          {errors.map((e, i) => (
            <li key={i} className="text-sm text-bad flex items-start gap-2 bg-paper3 rounded px-2 py-1">
              <AlertCircle size={14} className="mt-0.5 shrink-0" />
              <span>
                {e.hook !== undefined && <span className="font-mono text-xs mr-1">hook {e.hook}</span>}
                {e.beat !== undefined && <span className="font-mono text-xs mr-1">beat {e.beat + 1}</span>}
                {e.part !== undefined && <span className="font-mono text-xs mr-1">part {e.part}</span>}
                {e.chunk !== undefined && <span className="font-mono text-xs mr-1">chunk {e.chunk + 1}</span>}
                {e.message}
              </span>
            </li>
          ))}
        </ul>
      )}
      {warnings.length > 0 && (
        <ul className="space-y-1">
          {warnings.map((w, i) => (
            <li key={i} className="text-sm text-warn flex items-start gap-2 bg-paper3 rounded px-2 py-1">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <span>
                {w.chunk !== undefined && <span className="font-mono text-xs mr-1">chunk {w.chunk + 1}</span>}
                {w.message} <span className="text-muted">· review, not auto-failed</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
