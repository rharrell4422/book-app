import Link from "next/link";
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
import { Card, CardContent } from "@/components/ui/card";
import { BookSpines } from "@/components/landing/book-spines";

const VALUE_PROPS = [
  "Never miss a new release again.",
  "Know exactly where you left off.",
  "Discover new books instantly.",
];

const FEATURES: { icon: typeof LibraryIcon; title: string; description: string }[] = [
  {
    icon: LayersIcon,
    title: "Series Intelligence",
    description: "Understand every series you track, at a glance.",
  },
  {
    icon: SparklesIcon,
    title: "Automatic Discovery",
    description: "Find new releases the moment they're announced.",
  },
  {
    icon: BellIcon,
    title: "Upcoming Release Tracking",
    description: "Always know what's coming next.",
  },
  {
    icon: BookMarkedIcon,
    title: "Standalone Library",
    description: "Track non-series books cleanly, on their own.",
  },
  {
    icon: UserSearchIcon,
    title: "Author-wide Exploration",
    description: "See everything an author has written, all in one place.",
  },
  {
    icon: CompassIcon,
    title: "Series Maturity Indicators",
    description: "Completed vs. ongoing, book counts, and next release.",
  },
  {
    icon: LibraryIcon,
    title: "Optional Series Overview",
    description: "Quick, on-demand summaries of what a series is about.",
  },
];

const STEPS = ["Add your books", "Track your series", "Discover new releases"];

export function LandingPage() {
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-16 px-4 py-12 sm:py-16">
      {/* Hero */}
      <section className="relative flex flex-col items-center overflow-hidden rounded-2xl bg-gradient-to-br from-muted/60 via-background to-muted/30 px-6 pt-16 pb-10 text-center ring-1 ring-foreground/10 sm:pt-24 sm:pb-14">
        <div className="relative flex flex-col items-center gap-4">
          <h1 className="text-4xl font-semibold tracking-tight sm:text-5xl">ReaderPro</h1>
          <p className="text-lg font-medium text-foreground/80 sm:text-xl">
            Your Personal Book Intelligence Engine
          </p>
          <p className="max-w-md text-sm text-muted-foreground sm:text-base">
            Track series. Discover new releases. Stay ahead.
          </p>
        </div>
        <BookSpines className="pointer-events-none mt-10 h-24 w-full max-w-md opacity-90 sm:h-32" />
      </section>

      {/* Value proposition */}
      <section className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        {VALUE_PROPS.map((text) => (
          <div key={text} className="rounded-xl bg-muted/50 px-5 py-4 text-center text-sm font-medium">
            {text}
          </div>
        ))}
      </section>

      {/* Feature highlights */}
      <section className="flex flex-col gap-5">
        <h2 className="text-center text-xl font-semibold">What ReaderPro does</h2>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map(({ icon: Icon, title, description }) => (
            <Card key={title}>
              <CardContent className="flex items-start gap-3">
                <Icon className="mt-0.5 size-5 shrink-0 text-foreground/70" />
                <div className="flex flex-col gap-0.5">
                  <p className="text-sm font-medium">{title}</p>
                  <p className="text-xs text-muted-foreground">{description}</p>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      </section>

      {/* Onboarding */}
      <section className="flex flex-col items-center gap-5">
        <h2 className="text-center text-xl font-semibold">How it works</h2>
        <div className="flex flex-col items-center gap-3 sm:flex-row sm:gap-4">
          {STEPS.map((step, index) => (
            <div key={step} className="flex items-center gap-3 sm:gap-4">
              <div className="flex items-center gap-2 rounded-full bg-muted/60 px-4 py-2 text-sm font-medium">
                <span className="flex size-5 items-center justify-center rounded-full bg-primary text-xs text-primary-foreground">
                  {index + 1}
                </span>
                {step}
              </div>
              {index < STEPS.length - 1 ? (
                <span className="hidden text-muted-foreground sm:inline" aria-hidden>
                  &rarr;
                </span>
              ) : null}
            </div>
          ))}
        </div>
      </section>

      {/* Call to action */}
      <section className="flex flex-col items-center gap-3 pb-4">
        <Link href="/books">
          <Button type="button" size="lg" className="h-11 px-8 text-base">
            Enter Your Library
          </Button>
        </Link>
        <Link href="/books">
          <Button type="button" variant="ghost" size="sm">
            Skip to Library
          </Button>
        </Link>
      </section>
    </div>
  );
}
