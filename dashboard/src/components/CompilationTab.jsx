import { useState, useEffect, useRef } from 'react';
import { Upload, FileVideo, FileAudio, X, Loader2, AlertCircle, Download, Clapperboard } from 'lucide-react';
import { getApiUrl } from '../config';
import { apiFetch } from '../lib/api';

const EXAMPLE_PLAN_HINT = `Paste the Task 5 edit-plan JSON here — the block your Gemini chat
(or the Claude Code paste) produced: { "sequence": ..., "padding": ..., "shots": [...], "vo": {...} }`;

function UploadBox({ file, setFile, accept, icon, label, hint }) {
  const Icon = icon;
  const handleDrop = (e) => {
    e.preventDefault();
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) setFile(dropped);
  };

  return (
    <div
      className={`border-2 border-dashed rounded-card p-6 text-center transition-colors ${file ? 'border-brass' : 'border-rule2 hover:border-brass'}`}
      onDragOver={(e) => e.preventDefault()}
      onDrop={handleDrop}
    >
      {file ? (
        <div className="flex items-center justify-center gap-3 text-ok min-w-0">
          <Icon size={18} className="shrink-0" />
          <span className="font-medium truncate">{file.name}</span>
          <button type="button" onClick={() => setFile(null)}
            className="p-1 text-muted hover:text-ink hover:bg-paper3 rounded-full transition-colors">
            <X size={16} />
          </button>
        </div>
      ) : (
        <label className="cursor-pointer block">
          <input type="file" accept={accept} onChange={(e) => setFile(e.target.files?.[0] || null)} className="hidden" />
          <Upload className="mx-auto mb-3 text-muted" size={18} />
          <p className="text-ink2 lowercase">{label}</p>
          <p className="readout mt-2">{hint}</p>
        </label>
      )}
    </div>
  );
}

