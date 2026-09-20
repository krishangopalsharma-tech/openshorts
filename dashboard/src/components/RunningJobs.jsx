import { useCallback, useEffect, useState } from 'react';
import { Loader2, Square, ExternalLink } from 'lucide-react';
import { apiFetch } from '../lib/api';

/**
 * What is running right now, and a way to stop it.
 *
 * Two things this exists for, both of which cost a real evening:
 *
 * - nothing showed that a job was still going, so the same video was
 *   submitted four times in five minutes; four main.py processes then loaded
 *   demucs and whisper onto one 8 GB card and the machine ran out of memory.
 * - stopping the backend did not stop the work. The resume manifest
 *   re-enqueued it on the next start, so "I killed the server" and "the job
 *   is cancelled" were different facts. Stop writes that down.
 *
 * Polls rather than streams: the list is tiny, the backend already answers
 * /api/status on a timer, and a websocket for four rows is not worth the
 * reconnect logic.
 */

const POLL_MS = 4000;

const elapsed = (seconds) => {
  if (!seconds && seconds !== 0) return '';
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
};

export default function RunningJobs({ onOpen, currentJobId }) {
  const [jobs, setJobs] = useState([]);
  const [maxConcurrent, setMaxConcurrent] = useState(null);
  const [stopping, setStopping] = useState({});
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const res = await apiFetch('/api/jobs');
      if (!res.ok) return;                 // 404 in cloud builds without it
      const data = await res.json();
      setJobs(data.jobs || []);
      setMaxConcurrent(data.max_concurrent ?? null);
    } catch { /* a poll that misses is not worth an error line */ }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const stop = async (jobId) => {
    setStopping((prev) => ({ ...prev, [jobId]: true }));
    setError('');
    try {
      const res = await apiFetch(`/api/job/${jobId}/cancel`, { method: 'POST' });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(typeof body.detail === 'string' ? body.detail : 'Could not stop that job.');
      }
    } catch {
      setError('Could not reach the backend.');
    } finally {
      setStopping((prev) => ({ ...prev, [jobId]: false }));
      load();
    }
  };

  if (!jobs.length) return null;

  return (
    <div className="max-w-xl mx-auto text-left rounded-input border border-rule bg-paper2 p-3 space-y-2">
      <div className="flex items-center gap-2">
        <Loader2 size={13} className="animate-spin text-brass shrink-0" />
        <p className="eyebrow flex-1">
          {jobs.length} job{jobs.length > 1 ? 's' : ''} running
          {maxConcurrent ? ` · ${maxConcurrent} at a time` : ''}
        </p>
      </div>

      {jobs.map((job) => (
        <div key={job.job_id}
             className="flex items-center gap-2 rounded border border-rule bg-paper3 px-2.5 py-2">
          <div className="min-w-0 flex-1">
            <p className="text-[13px] text-ink truncate">{job.name}</p>
            <p className="text-[11px] text-muted truncate">
              {[job.status,
                job.transcribe_only ? 'transcript only' : null,
                elapsed(job.elapsed_seconds)].filter(Boolean).join(' · ')}
              {job.last_log ? ` — ${job.last_log}` : ''}
            </p>
          </div>
          {job.job_id !== currentJobId && (
            <button
              type="button"
              onClick={() => onOpen?.(job.job_id)}
              className="btn-quiet shrink-0 text-[12px] flex items-center gap-1.5"
              title="Follow this job"
            >
              <ExternalLink size={13} /> open
            </button>
          )}
          <button
            type="button"
            onClick={() => stop(job.job_id)}
            disabled={!!stopping[job.job_id]}
            className="btn-quiet shrink-0 text-[12px] flex items-center gap-1.5 hover:text-bad"
            title="Stop this job"
          >
            {stopping[job.job_id]
              ? <Loader2 size={13} className="animate-spin" />
              : <Square size={12} />}
            stop
          </button>
        </div>
      ))}

      {error && <p className="text-[11px] text-bad">{error}</p>}
    </div>
  );
}
