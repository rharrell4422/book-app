/** Glowing, book-themed hero centerpiece -- a row of stylized book spines
 * with a bright wave of light flowing behind/through them, plus a faint
 * reflection below. Deliberately not a literal book cover or bookstore
 * photo (per the design brief): this is pure inline SVG/CSS, so it costs
 * nothing to load, stays crisp at any size or screen density, and every
 * piece (wave, bars, reflection) can be independently animated.
 */
export function BookSpines({ className }: { className?: string }) {
  // Every bar is bottom-anchored to a shared baseline (only height varies)
  // so this reads as books standing upright on a shelf, not a floating bar
  // chart. Mostly blue/cyan, with a cluster of warm/violet accent bars
  // breaking the pattern in the middle -- matching the concept image's
  // color break -- rather than the app's normal grayscale tokens, since
  // this motif only ever appears against the landing hero's dark
  // background.
  const bars = [
    { h: 100, w: 20, color: "#38bdf8", delay: "0s" },
    { h: 140, w: 18, color: "#60a5fa", delay: "0.25s" },
    { h: 90, w: 22, color: "#22d3ee", delay: "0.5s" },
    { h: 160, w: 16, color: "#3b82f6", delay: "0.75s" },
    { h: 112, w: 20, color: "#38bdf8", delay: "1s" },
    { h: 150, w: 18, color: "#fb7185", delay: "1.25s" },
    { h: 132, w: 20, color: "#fb923c", delay: "1.5s" },
    { h: 168, w: 16, color: "#a78bfa", delay: "1.75s" },
    { h: 120, w: 20, color: "#22d3ee", delay: "2s" },
    { h: 96, w: 22, color: "#38bdf8", delay: "2.25s" },
    { h: 145, w: 18, color: "#60a5fa", delay: "2.5s" },
    { h: 106, w: 20, color: "#22d3ee", delay: "2.75s" },
  ];
  const gap = 10;
  const width = bars.reduce((sum, bar) => sum + bar.w + gap, 0);
  // Bars + wave live in the top `barsHeight`; the remaining strip below the
  // shared baseline holds the compressed, faded reflection.
  const barsHeight = 190;
  const reflectionHeight = 70;
  const totalHeight = barsHeight + reflectionHeight;

  // The glowing wave is a plain polyline sampled from a couple of
  // overlapping sine cycles rather than hand-authored bezier curves --
  // once blurred (see the "waveGlow" filter below) the facets disappear
  // and it reads as a smooth flowing line, the same trick used for most
  // CSS/SVG audio-visualizer-style glows.
  const waveBaseline = 122;
  const waveAmplitude = 20;
  const sampleCount = 48;
  const wavePath = Array.from({ length: sampleCount + 1 }, (_, i) => {
    const t = i / sampleCount;
    const x = width * t;
    const y = waveBaseline + Math.sin(t * Math.PI * 2.5 + 0.4) * waveAmplitude;
    return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");

  return (
    <div className={`relative ${className ?? ""}`}>
      {/* Soft teal/cyan glow concentrated right behind the wave -- kept
          local to this component (rather than a page-wide bright band) so
          the brighter "soft teal in the center" effect from the brief
          doesn't reduce text contrast anywhere else on the page. */}
      <div
        className="pointer-events-none absolute left-1/2 top-1/3 h-2/3 w-4/5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[radial-gradient(closest-side,rgba(34,211,238,0.35),transparent_70%)] blur-2xl"
        aria-hidden
      />
      <svg
        viewBox={`0 0 ${width} ${totalHeight}`}
        className="relative z-10 h-full w-full"
        role="presentation"
        aria-hidden="true"
        preserveAspectRatio="xMidYMax slice"
      >
        <defs>
          <linearGradient id="bookSpinesWaveGradient" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#38bdf8" />
            <stop offset="50%" stopColor="#22d3ee" />
            <stop offset="100%" stopColor="#818cf8" />
          </linearGradient>
          {/* feMerge stacks a blurred copy behind the crisp original, giving
              a glow halo without softening the line itself into mush. */}
          <filter id="bookSpinesWaveGlow" x="-20%" y="-200%" width="140%" height="500%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        <path
          d={wavePath}
          fill="none"
          stroke="url(#bookSpinesWaveGradient)"
          strokeWidth={2.5}
          strokeLinecap="round"
          filter="url(#bookSpinesWaveGlow)"
          opacity={0.85}
          className="animate-spine-pulse motion-reduce:animate-none"
        />

        {bars.map((bar, index) => {
          const x = bars.slice(0, index).reduce((sum, b) => sum + b.w + gap, 0);
          return (
            <g
              key={index}
              className="animate-wave-bob motion-reduce:animate-none"
              style={{ animationDelay: bar.delay }}
            >
              <rect
                className="animate-spine-pulse motion-reduce:animate-none"
                x={x}
                y={barsHeight - bar.h}
                width={bar.w}
                height={bar.h}
                rx={3}
                fill={bar.color}
                opacity={0.85}
                style={{ animationDelay: bar.delay, transformOrigin: `${x + bar.w / 2}px ${barsHeight}px` }}
              />
              {/* Reflection: a short, dimmed echo of the same bar just below
                  the baseline -- flat opacity rather than a per-bar
                  fade-to-transparent gradient, which would need one
                  <linearGradient> per bar color to look right. */}
              <rect
                x={x}
                y={barsHeight}
                width={bar.w}
                height={bar.h * 0.35}
                rx={3}
                fill={bar.color}
                opacity={0.16}
              />
            </g>
          );
        })}
      </svg>
    </div>
  );
}
