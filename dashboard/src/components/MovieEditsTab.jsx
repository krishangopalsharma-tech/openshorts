import { useCallback, useState } from 'react';
import { Zap } from 'lucide-react';
import SessionSetup from './film/SessionSetup';
import ShortsFlow from './film/ShortsFlow';
import { useRenderPoll } from './film/filmApi';

/**
 * Movie Edits: the action / thriller / mystery / underdog edit, on its own
 * page. Same film + SRT session and the same montage pipeline as the shorts
 * in Movie Recap & Shorts (film_montage, rendered as ordinary clip jobs),
 * but a session of kind ``edits``: its films list only here, it starts with
 * the movie-edit look (gritty grade, neon_punch captions, climax on) and its
 * prompt requires the climax tail, the beat-cut flash shots after the story.
 */
export default function MovieEditsTab({ onOpenJob }) {
  const [session, setSession] = useState(null);
  const [renders, setRenders] = useState({});

  const onSession = useCallback((s) => {
    setSession(s);
    setRenders(s?.renders || {});
  }, []);

  useRenderPoll(session?.id, renders, setRenders);

  return (
    <div className="h-full overflow-y-auto custom-scrollbar p-4 sm:p-6 md:p-10 animate-fade">
      <div className="max-w-5xl mx-auto space-y-6">
        <header>
          <p className="text-[11px] font-mono uppercase tracking-wider text-muted">action · thriller · mystery · underdog</p>
          <h1 className="text-xl font-semibold text-ink flex items-center gap-2 mt-1"><Zap size={20} className="text-brass" /> Movie Edits</h1>
          <p className="text-sm text-muted mt-1 max-w-2xl">
            Shorts cut from a film the way movie-edit channels cut them: scenes that build tension in the order
            that tells it best, the film&apos;s own lines as captions over a dark bed, then the payoff, a few
            beat-cut flash shots on the music&apos;s drop with the film muted. Planned in a chat window from the
            subtitle lines, rendered as ordinary clips you can open in the clip editor.
          </p>
        </header>

        <SessionSetup kind="edits" session={session} onSession={onSession} settings={{}} />

        {session && (
          <ShortsFlow session={session} onSession={onSession} renders={renders} setRenders={setRenders} onOpenJob={onOpenJob}
            planHint="Copy the prompt into Claude, ChatGPT or Gemini. It picks the scenes that build tension (as subtitle LINES, never times) and 3-6 climax flash shots by time; a short without a climax is sent back for correction." />
        )}
      </div>
    </div>
  );
}
