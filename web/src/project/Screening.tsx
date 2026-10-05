import { useEffect, useRef, useState } from "react";
import { Play } from "../components/icons";
import { track } from "./track";

// The project's films, hosted on Cloudflare R2 (media.plexbie.com), the page's TV's two
// channels: the teaser (01) and the three-minute tour (02). Nothing loads until
// someone presses play, and there's no autoplay. A new cut gets a new name (-v2), since
// the files are cached for a year.

const MEDIA = "https://media.plexbie.com/video";

export type Film = "teaser" | "tour";
export const FILMS: Record<Film, { channel: string; name: string; file: string; length: string }> = {
  // The 4:3 cut, made for the TV's screen (plexbie-trailer's 4:3 teaser).
  teaser: { channel: "Teaser", name: "The Plexbie teaser", file: "plexbie-teaser-4x3", length: "1:13" },
  // The 4:3 cut, made for the TV's screen (plexbie-trailer's Trailer43 composition).
  tour: { channel: "Full tour", name: "The three-minute tour of Plexbie", file: "plexbie-tour-4x3", length: "3:02" },
};

/** A film on the TV's screen: its poster and a play button, then the film with controls
 *  (full screen is one of them). `start` plays it as soon as it's on. */
export function TvFilm({ film, start = false }: { film: Film; start?: boolean }) {
  const f = FILMS[film];
  const [started, setStarted] = useState(start);
  const video = useRef<HTMLVideoElement>(null);
  // How far into the tour people get: played, then each quarter, once per showing.
  const reached = useRef(new Set<number>());
  const progress = () => {
    const v = video.current;
    if (!v?.duration) return;
    const pct = (v.currentTime / v.duration) * 100;
    for (const q of [25, 50, 75]) {
      if (pct >= q && !reached.current.has(q)) { reached.current.add(q); track("video", { l: f.channel, v: q }); }
    }
  };
  useEffect(() => {
    // A browser may refuse to start sound it didn't see pressed: the play button stays.
    if (started) void video.current?.play().catch(() => setStarted(false));
  }, [started]);

  return (
    <div className="tv-film">
      <video ref={video} controls={started} playsInline preload="none" poster={`${MEDIA}/${f.file}-poster-v1.jpg`}
        aria-label={`${f.name}, ${f.length}`} onTimeUpdate={progress}
        onPlay={() => { setStarted(true); if (!reached.current.has(0)) { reached.current.add(0); track("video", { l: f.channel, v: 0 }); } }}
        onEnded={() => { if (!reached.current.has(100)) { reached.current.add(100); track("video", { l: f.channel, v: 100 }); } }}>
        <source src={`${MEDIA}/${f.file}-v1.mp4`} type="video/mp4" />
        <a href={`${MEDIA}/${f.file}-v1.mp4`}>Download {f.name} (MP4)</a>
      </video>
      {!started ? (
        <button type="button" className="tv-film__play" onClick={() => setStarted(true)} aria-label={`Play ${f.name}, ${f.length}`}>
          <Play size={28} aria-hidden />
          <span>{f.length}</span>
        </button>
      ) : null}
    </div>
  );
}
