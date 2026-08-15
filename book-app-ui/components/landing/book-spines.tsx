/** Abstract, book-themed hero motif -- a row of stylized book spines
 * rendered as plain inline SVG rectangles. Deliberately not a literal book
 * cover or bookstore photo (per the design brief): this is pure vector
 * markup with zero network requests, so it costs nothing to load and stays
 * crisp at any size or screen density. Each bar gently pulses in place
 * (opacity/brightness only, see the "spine-pulse" keyframe in
 * globals.css) so the shelf reads as alive rather than static.
 */
export function BookSpines({ className }: { className?: string }) {
  // Every bar is bottom-anchored to a shared baseline (only height varies)
  // so this reads as books standing upright on a shelf, not a floating bar
  // chart. Colors pull from the landing page's electric-blue / violet /
  // coral accent palette rather than the app's normal grayscale tokens,
  // since this motif only ever appears against the landing hero's dark
  // background.
  const bars = [
    { h: 130, w: 22, color: "#60a5fa", delay: "0s" },
    { h: 165, w: 18, color: "#a78bfa", delay: "0.3s" },
    { h: 100, w: 26, color: "#fb7185", delay: "0.6s" },
    { h: 180, w: 16, color: "#818cf8", delay: "0.9s" },
    { h: 115, w: 24, color: "#38bdf8", delay: "1.2s" },
    { h: 150, w: 20, color: "#fb923c", delay: "1.5s" },
    { h: 105, w: 22, color: "#a78bfa", delay: "1.8s" },
    { h: 170, w: 18, color: "#fda4af", delay: "2.1s" },
    { h: 135, w: 24, color: "#60a5fa", delay: "2.4s" },
    { h: 95, w: 20, color: "#fb923c", delay: "2.7s" },
    { h: 145, w: 20, color: "#38bdf8", delay: "3s" },
    { h: 120, w: 18, color: "#c084fc", delay: "3.3s" },
  ];
  const gap = 10;
  const width = bars.reduce((sum, bar) => sum + bar.w + gap, 0);
  const height = 190;

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className={className}
      role="presentation"
      aria-hidden="true"
      preserveAspectRatio="xMidYMax slice"
    >
      {bars.map((bar, index) => {
        const x = bars.slice(0, index).reduce((sum, b) => sum + b.w + gap, 0);
        return (
          <rect
            key={index}
            className="animate-spine-pulse motion-reduce:animate-none"
            x={x}
            y={height - bar.h}
            width={bar.w}
            height={bar.h}
            rx={3}
            fill={bar.color}
            opacity={0.85}
            style={{ animationDelay: bar.delay, transformOrigin: `${x + bar.w / 2}px ${height}px` }}
          />
        );
      })}
    </svg>
  );
}
