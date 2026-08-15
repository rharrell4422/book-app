import Link from "next/link";
import { Inter } from "next/font/google";
import {
  BellIcon,
  BookMarkedIcon,
  BookOpenIcon,
  CompassIcon,
  LayersIcon,
  LibraryIcon,
  SearchIcon,
  SparklesIcon,
  UserSearchIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { BookSpines } from "@/components/landing/book-spines";
import { Particles } from "@/components/landing/particles";

// Scoped to just this page (className applied only to the landing
// wrapper below), not the whole app -- next/font self-hosts and preloads
// at build time, so this has zero runtime request/CLS cost even though
// it's only used here.
const inter = Inter({ subsets: ["latin"], weight: ["400", "500", "600", "700"], display: "swap" });

const VALUE_PROPS: { icon: typeof LibraryIcon; text: string }[] = [
  { icon: BellIcon, text: "Never miss a new release again." },
  { icon: BookOpenIcon, text: "Know exactly where you left off." },
  { icon: SearchIcon, text: "Discover new books instantly." },
];

const FEATURES: { icon: typeof LibraryIcon; label: string; description: string }[] = [
  { icon: LayersIcon, label: "Series Intelligence", description: "Understand every series you track, at a glance." },
  { icon: SparklesIcon, label: "Automatic Discovery", description: "Find new releases the moment they're announced." },
  { icon: BellIcon, label: "Upcoming Release Tracking", description: "Always know what's coming next." },
  { icon: BookMarkedIcon, label: "Standalone Library", description: "Keep non-series books organized and clean." },
  { icon: UserSearchIcon, label: "Author-wide Exploration", description: "See everything an author has written." },
  { icon: CompassIcon, label: "Series Maturity Indicators", description: "Completed vs. ongoing, book counts, next release." },
  { icon: LibraryIcon, label: "Optional Series Overview", description: "Quick summaries of what a series is about." },
];

// Matches Tailwind's default delay scale (see tw-animate-css/globals.css)
// -- indexed into for a staggered entrance instead of every tile
// appearing at once.
const STAGGER_DELAYS = ["delay-0", "delay-75", "delay-150", "delay-200", "delay-300", "delay-500", "delay-700"];

export function LandingPage() {
  return (
    <div
      className={`${inter.className} relative w-full overflow-hidden bg-gradient-to-b from-[#070b1f] via-[#0f5470] to-[#070b1f] text-white`}
    >
      {/* Ambient background glow -- slow transform-only drift (see the
          "drift" keyframe in globals.css), never scroll-linked. This page
          has almost no scroll distance, so real parallax would add JS
          scroll-listener complexity for an effect nobody would see; this
          gets a similar "alive" quality for free. Re-tinted to the new
          blue/cyan/teal palette (previously blue/violet/rose). */}
      <div
        className="pointer-events-none absolute -left-32 -top-32 h-96 w-96 rounded-full bg-blue-600/30 blur-3xl animate-drift motion-reduce:animate-none"
        aria-hidden
      />
      <div
        className="pointer-events-none absolute -right-32 top-1/3 h-96 w-96 rounded-full bg-cyan-500/25 blur-3xl animate-drift motion-reduce:animate-none"
        style={{ animationDelay: "4s" }}
        aria-hidden
      />
      <div
        className="pointer-events-none absolute bottom-0 left-1/3 h-72 w-72 rounded-full bg-teal-400/20 blur-3xl animate-drift motion-reduce:animate-none"
        style={{ animationDelay: "8s" }}
        aria-hidden
      />

      {/* Radial glow behind the hero text specifically -- distinct from the
          three ambient corner blobs above, which stay lower-key depth
          accents. Kept over the darker upper part of the gradient (not the
          brighter teal band lower down) so it never competes with text
          contrast. */}
      <div
        className="pointer-events-none absolute left-1/2 top-24 h-[420px] w-[720px] max-w-[90vw] -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,rgba(56,189,248,0.3),transparent_70%)] blur-2xl"
        aria-hidden
      />

      <Particles />

      {/* Section spacing is intentionally tight throughout this wrapper --
          the curved wave centerpiece plus a full value-prop row and 7-item
          feature grid is a lot of vertical content, and the goal is fitting
          all of it on one screen without cutting anything (see the "Fitting
          everything on one screen" section of the redesign plan). */}
      <div className="relative mx-auto flex max-w-5xl flex-col gap-8 px-4 py-8 sm:gap-10 sm:py-10">
        {/* Hero */}
        <section className="flex flex-col items-center gap-2 text-center">
          <h1 className="animate-in fade-in slide-in-from-bottom-4 duration-700 text-4xl font-semibold tracking-tight sm:text-5xl">
            ReaderPro
          </h1>
          <p className="animate-in fade-in slide-in-from-bottom-4 delay-150 duration-700 text-base font-medium text-blue-100 sm:text-lg">
            Your Personal Book Intelligence Engine
          </p>
          <p className="animate-in fade-in slide-in-from-bottom-4 delay-300 max-w-md text-xs text-slate-300 duration-700 sm:text-sm">
            Track series. Discover new releases. Stay ahead.
          </p>
          <BookSpines className="animate-in fade-in delay-500 mt-4 h-32 w-full max-w-md duration-1000 sm:h-44" />
        </section>

        {/* Value proposition -- a single compact row of icon+text chips
            rather than three full padded cards, so all three messages stay
            without costing a whole extra "section" worth of height. */}
        <section className="flex flex-wrap items-center justify-center gap-3">
          {VALUE_PROPS.map(({ icon: Icon, text }, index) => (
            <div
              key={text}
              className={`animate-in fade-in slide-in-from-bottom-4 ${STAGGER_DELAYS[index]} group flex items-center gap-2 rounded-full border border-white/10 bg-black/30 px-4 py-2 text-xs font-medium backdrop-blur-sm duration-700 transition-[transform,background-color,border-color,box-shadow] hover:-translate-y-0.5 hover:border-white/20 hover:bg-white/10 hover:shadow-[0_0_20px_-5px_rgba(34,211,238,0.4)] sm:text-sm`}
            >
              <Icon className="size-4 shrink-0 text-cyan-300 transition-transform group-hover:scale-110" />
              {text}
            </div>
          ))}
        </section>

        {/* Feature highlights -- 4 columns from `sm` up (rather than
            starting at `lg`) so the 7 cards resolve to 2 rows instead of 3,
            with tighter padding/text than a standalone feature showcase
            would use. */}
        <section className="flex flex-col gap-3">
          <h2 className="text-center text-base font-semibold">What ReaderPro Does</h2>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {FEATURES.map(({ icon: Icon, label, description }, index) => (
              <div
                key={label}
                className={`animate-in fade-in slide-in-from-bottom-4 ${STAGGER_DELAYS[index % STAGGER_DELAYS.length]} group flex flex-col items-center gap-1.5 rounded-xl border border-white/10 bg-black/30 px-3 py-3 text-center backdrop-blur-sm duration-700 transition-[transform,background-color,border-color,box-shadow] hover:-translate-y-1 hover:border-white/20 hover:bg-white/10 hover:shadow-[0_0_30px_-5px_rgba(96,165,250,0.4)]`}
              >
                <span className="flex size-9 items-center justify-center rounded-full bg-white/10 ring-1 ring-white/15 transition-colors group-hover:bg-white/15">
                  <Icon className="size-4 text-blue-300 transition-transform group-hover:scale-110" />
                </span>
                <p className="text-xs font-semibold text-slate-100 sm:text-sm">{label}</p>
                <p className="text-[11px] leading-snug text-slate-300">{description}</p>
              </div>
            ))}
          </div>
        </section>

        {/* Call to action -- a single button: with the page now shown on
            every visit, a second "skip" button that went to the exact
            same place added nothing but redundant clutter. White pill
            (rather than the previous colorful gradient) to match the
            concept image, with a colored glow on hover instead of a
            gradient shift. */}
        <section className="flex flex-col items-center">
          <Link href="/books">
            <Button
              type="button"
              size="lg"
              className="h-11 rounded-full border-0 bg-white px-10 text-base font-semibold text-slate-900 shadow-[0_0_25px_-5px_rgba(255,255,255,0.5)] transition-all hover:-translate-y-0.5 hover:bg-white hover:shadow-[0_0_45px_-5px_rgba(34,211,238,0.9)]"
            >
              Enter Your Library
            </Button>
          </Link>
        </section>
      </div>
    </div>
  );
}