export default function CompilationTab() {
  const [video, setVideo] = useState(null);
  const [vo, setVo] = useState(null);
  const [plan, setPlan] = useState('');
  const [burnCaptions, setBurnCaptions] = useState(true);

  const [jobId, setJobId] = useState(null);
  const [status, setStatus] = useState('idle'); // idle | processing | completed | failed
  const [logs, setLogs] = useState([]);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const logsEndRef = useRef(null);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  useEffect(() => {
    let interval;
    if (jobId && status === 'processing') {
      interval = setInterval(async () => {
        try {
          const res = await apiFetch(`/api/compilation/status/${jobId}`);
          if (res.status === 404) {
            setStatus('failed');
            setError('Job lost — the server may have restarted. Try again.');
            clearInterval(interval);
            return;
          }
          const data = await res.json();
          setLogs(data.logs || []);
          if (data.status === 'completed') {
            setStatus('completed');
            setResult(data.result);
            clearInterval(interval);
          } else if (data.status === 'failed') {
            setStatus('failed');
            setError((data.logs || []).slice(-1)[0] || 'Compilation failed.');
            clearInterval(interval);
          }
        } catch {
          // transient — keep polling
        }
      }, 3000);
    }
    return () => clearInterval(interval);
  }, [jobId, status]);

  const canGenerate = video && vo && plan.trim().length > 0 && status !== 'processing';

  const handleGenerate = async () => {
    setError('');
    setResult(null);
    setLogs([]);

    let planData;
    try {
      planData = JSON.parse(plan);
    } catch {
      setError('That doesn\'t parse as JSON — paste the Task 5 block exactly as Gemini produced it.');
      return;
    }
    for (const key of ['sequence', 'padding', 'shots', 'vo']) {
      if (!(key in planData)) {
        setError(`Plan JSON is missing "${key}" — make sure you pasted the full Task 5 block.`);
        return;
      }
    }

    const formData = new FormData();
    formData.append('video', video);
    formData.append('vo', vo);
    formData.append('plan', plan);
    formData.append('burn_captions', burnCaptions ? 'true' : 'false');

    setStatus('processing');
    try {
      const res = await apiFetch('/api/compilation/generate', { method: 'POST', body: formData });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed to start job' }));
        setError(err.detail || 'Failed to start job');
        setStatus('failed');
        return;
      }
      const data = await res.json();
      setJobId(data.job_id);
    } catch (e) {
      setError(String(e));
      setStatus('failed');
    }
  };

  const reset = () => {
    setVideo(null); setVo(null); setPlan(''); setJobId(null);
    setStatus('idle'); setLogs([]); setResult(null); setError('');
  };

  return (
    <div className="h-full overflow-y-auto custom-scrollbar p-4 sm:p-6 md:p-10 animate-fade">
      <div className="max-w-4xl mx-auto space-y-8">
        <div className="space-y-3">
          <p className="eyebrow flex items-center gap-2">
            <Clapperboard size={12} /> 06 · COMPILATION
          </p>
          <h1 className="font-display lowercase text-3xl md:text-4xl text-ink">
            Narrated Compilation
          </h1>
          <p className="text-muted text-base md:text-lg leading-relaxed max-w-2xl">
            Turn a full-length source video into one narrated vertical short. Write the script and
            generate the voiceover yourself (Gemini + ElevenLabs) — drop the source video, the VO,
            and the edit-plan JSON here, and this cuts, fits, mixes and captions it.
          </p>
        </div>

        {status !== 'completed' && (
          <div className="card p-5 sm:p-6 space-y-6">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <p className="eyebrow mb-2">Source video</p>
                <UploadBox file={video} setFile={setVideo} accept="video/*" icon={FileVideo}
                  label="Click to upload or drag and drop" hint="The full-length source" />
              </div>
              <div>
                <p className="eyebrow mb-2">Voiceover (ElevenLabs)</p>
                <UploadBox file={vo} setFile={setVo} accept="audio/*" icon={FileAudio}
                  label="Click to upload or drag and drop" hint="VO.mp3 with [long pause] markers" />
              </div>
            </div>

            <div>
              <p className="eyebrow mb-2">Edit plan (Task 5 JSON)</p>
              <textarea
                value={plan}
                onChange={(e) => setPlan(e.target.value)}
                placeholder={EXAMPLE_PLAN_HINT}
                rows={10}
                className="input-field font-mono text-xs w-full"
              />
            </div>

            <label className="flex items-center gap-2 text-sm text-ink2 cursor-pointer">
              <input type="checkbox" checked={burnCaptions} onChange={(e) => setBurnCaptions(e.target.checked)} />
              Burn captions from the finished narration
            </label>

            {error && (
              <div className="flex items-start gap-2 text-sm text-danger bg-paper2 border border-rule rounded-input p-3">
                <AlertCircle size={16} className="shrink-0 mt-0.5" />
                <span>{error}</span>
              </div>
            )}

            <button
              onClick={handleGenerate}
              disabled={!canGenerate}
              className="btn-primary px-5 py-2.5 text-sm disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {status === 'processing' ? <Loader2 size={16} className="animate-spin" /> : <Clapperboard size={16} />}
              {status === 'processing' ? 'Compiling…' : 'Generate Compilation'}
            </button>
          </div>
        )}

        {status === 'processing' && (
          <div className="card p-5 sm:p-6 space-y-3">
            <p className="eyebrow">Progress</p>
            <div className="bg-paper2 border border-rule rounded-input p-3 max-h-64 overflow-y-auto font-mono text-xs text-ink2 space-y-1">
              {logs.length === 0 && <p className="text-muted">Starting…</p>}
              {logs.map((line, i) => <p key={i}>{line}</p>)}
              <div ref={logsEndRef} />
            </div>
          </div>
        )}

        {status === 'completed' && result && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div className="card aspect-[9/16] max-h-[600px] bg-black overflow-hidden relative">
              <video src={getApiUrl(result.video_url)} controls className="w-full h-full object-contain" autoPlay />
            </div>
            <div className="space-y-4">
              <p className="eyebrow">Done</p>
              <h2 className="font-display lowercase text-2xl text-ink">Compilation ready</h2>
              <div className="flex gap-3 pt-2">
                <a href={getApiUrl(result.video_url)} download className="btn-primary px-4 py-2 text-sm">
                  <Download size={14} /> Download
                </a>
                <button onClick={reset} className="btn-quiet px-4 py-2 text-sm">Start another</button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
