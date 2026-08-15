import Link from "next/link";
import { Inter } from "next/font/google";
import {
  BellIcon,
  BookMarkedIcon,
  CompassIcon,
  LayersIcon,
  LibraryIcon,
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

const VALUE_PROPS = ["Never miss a new release again.", "Know exactly where you left off.", "Discover new books instantly."];

const FEATURES: { icon: typeof LibraryIcon; label: string }[] = [
  { icon: LayersIcon, label: "Series Intelligence" },
  { icon: SparklesIcon, label: "Automatic Discovery" },
  { icon: BellIcon, label: "Upcoming Releases" },
  { icon: BookMarkedIcon, label: "Standalone Library" },
  { icon: UserSearchIcon, label: "Author-wide Exploration" },
  { icon: CompassIcon, label: "Series Maturity Indicators" },
  { icon: LibraryIcon, label: "Optional Series Overview" },
];

// Matches Tailwind's default delay scale (see tw-animate-css/globals.css)
// -- indexed into for a staggered entrance instead of every tile
// appearing at once.
const STAGGER_DELAYS = ["delay-0", "delay-75", "delay-150", "delay-200", "delay-300", "delay-500", "delay-700"];

export function LandingPage() {
  return (
    <div
      className={`${inter.className} relative w-full overflow-hidden bg-gradient-to-br from-[#05061a] via-[#150a2e] to-[#1d0f3d] text-white`}
    >
      {/* Ambient background glow -- slow transform-only drift (see the
          "drift" keyframe in globals.css), never scroll-linked. This page
          has almost no scroll distance, so real parallax would add JS
          scroll-listener complexity for an effect nobody would see; this
          gets a similar "alive" quality for free. */}
      <div
        className="pointer-events-none absolute -left-32 -top-32 h-96 w-96 rounded-full bg-blue-600/30 blur-3xl animate-drift motion-reduce:animate-none"
        aria-hidden
      />
      <div
        className="pointer-events-none absolute -right-32 top-1/3 h-96 w-96 rounded-full bg-violet-600/30 blur-3xl animate-drift motion-reduce:animate-none"
        style={{ animationDelay: "4s" }}
        aria-hidden
      />
      <div
        className="pointer-events-none absolute bottom-0 left-1/3 h-72 w-72 rounded-full bg-rose-500/20 blur-3xl animate-drift motion-reduce:animate-none"
        style={{ animationDelay: "8s" }}
        aria-hidden
      />

      <Particles />

      <div className="relative mx-auto flex max-w-5xl flex-col gap-16 px-4 py-16 sm:py-24">
        {/* Hero */}
        <section className="flex flex-col items-center gap-4 text-center">
          <h1 className="animate-in fade-in slide-in-from-bottom-4 duration-700 text-5xl font-semibold tracking-tight sm:text-6xl">
            ReaderPro
          </h1>
          <p className="animate-in fade-in slide-in-from-bottom-4 delay-150 duration-700 text-lg font-medium text-blue-100 sm:text-xl">
            Your Personal Book Intelligence Engine
          </p>
          <p className="animate-in fade-in slide-in-from-bottom-4 delay-300 max-w-md text-sm text-slate-300 duration-700 sm:text-base">
            Track series. Discover new releases. Stay ahead.
          </p>
          <BookSpines className="animate-in fade-in delay-500 mt-8 h-24 w-full max-w-md duration-1000 sm:h-32" />
        </section>

        {/* Value proposition */}
        <section className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {VALUE_PROPS.map((text, index) => (
            <div
              key={text}
              className={`animate-in fade-in slide-in-from-bottom-4 ${STAGGER_DELAYS[index]} group rounded-xl border border-white/10 bg-white/5 px-5 py-5 text-center text-sm font-medium backdrop-blur-sm duration-700 transition-[transform,background-color,border-color,box-shadow] hover:-translate-y-1 hover:border-white/20 hover:bg-white/10 hover:shadow-[0_0_30px_-5px_rgba(139,92,246,0.4)]`}
            >
              {text}
            </div>
          ))}
        </section>

        {/* Feature highlights */}
        <section className="flex flex-col gap-6">
          <h2 className="text-center text-xl font-semibold">What ReaderPro Does</h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {FEATURES.map(({ icon: Icon, label }, index) => (
              <div
                key={label}
                className={`animate-in fade-in slide-in-from-bottom-4 ${STAGGER_DELAYS[index % STAGGER_DELAYS.length]} group flex flex-col items-center gap-2 rounded-xl border border-white/10 bg-white/5 px-3 py-5 text-center backdrop-blur-sm duration-700 transition-[transform,background-color,border-color,box-shadow] hover:-translate-y-1 hover:border-white/20 hover:bg-white/10 hover:shadow-[0_0_30px_-5px_rgba(96,165,250,0.4)]`}
              >
                <Icon className="size-5 text-blue-300 transition-transform group-hover:scale-110" />
                <p className="text-xs font-medium text-slate-100">{label}</p>
              </div>
            ))}
          </div>
        </section>

        {/* Call to action -- a single button: with the page now shown on
            every visit, a second "skip" button that went to the exact
            same place added nothing but redundant clutter. */}
        <section className="flex flex-col items-center pb-4">
          <Link href="/books">
            <Button
              type="button"
              size="lg"
              className="h-12 rounded-full border-0 bg-gradient-to-r from-blue-500 via-violet-500 to-rose-400 px-10 text-base font-semibold text-white shadow-[0_0_25px_-5px_rgba(139,92,246,0.6)] transition-all hover:-translate-y-0.5 hover:shadow-[0_0_40px_-5px_rgba(139,92,246,0.9)] hover:brightness-110"
            >
              Enter Your Library
            </Button>
          </Link>
        </section>
      </div>
    </div>
  );
}
