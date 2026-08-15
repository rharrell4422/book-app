/** Glowing, book-themed hero centerpiece -- a bold curved wave of light with
 * a soft white glow beneath it, and alternating-color book-spine bars
 * layered on top. Deliberately not a literal book cover or bookstore photo
 * (per the design brief): this is pure inline SVG, so it costs nothing to
 * load, stays crisp at any size or screen density, and every piece (glow,
 * wave, bars) can be independently animated.
 *
 * Everything lives inside one <svg> (rather than layering a separate HTML
 * glow <div> underneath, clipped via CSS clip-path, as originally planned):
 * this SVG uses preserveAspectRatio="xMidYMax slice" to fill its container,
 * which at the aspect ratios this component actually renders at covers by
 * width and crops the height from the top -- a plain percentage-based CSS
 * clip-path on a sibling HTML element has no way to know how much of the
 * viewBox got cropped, so it would drift out of alignment with the
 * wave/bars at different viewport widths. Defining the glow's clipPath
 * natively inside the same SVG sidesteps that entirely: it scales and
 * crops in lockstep with everything else, no coordinate-mapping to keep in
 * sync.
 */
export function BookSpines({ className }: { className?: string }) {
  // Alternates through all four accent colors in sequence (rather than
  // clustering the warm/violet accents in the middle, as before) per the
  // brief's explicit "alternating colors" -- blue, cyan, orange, purple,
  // repeating.
  const COLORS = ["#60a5fa", "#22d3ee", "#fb923c", "#a78bfa"];
  const heights = [100, 140, 90, 160, 112, 150, 132, 168, 120, 96, 145, 106];
  const widths = [20, 18, 22, 16, 20, 18, 20, 16, 20, 22, 18, 20];
  const bars = heights.map((h, i) => ({
    h,
    w: widths[i],
    color: COLORS[i % COLORS.length],
    delay: `${(i * 0.25).toFixed(2)}s`,
  }));
  const gap = 10;
  const width = bars.reduce((sum, bar) => sum + bar.w + gap, 0);
  const barsHeight = 190;

  // The glowing wave is a plain polyline sampled from a couple of
  // overlapping sine cycles rather than hand-authored bezier curves --
  // once blurred (see the filters below) the facets disappear and it reads
  // as a smooth flowing line, the same trick used for most CSS/SVG
  // audio-visualizer-style glows.
  const waveBaseline = 122;
  const waveAmplitude = 20;
  const sampleCount = 48;
  const wavePoints = Array.from({ length: sampleCount + 1 }, (_, i) => {
    const t = i / sampleCount;
    const x = width * t;
    const y = waveBaseline + Math.sin(t * Math.PI * 2.5 + 0.4) * waveAmplitude;
    return [x, y] as const;
  });
  const crestPathD = wavePoints
    .map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`)
    .join(" ");
  // Same points, closed down to the bottom corners -- defines the fillable
  // "area under the curve" the glow lives in.
  const glowClipD = `${crestPathD} L${width.toFixed(1)},${barsHeight} L0,${barsHeight} Z`;

  return (
    <div className={`relative ${className ?? ""}`}>
      <svg
        viewBox={`0 0 ${width} ${barsHeight}`}
        className="relative h-full w-full"
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
          <radialGradient id="bookSpinesGlowGradient" cx="50%" cy="30%" r="70%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="1" />
            <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
          </radialGradient>
          {/* feMerge stacks a blurred copy behind the crisp original, giving
              the crest line a glow halo without softening the line itself
              into mush. */}
          <filter id="bookSpinesWaveGlow" x="-20%" y="-200%" width="140%" height="500%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <filter id="bookSpinesGlowBlur" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="9" />
          </filter>
          <clipPath id="bookSpinesGlowClip">
            <path d={glowClipD} />
          </clipPath>
        </defs>

        {/* Soft white glow, clipped to the same curve as the crest line
            below so it visibly "follows" the wave shape. Clip is applied on
            the inner group (a hard-edged wave silhouette cut from the
            radial gradient), then blurred as a whole on the outer group --
            that order (clip, then blur) is what makes the blur bleed the
            clipped edge outward into a soft boundary, rather than blurring
            first and having the clip slice a hard line through it. */}
        <g filter="url(#bookSpinesGlowBlur)">
          <g clipPath="url(#bookSpinesGlowClip)">
            <rect x={0} y={0} width={width} height={barsHeight} fill="url(#bookSpinesGlowGradient)" opacity={0.3} />
          </g>
        </g>

        <path
          d={crestPathD}
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
                opacity={0.9}
                style={{
                  animationDelay: bar.delay,
                  transformOrigin: `${x + bar.w / 2}px ${barsHeight}px`,
                  // Static outer glow (kept off the animated "filter"
                  // property -- see the spine-pulse comment in globals.css
                  // for why the two can't share it). "99" alpha suffix is
                  // exactly 60% opacity (0x99 / 0xff).
                  filter: `drop-shadow(0 0 6px ${bar.color}99)`,
                }}
              />
            </g>
          );
        })}
      </svg>
    </div>
  );
}
