import React, { useState, useEffect, useRef } from 'react';
import { Link2, Upload, FileVideo, X, Info, Loader2, ChevronDown, AlertTriangle } from 'lucide-react';
import { getApiUrl } from '../config';

const SUPPORTED_PLATFORMS = [
    'YouTube', 'Vimeo', 'TikTok', 'X / Twitter', 'Twitch',
    'Facebook', 'Instagram', 'Dailymotion', 'Reddit', 'Streamable',
];

export default function MediaInput({ onProcess, isProcessing }) {
    const [youtubeUrlEnabled, setYoutubeUrlEnabled] = useState(true);
    // File upload is the primary path; the link is secondary.
    const [mode, setMode] = useState('file'); // 'file' | 'url'
    const [url, setUrl] = useState('');
    const [file, setFile] = useState(null);
    const [acknowledged, setAcknowledged] = useState(false);
    // A source this pipeline has already cut. Asked before submitting, because
    // an accidental re-upload costs a full transcription plus a render per clip
    // and there is nothing later in the flow that would catch it.
    const [duplicate, setDuplicate] = useState(null);
    const [duplicateAcked, setDuplicateAcked] = useState(false);
    const [showInfo, setShowInfo] = useState(false);
    // Advanced generation controls — empty string means "let the AI decide",
    // which keeps the default pipeline behavior untouched.
    const [showAdvanced, setShowAdvanced] = useState(false);
    const [targetClips, setTargetClips] = useState('');
    const [clipMinSeconds, setClipMinSeconds] = useState('');
    const [clipMaxSeconds, setClipMaxSeconds] = useState('');
    // Layout: 'auto' lets the AI pick per video (server default); the others
    // force one on so a podcast host who knows what they uploaded doesn't
    // depend on the detector, and 'none' keeps the plain single crop.
    const [layout, setLayout] = useState(() => {
        try { return localStorage.getItem('os_layout') || 'auto'; } catch { return 'auto'; }
    });
    // Spoken language. 'auto' is whisper's own detection, right almost
    // everywhere; naming the language matters on Hindi/Urdu, where auto-detect
    // slides into English translation. 'hinglish' transcribes Hindi and writes
    // it in Latin letters, so the caption presets (all Latin display faces)
    // still apply.
    const [language, setLanguage] = useState(() => {
        try { return localStorage.getItem('os_language') || 'auto'; } catch { return 'auto'; }
    });
    // Names and domain words for the decode. Whisper writes a name it has
    // never heard the way it sounded ("अजीए" for अजय); listing it fixes that
    // word. Not remembered across sessions — it belongs to one video.
    const [transcribePrompt, setTranscribePrompt] = useState('');
    // Output format, cinematic look, captions and hook titles are no longer
    // chosen here: every clip renders plain 9:16 and the user picks those per
    // clip (or for all clips) from the result card afterwards.
    const infoRef = useRef(null);

    // Close the compatibility popover on any outside click.
    useEffect(() => {
        if (!showInfo) return;
        const onClick = (e) => {
            if (infoRef.current && !infoRef.current.contains(e.target)) setShowInfo(false);
        };
        document.addEventListener('mousedown', onClick);
        return () => document.removeEventListener('mousedown', onClick);
    }, [showInfo]);

    useEffect(() => {
        fetch(getApiUrl('/api/config'))
            .then((r) => r.ok ? r.json() : null)
            .then((cfg) => {
                if (cfg && cfg.youtubeUrlEnabled === false) {
                    setYoutubeUrlEnabled(false);
                    setMode('file');
                }
            })
            .catch(() => {});
    }, []);

    // A link pasted in the landing hero: preload it here so the user picks up
    // where they left off. Not auto-submitted — the rights attestation below
    // has to be ticked by the user.
    useEffect(() => {
        let pending = null;
        try {
            pending = localStorage.getItem('os_pending_url');
            if (pending) localStorage.removeItem('os_pending_url');
        } catch { /* ignore */ }
        if (pending) {
            setMode('url');
            setUrl(pending);
        }
    }, []);

    // Ask about the current source. Only the name, size and URL go over the
    // wire — never the file, or checking a 600 MB upload would cost as much as
    // submitting it. Debounced because the URL box fires this per keystroke.
    useEffect(() => {
        const body = mode === 'url'
            ? (url.trim() ? { url: url.trim() } : null)
            : (file ? { title: file.name, size_bytes: file.size } : null);
        setDuplicate(null);
        setDuplicateAcked(false);
        if (!body) return;

        let live = true;
        const timer = setTimeout(async () => {
            try {
                const r = await fetch(`${getApiUrl()}/api/source/check`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!r.ok) return;
                const d = await r.json();
                // The check is advisory: a failure here must never stop a submit.
                if (live && d.duplicate) setDuplicate(d);
            } catch { /* offline or old server: no warning, submit still works */ }
        }, mode === 'url' ? 600 : 0);

        return () => { live = false; clearTimeout(timer); };
    }, [mode, url, file]);

    const blockedByDuplicate = !!duplicate && !duplicateAcked;

    const handleSubmit = (e) => {
        e.preventDefault();
        if (!acknowledged || blockedByDuplicate) return;
        const advanced = {
            targetClips: targetClips || null,
            clipMinSeconds: clipMinSeconds || null,
            clipMaxSeconds: clipMaxSeconds || null,
            layout,
            language,
            transcribePrompt: transcribePrompt.trim() || null,
        };
        try {
            localStorage.setItem('os_layout', layout);
            localStorage.setItem('os_language', language);
        } catch { /* ignore */ }
        if (mode === 'url' && url) {
            onProcess({ type: 'url', payload: url, acknowledged: true, ...advanced });
        } else if (mode === 'file' && file) {
            onProcess({ type: 'file', payload: file, acknowledged: true, ...advanced });
        }
    };

    const handleDrop = (e) => {
        e.preventDefault();
        if (e.dataTransfer.files && e.dataTransfer.files[0]) {
            setFile(e.dataTransfer.files[0]);
            setMode('file');
        }
    };

    return (
        <div className="card p-4 sm:p-6 animate-fade">
            <div className="flex gap-4 sm:gap-6 mb-6 border-b border-rule" data-tutorial="source-tabs">
                <button
                    onClick={() => setMode('file')}
                    className={`flex items-center gap-2 pb-3 px-1 -mb-px border-b-2 text-sm lowercase whitespace-nowrap transition-colors ${mode === 'file'
                        ? 'text-ink border-brass'
                        : 'text-muted border-transparent hover:text-ink2'
                        }`}
                >
                    <Upload size={16} className={`hidden sm:block ${mode === 'file' ? 'text-brass' : ''}`} />
                    Upload File
                </button>
                {youtubeUrlEnabled && (
                    <button
                        onClick={() => setMode('url')}
                        className={`flex items-center gap-2 pb-3 px-1 -mb-px border-b-2 text-sm lowercase whitespace-nowrap transition-colors ${mode === 'url'
                            ? 'text-ink border-brass'
                            : 'text-muted border-transparent hover:text-ink2'
                            }`}
                    >
                        <Link2 size={16} className={`hidden sm:block ${mode === 'url' ? 'text-brass' : ''}`} />
                        Video URL
                    </button>
                )}
            </div>

            <form onSubmit={handleSubmit}>
                {mode === 'url' ? (
                    <div className="space-y-4" data-tutorial="drop-zone">
                        <div className="relative">
                            <input
                                type="url"
                                value={url}
                                onChange={(e) => setUrl(e.target.value)}
                                placeholder="https://... paste a video link"
                                className="input-field pr-11"
                                required
                            />
                            <div className="absolute inset-y-0 right-2 flex items-center" ref={infoRef}>
                                <button
                                    type="button"
                                    onClick={() => setShowInfo((v) => !v)}
                                    aria-label="Supported platforms"
                                    className="p-1.5 text-muted hover:text-brass transition-colors"
                                >
                                    <Info size={16} />
                                </button>
                                {showInfo && (
                                    <div className="absolute right-0 top-full mt-2 w-64 z-20 card p-4 text-left animate-fade">
                                        <p className="eyebrow mb-2">Paste a link from</p>
                                        <div className="flex flex-wrap gap-1.5">
                                            {SUPPORTED_PLATFORMS.map((p) => (
                                                <span key={p} className="text-xs px-2 py-0.5 rounded-full bg-paper3 text-ink2">
                                                    {p}
                                                </span>
                                            ))}
                                        </div>
                                        <p className="text-xs text-muted mt-2.5 leading-relaxed">
                                            …and 1,000+ more sites. If a link has a public video, we can usually fetch it.
                                        </p>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                ) : (
                    <div
                        data-tutorial="drop-zone"
                        className={`border-2 border-dashed rounded-card p-6 sm:p-8 text-center transition-colors ${file ? 'border-brass' : 'border-rule2 hover:border-brass'
                            }`}
                        onDragOver={(e) => e.preventDefault()}
                        onDrop={handleDrop}
                    >
                        {file ? (
                            <div className="flex items-center justify-center gap-3 text-ok min-w-0">
                                <FileVideo size={18} className="shrink-0" />
                                <span className="font-medium truncate">{file.name}</span>
                                <button
                                    type="button"
                                    onClick={() => setFile(null)}
                                    className="p-1 text-muted hover:text-ink hover:bg-paper3 rounded-full transition-colors"
                                >
                                    <X size={16} />
                                </button>
                            </div>
                        ) : (
                            <label className="cursor-pointer block">
                                <input
                                    type="file"
                                    accept="video/*"
                                    onChange={(e) => setFile(e.target.files?.[0] || null)}
                                    className="hidden"
                                />
                                <Upload className="mx-auto mb-3 text-muted" size={18} />
                                <p className="text-ink2 lowercase">Click to upload or drag and drop</p>
                                <p className="readout mt-2">MP4, MOV up to 500MB</p>
                            </label>
                        )}
                    </div>
                )}

                {/* Advanced generation controls — collapsed by default; blank = AI decides */}
                <div className="mt-5">
                    <button
                        type="button"
                        onClick={() => setShowAdvanced((v) => !v)}
                        className="flex items-center gap-1.5 text-xs text-muted hover:text-ink2 lowercase transition-colors"
                    >
                        <ChevronDown size={14} className={`transition-transform ${showAdvanced ? 'rotate-180' : ''}`} />
                        advanced options
                        {(targetClips || clipMinSeconds || clipMaxSeconds || layout !== 'auto'
                            || language !== 'auto' || transcribePrompt.trim()) && (
                            <span className="text-brass">·</span>
                        )}
                    </button>
                    {showAdvanced && (
                        /* Stacked on a phone: three number fields side by side leaves
                           ~100px each, which crushes both label and value. */
                        <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-2 animate-fade">
                            <div>
                                <p className="eyebrow mb-1.5">clips to aim for</p>
                                <input
                                    type="number" min="1" max="15" step="1"
                                    value={targetClips}
                                    onChange={(e) => setTargetClips(e.target.value)}
                                    placeholder="auto"
                                    className="input-field"
                                />
                            </div>
                            <div>
                                <p className="eyebrow mb-1.5">min length (s)</p>
                                <input
                                    type="number" min="5" max="175" step="1"
                                    value={clipMinSeconds}
                                    onChange={(e) => setClipMinSeconds(e.target.value)}
                                    placeholder="15"
                                    className="input-field"
                                />
                            </div>
                            <div>
                                <p className="eyebrow mb-1.5">max length (s)</p>
                                <input
                                    type="number" min="10" max="180" step="1"
                                    value={clipMaxSeconds}
                                    onChange={(e) => setClipMaxSeconds(e.target.value)}
                                    placeholder="60"
                                    className="input-field"
                                />
                            </div>
                            <p className="col-span-1 sm:col-span-3 text-[11px] leading-relaxed text-muted">
                                Targets, not guarantees: the AI returns fewer clips when the
                                material doesn't hold them. Leave blank to let it decide.
                            </p>
                            <div className="col-span-1 sm:col-span-3 flex flex-wrap items-center justify-between gap-3 pt-3 sm:pt-1 border-t border-rule">
                                <span className="text-xs text-ink2">vertical layout</span>
                                <select
                                    value={layout}
                                    onChange={(e) => setLayout(e.target.value)}
                                    className="input-field !w-auto text-xs py-1.5"
                                    aria-label="vertical layout"
                                >
                                    <option value="auto">Auto (AI picks per video)</option>
                                    <option value="split">Two speakers stacked</option>
                                    <option value="screencast">Screen over presenter</option>
                                    <option value="none">Single crop only</option>
                                </select>
                            </div>
                            <div className="col-span-1 sm:col-span-3 flex flex-wrap items-center justify-between gap-3 pt-3 sm:pt-1 border-t border-rule">
                                <span className="text-xs text-ink2">spoken language</span>
                                <select
                                    value={language}
                                    onChange={(e) => setLanguage(e.target.value)}
                                    className="input-field !w-auto text-xs py-1.5"
                                    aria-label="spoken language"
                                >
                                    <option value="auto">Auto-detect</option>
                                    <option value="hinglish">Hinglish (Hindi in Latin letters)</option>
                                    <option value="hi">Hindi (Devanagari)</option>
                                    <option value="ur">Urdu</option>
                                    <option value="en">English</option>
                                    <option value="es">Spanish</option>
                                    <option value="pt">Portuguese</option>
                                    <option value="fr">French</option>
                                    <option value="de">German</option>
                                    <option value="it">Italian</option>
                                    <option value="ar">Arabic</option>
                                    <option value="ru">Russian</option>
                                    <option value="ja">Japanese</option>
                                    <option value="ko">Korean</option>
                                    <option value="zh">Chinese</option>
                                </select>
                            </div>
                            <p className="col-span-1 sm:col-span-3 text-[11px] leading-relaxed text-muted">
                                Naming the language stops auto-detect from translating a Hindi
                                or Urdu video into English. Hinglish keeps the words but writes
                                them in Latin letters ("aap kaise hain"), so the caption styles
                                still work.
                            </p>
                            <div className="col-span-1 sm:col-span-3 pt-3 sm:pt-1 border-t border-rule">
                                <p className="eyebrow mb-1.5">names &amp; terms in this video</p>
                                <input
                                    type="text"
                                    value={transcribePrompt}
                                    onChange={(e) => setTranscribePrompt(e.target.value)}
                                    placeholder="Kapil Sharma, Ajay Devgn, Singham Returns"
                                    className="input-field"
                                    aria-label="names and terms in this video"
                                />
                                <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
                                    Optional. A name the model has never heard comes out spelled
                                    how it sounded; listing it here fixes that word. Keep it to a
                                    few proper nouns in Latin letters &mdash; a long list, or one
                                    written in the video's own script, gets repeated back as the
                                    transcript.
                                </p>
                            </div>
                        </div>
                    )}
                    <p className="mt-3 text-[11px] leading-relaxed text-muted">
                        Clips render as plain 9:16 without captions. Pick the output
                        format, cinematic look and captions per clip once they are done.
                    </p>
                </div>

                {duplicate && (
                    <div className="mt-5 p-3 rounded-input border border-[color:var(--color-accent)] bg-paper3 text-left">
                        <p className="flex items-center gap-2 text-[13px] sm:text-xs font-medium text-ink">
                            <AlertTriangle size={15} className="shrink-0 text-[color:var(--color-accent)]" />
                            Clips were already generated from this video
                        </p>
                        <ul className="mt-1.5 space-y-0.5 text-[11px] leading-relaxed text-muted">
                            {duplicate.matches.map((m) => (
                                <li key={m.job_id}>
                                    {m.clip_count} clip{m.clip_count === 1 ? '' : 's'}
                                    {m.created_at ? ` on ${new Date(m.created_at * 1000).toLocaleString()}` : ''}
                                    {m.match_reason === 'title' ? ' — same title' : ''}
                                    {m.match_reason === 'youtube_id' ? ' — same YouTube video' : ''}
                                    {m.match_reason && m.match_reason.startsWith('size') ? ' — same file' : ''}
                                </li>
                            ))}
                        </ul>
                        <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
                            Look for them in your clips before spending another run: this
                            one costs a full transcription plus a render per clip.
                        </p>
                        <label className="flex items-start gap-2.5 mt-2.5 text-[12px] sm:text-[11px] leading-relaxed text-ink2 cursor-pointer select-none">
                            <input
                                type="checkbox"
                                checked={duplicateAcked}
                                onChange={(e) => setDuplicateAcked(e.target.checked)}
                                className="mt-0.5 w-4 h-4 shrink-0 accent-[var(--color-accent)] cursor-pointer"
                            />
                            <span>Generate clips from it again anyway</span>
                        </label>
                    </div>
                )}

                <label className="flex items-start gap-2.5 mt-5 text-left text-[13px] sm:text-xs leading-relaxed text-muted cursor-pointer select-none">
                    <input
                        type="checkbox"
                        checked={acknowledged}
                        onChange={(e) => setAcknowledged(e.target.checked)}
                        className="mt-0.5 w-4 h-4 shrink-0 accent-[var(--color-accent)] cursor-pointer"
                    />
                    <span>
                        I confirm I own this content or have the rights to process it. I am responsible for any content I submit. See our <a href="/terms" target="_blank" rel="noopener noreferrer" className="text-ink2 underline underline-offset-2 hover:text-brass transition-colors" onClick={(e) => e.stopPropagation()}>Terms</a> and <a href="/privacy" target="_blank" rel="noopener noreferrer" className="text-ink2 underline underline-offset-2 hover:text-brass transition-colors" onClick={(e) => e.stopPropagation()}>Privacy Policy</a>.
                    </span>
                </label>

                <button
                    type="submit"
                    data-tutorial="generate"
                    disabled={isProcessing || !acknowledged || blockedByDuplicate || (mode === 'url' && !url) || (mode === 'file' && !file)}
                    className="w-full btn-primary mt-4"
                >
                    {isProcessing ? (
                        <>
                            <Loader2 size={16} className="animate-spin" />
                            Processing Video...
                        </>
                    ) : (
                        <>
                            Generate Clips
                        </>
                    )}
                </button>
            </form>
        </div>
    );
}
