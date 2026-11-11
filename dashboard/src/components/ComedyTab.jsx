import React, { useState, useEffect, useMemo, useCallback } from 'react';
import {
  Smile,
  Upload,
  Check,
  Copy,
  Download,
  Loader2,
  AlertTriangle,
  CheckCircle2,
  Sparkles,
  Film,
  Tv,
  Sliders,
  X,
  Eye,
  ShieldCheck,
} from 'lucide-react';
import LocalFileBrowser from './LocalFileBrowser';
import { thumbUrl } from '../lib/localBrowser';
import { apiFetch } from '../lib/api';
import { getApiUrl } from '../config';

const parsePastedClips = (raw) => {
  if (!raw || !raw.trim()) return [];
  try {
    const cleaned = raw.replace(/```(?:json)?/gi, '').trim();
    const firstBrace = cleaned.indexOf('{');
    const firstBracket = cleaned.indexOf('[');
    const starts = [firstBrace, firstBracket].filter((i) => i !== -1);
    if (!starts.length) return [];
    const start = Math.min(...starts);
    const end = Math.max(cleaned.lastIndexOf('}'), cleaned.lastIndexOf(']'));
    if (end <= start) return [];
    const parsed = JSON.parse(cleaned.slice(start, end + 1));
    const arr = Array.isArray(parsed)
      ? parsed
      : parsed.shorts || parsed.clips || [];
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
};

export default function ComedyTab({ onJobStarted }) {
  // Channel Profile State (Twitter/X Header)
  const [channelName, setChannelName] = useState(
    () => localStorage.getItem('os_comedy_channel_name') || 'Cheerful Videos'
  );
  const [handle, setHandle] = useState(
    () => localStorage.getItem('os_comedy_handle') || '@Cheerfulvids'
  );
  const [verified, setVerified] = useState(
    () => (localStorage.getItem('os_comedy_verified') ?? '1') !== '0'
  );
  const [burnSubtitles, setBurnSubtitles] = useState(
    () => localStorage.getItem('os_comedy_burn_subs') === '1'
  );
  const [avatarUrl, setAvatarUrl] = useState(null);
  const [profileSaved, setProfileSaved] = useState(false);
  const [previewPov, setPreviewPov] = useState('Zero survival instinct 😂');

  // Video & Transcript State
  const [videoPath, setVideoPath] = useState('');
  const [browseOpen, setBrowseOpen] = useState(false);
  const [language, setLanguage] = useState(
    () => localStorage.getItem('os_comedy_language') || 'auto'
  );
  const [layout, setLayout] = useState(
    () => localStorage.getItem('os_comedy_layout') || 'auto'
  );
  const [punchIn, setPunchIn] = useState(
    () => localStorage.getItem('os_comedy_punch_in') === '1'
  );
  const [transcriptFile, setTranscriptFile] = useState(null); // { name, text }
  const [pastedTranscript, setPastedTranscript] = useState('');
  const [showPasteTranscript, setShowPasteTranscript] = useState(false);
  const [manualOffset, setManualOffset] = useState('');

  // Mandatory Metadata State (Series or Movie)
  const [contentType, setContentType] = useState(
    () => localStorage.getItem('os_comedy_content_type') || 'series'
  );
  const [title, setTitle] = useState('');
  const [season, setSeason] = useState('');
  const [episode, setEpisode] = useState('');
  const [episodeTitle, setEpisodeTitle] = useState('');
  const [year, setYear] = useState('');
  const [notes, setNotes] = useState('');

  // Prompt & Whisper Alignment State
  const [promptBusy, setPromptBusy] = useState(false);
  const [transcribeJobId, setTranscribeJobId] = useState('');
  const [transcribeLogs, setTranscribeLogs] = useState([]);
  const [brief, setBrief] = useState(null);
  const [briefCopied, setBriefCopied] = useState(false);
  const [error, setError] = useState('');

  // Clips JSON State
  const [clipsText, setClipsText] = useState('');
  const [editableClips, setEditableClips] = useState([]);
  const [renderBusy, setRenderBusy] = useState(false);

  // Load saved profile from backend on mount
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await apiFetch('/api/comedy/profile');
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (data.channel_name) setChannelName(data.channel_name);
        if (data.handle) setHandle(data.handle);
        if (typeof data.verified === 'boolean') setVerified(data.verified);
        if (typeof data.burn_subtitles === 'boolean') setBurnSubtitles(data.burn_subtitles);
        if (data.avatar_url) setAvatarUrl(`${getApiUrl(data.avatar_url)}?t=${Date.now()}`);
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Poll background Whisper transcription job when no external transcript was attached
  useEffect(() => {
    if (!transcribeJobId || brief) return undefined;
    let cancelled = false;
    const poll = async () => {
      try {
        const sRes = await apiFetch(`/api/status/${transcribeJobId}`);
        if (sRes.ok) {
          const sData = await sRes.json();
          if (!cancelled && Array.isArray(sData.logs)) {
            setTranscribeLogs(sData.logs.slice(-5));
          }
          if (sData.status === 'failed' || sData.status === 'error') {
            if (!cancelled) {
              setPromptBusy(false);
              setError(sData.error || 'Whisper transcription failed.');
              setTranscribeJobId('');
            }
            return;
          }
        }
        const bRes = await apiFetch(`/api/local/brief/${transcribeJobId}`);
        if (bRes.ok && !cancelled) {
          const bData = await bRes.json();
          setBrief(bData);
          setPromptBusy(false);
        }
      } catch {
        /* keep polling */
      }
    };
    const timer = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [transcribeJobId, brief]);

  // Sync parsed clips when clipsText changes
  useEffect(() => {
    const parsed = parsePastedClips(clipsText);
    setEditableClips(parsed);
    if (parsed.length > 0) {
      const firstPov =
        parsed[0].pov_text || parsed[0].viral_hook_text || parsed[0].hook || parsed[0].title;
      if (firstPov) setPreviewPov(firstPov);
    }
  }, [clipsText]);

  // Hard Metadata Gate Validation
  const metadataValidation = useMemo(() => {
    const cleanTitle = title.trim();
    const cleanSeason = season.trim();
    const cleanEpisode = episode.trim();
    if (contentType === 'series') {
      const missing = [];
      if (!cleanTitle) missing.push('Series Title');
      if (!cleanSeason) missing.push('Season #');
      if (!cleanEpisode) missing.push('Episode #');
      return {
        valid: missing.length === 0,
        missing,
        message:
          missing.length > 0
            ? `Required to unlock prompt: enter ${missing.join(', ')}`
            : 'Series metadata complete',
      };
    }
    return {
      valid: Boolean(cleanTitle),
      missing: cleanTitle ? [] : ['Movie Title'],
      message: cleanTitle
        ? 'Movie metadata complete'
        : 'Required to unlock prompt: enter Movie Title',
    };
  }, [contentType, title, season, episode]);

  const saveProfile = useCallback(async () => {
    try {
      localStorage.setItem('os_comedy_channel_name', channelName);
      localStorage.setItem('os_comedy_handle', handle);
      localStorage.setItem('os_comedy_verified', verified ? '1' : '0');
      localStorage.setItem('os_comedy_burn_subs', burnSubtitles ? '1' : '0');
    } catch {
      /* ignore */
    }
    try {
      await apiFetch('/api/comedy/profile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          channel_name: channelName,
          handle,
          verified,
          burn_subtitles: burnSubtitles,
        }),
      });
      setProfileSaved(true);
      setTimeout(() => setProfileSaved(false), 2000);
    } catch (e) {
      setError(`Could not save profile: ${e.message}`);
    }
  }, [channelName, handle, verified, burnSubtitles]);

  const handleAvatarUpload = async (file) => {
    if (!file) return;
    setError('');
    const fd = new FormData();
    fd.append('file', file);
    try {
      const res = await apiFetch('/api/comedy/avatar', {
        method: 'POST',
        body: fd,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Avatar upload failed');
      setAvatarUrl(`${getApiUrl(data.avatar_url)}`);
    } catch (e) {
      setError(e.message);
    }
  };

  const handleTranscriptFile = (file) => {
    if (!file) return;
    setError('');
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || '');
      setTranscriptFile({ name: file.name, text });
      setBrief(null);
    };
    reader.readAsText(file);
  };

  const handleBuildPrompt = async () => {
    setError('');
    if (!metadataValidation.valid) {
      setError(metadataValidation.message);
      return;
    }
    if (!videoPath.trim()) {
      setError('Please select a video file first.');
      return;
    }

    setPromptBusy(true);
    setBrief(null);
    setTranscribeLogs([]);

    const rawTranscript = transcriptFile?.text || pastedTranscript.trim() || null;
    const metadataPayload = {
      content_type: contentType,
      title: title.trim(),
      season: season.trim(),
      episode: episode.trim(),
      episode_title: episodeTitle.trim(),
      year: year.trim(),
      notes: notes.trim(),
    };

    try {
      const res = await apiFetch('/api/comedy/build-prompt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_path: videoPath.trim(),
          metadata: metadataPayload,
          transcript_text: rawTranscript,
          manual_offset: manualOffset.trim() !== '' ? Number(manualOffset) : null,
          language,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(
          typeof data.detail === 'string'
            ? data.detail
            : data.detail?.message || `Failed to build comedy prompt (${res.status})`
        );
      }

      if (data.text) {
        setBrief(data);
        setTranscribeJobId(data.job_id);
        setPromptBusy(false);
      } else if (data.job_id) {
        setTranscribeJobId(data.job_id);
      }
    } catch (e) {
      setPromptBusy(false);
      setError(e.message);
    }
  };

  const copyPrompt = async () => {
    if (!brief?.text) return;
    try {
      await navigator.clipboard.writeText(brief.text);
      setBriefCopied(true);
      setTimeout(() => setBriefCopied(false), 2000);
    } catch {
      /* ignore */
    }
  };

  const downloadPrompt = () => {
    if (!brief?.text) return;
    const blob = new Blob([brief.text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const slug = title.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-') || 'comedy';
    a.download = `${slug}-comedy-prompt.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const updateClipField = (idx, field, value) => {
    const updated = editableClips.map((c, i) => (i === idx ? { ...c, [field]: value } : c));
    setEditableClips(updated);
    setClipsText(JSON.stringify(updated, null, 2));
    if (field === 'pov_text') {
      setPreviewPov(value);
    }
  };

  const handleRenderClips = async (e) => {
    e.preventDefault();
    setError('');
    if (!metadataValidation.valid) {
      setError(metadataValidation.message);
      return;
    }
    if (!videoPath.trim() || !clipsText.trim()) {
      setError('Select the video and paste the AI JSON reply before rendering.');
      return;
    }

    setRenderBusy(true);
    try {
      await saveProfile();
      const requestedLayouts = [layout];
      if (punchIn) requestedLayouts.push('punch_in');
      const rawTranscript = transcriptFile?.text || pastedTranscript.trim() || null;

      const body = {
        video_path: videoPath.trim(),
        clips_json: clipsText,
        language,
        layouts: requestedLayouts,
        comedy_card: true,
        comedy_card_config: {
          channel_name: channelName.trim() || 'Cheerful Videos',
          handle: handle.trim() || '@Cheerfulvids',
          verified,
          burn_subtitles: burnSubtitles,
        },
        comedy_metadata: {
          content_type: contentType,
          title: title.trim(),
          season: season.trim(),
          episode: episode.trim(),
          episode_title: episodeTitle.trim(),
          year: year.trim(),
          notes: notes.trim(),
        },
        ...(transcribeJobId
          ? { transcript_job: transcribeJobId }
          : rawTranscript
          ? { transcript_text: rawTranscript, transcript_name: transcriptFile?.name || 'transcript.srt' }
          : {}),
      };

      const res = await apiFetch('/api/process/local', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(
          typeof data.detail === 'string'
            ? data.detail
            : data.detail?.message || `Render request failed (${res.status})`
        );
      }
      if (onJobStarted && data.job_id) {
        onJobStarted(data.job_id, videoPath.trim());
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setRenderBusy(false);
    }
  };

  const formattedHandle = useMemo(() => {
    const h = handle.trim();
    if (!h) return '';
    return h.startsWith('@') ? h : `@${h}`;
  }, [handle]);

  return (
    <div className="h-full overflow-y-auto custom-scrollbar p-4 sm:p-6 md:p-10 animate-fade">
      <div className="max-w-5xl mx-auto space-y-6">
        {/* Header */}
        <header className="space-y-2">
          <p className="eyebrow flex items-center gap-2">
            <Smile size={13} className="text-brass" /> DEDICATED COMEDY STUDIO · TWITTER/X POST FRAME
          </p>
          <h1 className="font-display lowercase text-3xl sm:text-4xl text-ink">
            Comedy Clips from Movies &amp; Web Series
          </h1>
          <p className="text-sm text-muted max-w-2xl leading-relaxed">
            Verify and align your episode or movie transcript with Whisper, lock in the series/movie metadata,
            discover the funniest scenes via internet databases + transcript timestamps, and render in the
            Twitter/X comedy post frame with face tracking &amp; two-person split screen.
          </p>
        </header>

        {/* Top Grid: Channel Header Config + Live Twitter/X Card Preview */}
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
          {/* Left 7 cols: Channel Header & Workflow Steps */}
          <div className="lg:col-span-7 space-y-5">
            {/* Card 1: Twitter/X Channel Header Style */}
            <div className="card p-5 space-y-4">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div>
                  <p className="eyebrow">STYLE · TWITTER/X HEADER</p>
                  <h2 className="text-base font-medium text-ink">Channel Profile &amp; Frame Options</h2>
                </div>
                <button
                  type="button"
                  onClick={saveProfile}
                  className={profileSaved ? 'badge-ok px-3 py-1.5' : 'btn-quiet text-xs px-3 py-1.5'}
                >
                  {profileSaved ? (
                    <>
                      <Check size={12} /> Saved as default
                    </>
                  ) : (
                    'Save as default'
                  )}
                </button>
              </div>

              <div className="flex items-center gap-4 flex-wrap">
                <div className="relative w-14 h-14 rounded-full overflow-hidden border border-rule bg-paper3 flex items-center justify-center shrink-0">
                  {avatarUrl ? (
                    <img src={avatarUrl} alt="Avatar" className="w-full h-full object-cover" />
                  ) : (
                    <span className="font-bold text-lg text-brass">
                      {(channelName || 'C').trim().slice(0, 1).toUpperCase()}
                    </span>
                  )}
                </div>
                <div className="space-y-1">
                  <label className="btn-quiet text-xs cursor-pointer inline-flex items-center gap-1.5">
                    <Upload size={13} /> Upload Avatar
                    <input
                      type="file"
                      accept="image/png,image/jpeg,image/webp"
                      className="hidden"
                      onChange={(e) => handleAvatarUpload(e.target.files?.[0])}
                    />
                  </label>
                  <p className="text-[11px] text-muted">
                    Top-left circular profile image on the white comedy card.
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <label className="space-y-1 block">
                  <span className="text-xs text-muted">Channel Name</span>
                  <input
                    type="text"
                    value={channelName}
                    onChange={(e) => setChannelName(e.target.value)}
                    placeholder="Cheerful Videos"
                    className="input-field text-sm"
                  />
                </label>
                <label className="space-y-1 block">
                  <span className="text-xs text-muted">@Handle</span>
                  <input
                    type="text"
                    value={handle}
                    onChange={(e) => setHandle(e.target.value)}
                    placeholder="@Cheerfulvids"
                    className="input-field text-sm"
                  />
                </label>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
                <label className="flex items-center justify-between gap-2 rounded-input border border-rule bg-paper3 px-3 py-2 text-xs text-ink2 cursor-pointer">
                  <span className="flex items-center gap-1.5">
                    <span>Blue verified checkmark</span>
                    <span className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-[#1D9BF0] text-white text-[10px] font-bold">
                      ✓
                    </span>
                  </span>
                  <input
                    type="checkbox"
                    checked={verified}
                    onChange={(e) => setVerified(e.target.checked)}
                    className="accent-[var(--color-accent)]"
                  />
                </label>

                <label className="flex items-center justify-between gap-2 rounded-input border border-rule bg-paper3 px-3 py-2 text-xs text-ink2 cursor-pointer">
                  <span>Burn subtitles inside frame</span>
                  <input
                    type="checkbox"
                    checked={burnSubtitles}
                    onChange={(e) => setBurnSubtitles(e.target.checked)}
                    className="accent-[var(--color-accent)]"
                  />
                </label>
              </div>
            </div>

            {/* Card 2: Step 1 — Video & Whisper Transcript Verification */}
            <div className="card p-5 space-y-4">
              <div>
                <p className="eyebrow">STEP 1 · VIDEO &amp; WHISPER TIMESTAMP ALIGNMENT</p>
                <h2 className="text-base font-medium text-ink">
                  Select Long-Form Video &amp; Transcript
                </h2>
              </div>

              <div className="space-y-2">
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={videoPath}
                    onChange={(e) => {
                      setVideoPath(e.target.value);
                      setError('');
                    }}
                    placeholder="Path to full movie or web series episode on this computer..."
                    spellCheck={false}
                    className="input-field flex-1 font-mono text-xs"
                  />
                  <button
                    type="button"
                    onClick={() => setBrowseOpen(true)}
                    className="btn-quiet shrink-0 text-xs"
                  >
                    Browse…
                  </button>
                </div>

                {videoPath.trim() && (
                  <div className="flex items-center gap-2.5 rounded-input border border-rule bg-paper3 p-2">
                    <img
                      src={thumbUrl(videoPath.trim().replace(/^"|"$/g, ''))}
                      alt=""
                      className="w-[104px] h-[58px] object-cover rounded border border-rule shrink-0 bg-paper2"
                      onError={(e) => {
                        e.currentTarget.style.visibility = 'hidden';
                      }}
                    />
                    <p className="text-xs text-ink2 min-w-0 truncate">
                      {videoPath.trim().replace(/^"|"$/g, '').split(/[\\/]/).pop()}
                    </p>
                  </div>
                )}
              </div>

              {/* Transcript Upload / Paste + Whisper Sync Explanation */}
              <div className="rounded-input border border-rule bg-paper3 p-3 space-y-2.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <label className="btn-quiet cursor-pointer text-xs">
                      <input
                        type="file"
                        accept=".srt,.vtt,.txt,.json,text/plain,application/json"
                        className="hidden"
                        onChange={(e) => handleTranscriptFile(e.target.files?.[0])}
                      />
                      {transcriptFile ? 'Replace transcript (.srt/.vtt/.txt)' : 'Upload transcript (.srt/.vtt/.txt)'}
                    </label>
                    <button
                      type="button"
                      onClick={() => setShowPasteTranscript(!showPasteTranscript)}
                      className="btn-quiet text-xs"
                    >
                      {showPasteTranscript ? 'Hide paste box' : 'Paste transcript'}
                    </button>
                  </div>

                  {transcriptFile && (
                    <div className="flex items-center gap-1.5">
                      <span className="text-[11px] font-mono text-ink2 truncate max-w-[180px]">
                        {transcriptFile.name}
                      </span>
                      <button
                        type="button"
                        onClick={() => setTranscriptFile(null)}
                        className="text-muted hover:text-ink"
                      >
                        <X size={14} />
                      </button>
                    </div>
                  )}
                </div>

                {showPasteTranscript && (
                  <textarea
                    value={pastedTranscript}
                    onChange={(e) => {
                      setPastedTranscript(e.target.value);
                      setBrief(null);
                    }}
                    rows={3}
                    placeholder="Paste .srt, .vtt, Whisper JSON, or timestamped lines (0:14 dialogue)..."
                    className="w-full rounded border border-rule bg-paper2 p-2 font-mono text-[11px] text-ink outline-none"
                  />
                )}

                <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                  <p className="text-[11px] text-muted flex-1">
                    {transcriptFile || pastedTranscript.trim()
                      ? 'Whisper will verify and align your transcript timestamps against the audio track automatically.'
                      : 'No transcript attached — Whisper will transcribe the full video audio from scratch.'}
                  </p>
                  {(transcriptFile || pastedTranscript.trim()) && (
                    <div className="flex items-center gap-1.5">
                      <span className="text-[11px] text-muted">Manual offset (s):</span>
                      <input
                        type="number"
                        step="0.1"
                        value={manualOffset}
                        onChange={(e) => setManualOffset(e.target.value)}
                        placeholder="auto"
                        className="w-20 rounded border border-rule bg-paper2 px-2 py-1 font-mono text-xs text-ink"
                      />
                    </div>
                  )}
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <label className="space-y-1 block">
                  <span className="text-xs text-muted">Spoken Language</span>
                  <select
                    value={language}
                    onChange={(e) => {
                      setLanguage(e.target.value);
                      try {
                        localStorage.setItem('os_comedy_language', e.target.value);
                      } catch {
                        /* ignore */
                      }
                    }}
                    className="input-field text-xs py-2"
                  >
                    <option value="auto">Auto-detect</option>
                    <option value="hinglish">Hinglish (Hindi in Latin letters)</option>
                    <option value="hi">Hindi (Devanagari)</option>
                    <option value="en">English</option>
                    <option value="es">Spanish</option>
                    <option value="pt">Portuguese</option>
                    <option value="fr">French</option>
                    <option value="de">German</option>
                  </select>
                </label>

                <label className="space-y-1 block">
                  <span className="text-xs text-muted">Camera Tracking Inside Frame</span>
                  <select
                    value={layout}
                    onChange={(e) => {
                      setLayout(e.target.value);
                      try {
                        localStorage.setItem('os_comedy_layout', e.target.value);
                      } catch {
                        /* ignore */
                      }
                    }}
                    className="input-field text-xs py-2"
                  >
                    <option value="auto">Auto (face track + 2-person split screen)</option>
                    <option value="split">Two people: split screen (stacked)</option>
                    <option value="speaker_cut">Two people: cut to active speaker</option>
                    <option value="none">Single crop only (1 person track)</option>
                  </select>
                </label>
              </div>
            </div>

            {/* Card 3: Step 2 — Mandatory Metadata Gate */}
            <div className="card p-5 space-y-4 border-brass/40">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div>
                  <p className="eyebrow">STEP 2 · MANDATORY METADATA GATE</p>
                  <h2 className="text-base font-medium text-ink">
                    Series / Movie Metadata (Required to Build Prompt)
                  </h2>
                </div>
                <span className={metadataValidation.valid ? 'badge-ok' : 'badge-warn'}>
                  {metadataValidation.valid ? (
                    <>
                      <Check size={12} /> Ready
                    </>
                  ) : (
                    <>
                      <AlertTriangle size={12} /> Required
                    </>
                  )}
                </span>
              </div>

              {/* Content Type Toggle */}
              <div className="grid grid-cols-2 gap-2 p-1 rounded-input bg-paper3 border border-rule">
                <button
                  type="button"
                  onClick={() => {
                    setContentType('series');
                    try {
                      localStorage.setItem('os_comedy_content_type', 'series');
                    } catch {
                      /* ignore */
                    }
                  }}
                  className={`flex items-center justify-center gap-2 py-2 rounded text-xs font-medium transition-colors ${
                    contentType === 'series' ? 'bg-paper text-ink shadow-sm' : 'text-muted hover:text-ink2'
                  }`}
                >
                  <Tv size={14} className={contentType === 'series' ? 'text-brass' : ''} />
                  Web Series / TV Episode
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setContentType('movie');
                    try {
                      localStorage.setItem('os_comedy_content_type', 'movie');
                    } catch {
                      /* ignore */
                    }
                  }}
                  className={`flex items-center justify-center gap-2 py-2 rounded text-xs font-medium transition-colors ${
                    contentType === 'movie' ? 'bg-paper text-ink shadow-sm' : 'text-muted hover:text-ink2'
                  }`}
                >
                  <Film size={14} className={contentType === 'movie' ? 'text-brass' : ''} />
                  Movie / Film
                </button>
              </div>

              {contentType === 'series' ? (
                <div className="space-y-3">
                  <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
                    <label className="sm:col-span-2 space-y-1 block">
                      <span className="text-xs text-ink2 font-medium">
                        Series Title <span className="text-bad">*</span>
                      </span>
                      <input
                        type="text"
                        value={title}
                        onChange={(e) => setTitle(e.target.value)}
                        placeholder="e.g. The Office / Panchayat / Friends"
                        className="input-field text-sm"
                      />
                    </label>
                    <label className="space-y-1 block">
                      <span className="text-xs text-ink2 font-medium">
                        Season # <span className="text-bad">*</span>
                      </span>
                      <input
                        type="text"
                        value={season}
                        onChange={(e) => setSeason(e.target.value)}
                        placeholder="e.g. 5"
                        className="input-field text-sm font-mono"
                      />
                    </label>
                    <label className="space-y-1 block">
                      <span className="text-xs text-ink2 font-medium">
                        Episode # <span className="text-bad">*</span>
                      </span>
                      <input
                        type="text"
                        value={episode}
                        onChange={(e) => setEpisode(e.target.value)}
                        placeholder="e.g. 14"
                        className="input-field text-sm font-mono"
                      />
                    </label>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <label className="space-y-1 block">
                      <span className="text-xs text-muted">Episode Title (optional)</span>
                      <input
                        type="text"
                        value={episodeTitle}
                        onChange={(e) => setEpisodeTitle(e.target.value)}
                        placeholder="e.g. Stress Relief"
                        className="input-field text-xs"
                      />
                    </label>
                    <label className="space-y-1 block">
                      <span className="text-xs text-muted">Character / Scene Notes (optional)</span>
                      <input
                        type="text"
                        value={notes}
                        onChange={(e) => setNotes(e.target.value)}
                        placeholder="e.g. Focus on Dwight & Michael cold open"
                        className="input-field text-xs"
                      />
                    </label>
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    <label className="sm:col-span-2 space-y-1 block">
                      <span className="text-xs text-ink2 font-medium">
                        Movie Title <span className="text-bad">*</span>
                      </span>
                      <input
                        type="text"
                        value={title}
                        onChange={(e) => setTitle(e.target.value)}
                        placeholder="e.g. Hera Pheri / Superbad / The Hangover"
                        className="input-field text-sm"
                      />
                    </label>
                    <label className="space-y-1 block">
                      <span className="text-xs text-muted">Release Year (optional)</span>
                      <input
                        type="text"
                        value={year}
                        onChange={(e) => setYear(e.target.value)}
                        placeholder="e.g. 2000"
                        className="input-field text-sm font-mono"
                      />
                    </label>
                  </div>
                  <label className="space-y-1 block">
                    <span className="text-xs text-muted">Character / Scene Notes (optional)</span>
                    <input
                      type="text"
                      value={notes}
                      onChange={(e) => setNotes(e.target.value)}
                      placeholder="e.g. Baburao wrong number calls & Paresh Rawal punchlines"
                      className="input-field text-xs"
                    />
                  </label>
                </div>
              )}

              {!metadataValidation.valid && (
                <div className="rounded-input border border-warn/50 bg-paper3 px-3 py-2 text-xs text-warn flex items-center gap-2">
                  <AlertTriangle size={14} className="shrink-0" />
                  <span>{metadataValidation.message} — prompt generation is locked until filled.</span>
                </div>
              )}

              <button
                type="button"
                onClick={handleBuildPrompt}
                disabled={!metadataValidation.valid || !videoPath.trim() || promptBusy}
                className="w-full btn-primary py-2.5 text-sm disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {promptBusy ? (
                  <>
                    <Loader2 size={15} className="animate-spin" />
                    <span>
                      {transcriptFile || pastedTranscript.trim()
                        ? 'Verifying audio timestamps with Whisper & building prompt…'
                        : 'Transcribing with Whisper & building comedy prompt…'}
                    </span>
                  </>
                ) : transcriptFile || pastedTranscript.trim() ? (
                  <>
                    <ShieldCheck size={16} /> Verify Transcript with Whisper &amp; Build Comedy Prompt
                  </>
                ) : (
                  <>
                    <Sparkles size={16} /> Transcribe Video &amp; Build Comedy Prompt
                  </>
                )}
              </button>

              {promptBusy && transcribeLogs.length > 0 && (
                <pre className="rounded border border-rule bg-paper3 p-2 font-mono text-[11px] text-muted max-h-24 overflow-y-auto whitespace-pre-wrap">
                  {transcribeLogs.join('\n')}
                </pre>
              )}
            </div>

            {/* Card 4: Step 3 — Generated Prompt + Whisper Alignment Report */}
            {brief && (
              <div className="card p-5 space-y-3 animate-fade">
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <div>
                    <p className="eyebrow">STEP 3 · PASTE INTO CHATGPT / GEMINI / CLAUDE</p>
                    <h2 className="text-base font-medium text-ink">
                      Internet-Database + Aligned Transcript Prompt Ready
                    </h2>
                  </div>
                  {brief.alignment && (
                    <span className="badge-ok text-[11px]">
                      <CheckCircle2 size={12} />
                      Whisper Offset: {brief.alignment.offset >= 0 ? `+${brief.alignment.offset}` : brief.alignment.offset}s
                      {typeof brief.alignment.confidence === 'number'
                        ? ` (${Math.round(brief.alignment.confidence * 100)}% conf)`
                        : ''}
                    </span>
                  )}
                </div>

                <pre className="max-h-36 overflow-auto rounded-input border border-rule2 bg-paper p-3 font-mono text-[11px] leading-relaxed text-muted whitespace-pre-wrap">
                  {brief.text.slice(0, 900)}
                  {brief.text.length > 900 ? '\n…' : ''}
                </pre>

                <div className="flex flex-wrap items-center gap-2">
                  <button type="button" onClick={copyPrompt} className="btn-primary text-xs">
                    {briefCopied ? (
                      <>
                        <Check size={14} /> Copied Prompt
                      </>
                    ) : (
                      <>
                        <Copy size={14} /> Copy Prompt to Clipboard
                      </>
                    )}
                  </button>
                  <button type="button" onClick={downloadPrompt} className="btn-ghost text-xs">
                    <Download size={14} /> Download .txt
                  </button>
                  <span className="text-[11px] text-muted ml-auto">
                    {brief.segments ? `${brief.segments} aligned segments · ` : ''}
                    asks for {brief.min_clips || 3}–{brief.max_clips || 8} comedy clips
                  </span>
                </div>
              </div>
            )}

            {/* Card 5: Step 4 — Paste AI JSON Reply, Edit POV Captions & Render */}
            <form onSubmit={handleRenderClips} className="card p-5 space-y-4">
              <div>
                <p className="eyebrow">STEP 4 · PASTE AI REPLY &amp; RENDER COMEDY CLIPS</p>
                <h2 className="text-base font-medium text-ink">
                  Paste Comedy Clips JSON &amp; Customize POV Text
                </h2>
              </div>

              <textarea
                value={clipsText}
                onChange={(e) => {
                  setClipsText(e.target.value);
                  setError('');
                }}
                rows={4}
                spellCheck={false}
                placeholder='Paste the JSON reply from ChatGPT / Claude / Gemini — [{"start": 124.5, "end": 162.0, "title": "Fire Drill", "pov_text": "Zero survival instinct 😂"}]'
                className="input-field w-full font-mono text-xs leading-relaxed"
              />

              {/* Per-clip inline POV caption editor */}
              {editableClips.length > 0 && (
                <div className="space-y-2.5">
                  <p className="text-xs font-medium text-ink2">
                    {editableClips.length} comedy clip{editableClips.length === 1 ? '' : 's'} ready — edit any clip&apos;s POV caption below:
                  </p>
                  <div className="space-y-2 max-h-64 overflow-y-auto custom-scrollbar pr-1">
                    {editableClips.map((clip, idx) => {
                      const pov =
                        clip.pov_text ?? clip.viral_hook_text ?? clip.hook ?? clip.title ?? '';
                      return (
                        <div
                          key={idx}
                          className="rounded-input border border-rule bg-paper3 p-2.5 space-y-1.5"
                        >
                          <div className="flex items-center justify-between gap-2 text-xs">
                            <span className="font-mono text-brass">
                              #{idx + 1} · {clip.start}s – {clip.end}s
                            </span>
                            <button
                              type="button"
                              onClick={() => setPreviewPov(pov)}
                              className="text-[11px] text-muted hover:text-ink inline-flex items-center gap-1"
                            >
                              <Eye size={12} /> Preview on card
                            </button>
                          </div>
                          <input
                            type="text"
                            value={pov}
                            onChange={(e) => updateClipField(idx, 'pov_text', e.target.value)}
                            placeholder="POV caption above video (e.g. Zero survival instinct 😂)"
                            className="input-field text-xs py-1.5"
                          />
                          {clip.why_funny && (
                            <p className="text-[11px] text-muted truncate">{clip.why_funny}</p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              <label className="flex items-center justify-between gap-2 text-xs text-ink2 cursor-pointer">
                <span>Punch-in zoom on audio comedy beats</span>
                <input
                  type="checkbox"
                  checked={punchIn}
                  onChange={(e) => {
                    setPunchIn(e.target.checked);
                    try {
                      localStorage.setItem('os_comedy_punch_in', e.target.checked ? '1' : '0');
                    } catch {
                      /* ignore */
                    }
                  }}
                  className="accent-[var(--color-accent)]"
                />
              </label>

              {error && (
                <div className="rounded-input border border-bad/50 bg-paper3 px-3 py-2 text-xs text-bad flex items-center gap-2">
                  <AlertTriangle size={14} className="shrink-0" />
                  <span>{error}</span>
                </div>
              )}

              <button
                type="submit"
                disabled={
                  !metadataValidation.valid ||
                  !videoPath.trim() ||
                  !clipsText.trim() ||
                  renderBusy
                }
                className="w-full btn-primary py-2.5 text-sm disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {renderBusy ? (
                  <>
                    <Loader2 size={15} className="animate-spin" /> Starting Comedy Render…
                  </>
                ) : (
                  <>
                    <Sparkles size={16} /> Render Comedy Clips in Twitter/X Frame
                  </>
                )}
              </button>
            </form>
          </div>

          {/* Right 5 cols: Sticky Live Twitter/X Post Frame Mockup */}
          <div className="lg:col-span-5 lg:sticky lg:top-6 space-y-3">
            <div className="card p-4 space-y-3">
              <div className="flex items-center justify-between">
                <p className="eyebrow">LIVE FRAME PREVIEW · 9:16 (1080×1920)</p>
                <span className="readout">Twitter/X Comedy Card</span>
              </div>

              {/* 9:16 White Twitter/X Card Mockup */}
              <div
                className="mx-auto w-full max-w-[300px] aspect-[9/16] rounded-2xl border border-rule2 shadow-lg flex flex-col px-4 pt-9 pb-7 select-none"
                style={{ backgroundColor: '#FFFFFF', color: '#0F1419' }}
              >
                {/* Profile Header */}
                <div className="flex items-center gap-2.5">
                  <div className="w-9 h-9 rounded-full overflow-hidden bg-[#1D9BF0] flex items-center justify-center shrink-0 text-white font-bold text-sm">
                    {avatarUrl ? (
                      <img src={avatarUrl} alt="" className="w-full h-full object-cover" />
                    ) : (
                      (channelName || 'C').trim().slice(0, 1).toUpperCase()
                    )}
                  </div>
                  <div className="min-w-0 leading-tight">
                    <div className="flex items-center gap-1">
                      <span
                        className="font-bold text-[13px] truncate"
                        style={{ color: '#0F1419' }}
                      >
                        {channelName.trim() || 'Cheerful Videos'}
                      </span>
                      {verified && (
                        <span
                          className="inline-flex items-center justify-center w-3.5 h-3.5 rounded-full text-white text-[9px] font-bold shrink-0"
                          style={{ backgroundColor: '#1D9BF0' }}
                          title="Verified"
                        >
                          ✓
                        </span>
                      )}
                    </div>
                    {formattedHandle && (
                      <div className="text-[11px] truncate" style={{ color: '#536471' }}>
                        {formattedHandle}
                      </div>
                    )}
                  </div>
                </div>

                {/* POV Caption Text */}
                <div
                  className="mt-2.5 mb-2 text-[13.5px] font-normal leading-snug break-words"
                  style={{ color: '#0F1419' }}
                >
                  {previewPov || 'Zero survival instinct 😂'}
                </div>

                {/* Square-ish Rounded Video Window */}
                <div className="relative w-full flex-1 max-h-[330px] rounded-[10px] overflow-hidden bg-neutral-900 border border-black/10 flex items-center justify-center">
                  {videoPath.trim() ? (
                    <img
                      src={thumbUrl(videoPath.trim().replace(/^"|"$/g, ''))}
                      alt="Video preview"
                      className="w-full h-full object-cover"
                    />
                  ) : (
                    <div className="text-center px-4 space-y-1">
                      <Film size={24} className="mx-auto text-neutral-400" />
                      <p className="text-[11px] text-neutral-300">
                        Face-tracked / 2-person split-screen video renders inside this rounded frame
                      </p>
                    </div>
                  )}

                  {burnSubtitles && (
                    <div className="absolute bottom-3 inset-x-3 text-center">
                      <span className="px-2 py-0.5 rounded bg-black/75 text-white text-[10px] font-bold uppercase tracking-wide">
                        Sample subtitle line
                      </span>
                    </div>
                  )}
                </div>
              </div>

              <div className="space-y-1.5">
                <label className="text-[11px] text-muted block">
                  Test POV Caption in Preview:
                </label>
                <input
                  type="text"
                  value={previewPov}
                  onChange={(e) => setPreviewPov(e.target.value)}
                  className="input-field text-xs py-1.5"
                  placeholder="Zero survival instinct 😂"
                />
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* File Browser Modal */}
      {browseOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
          onClick={() => setBrowseOpen(false)}
        >
          <div
            className="w-full max-w-lg h-[76vh] flex flex-col rounded-input border border-rule2 bg-paper2 p-3"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 mb-2">
              <p className="eyebrow flex-1 truncate">Choose a video on this computer</p>
              <button
                type="button"
                onClick={() => setBrowseOpen(false)}
                className="text-muted hover:text-ink2"
                aria-label="close"
              >
                <X size={16} />
              </button>
            </div>
            <LocalFileBrowser
              kind="video"
              onPick={(picked) => {
                setVideoPath(picked);
                setBrowseOpen(false);
                setError('');
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}
