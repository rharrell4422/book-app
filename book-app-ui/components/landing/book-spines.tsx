/** Abstract, book-themed hero motif -- a row of stylized book spines
 * rendered as plain inline SVG rectangles. Deliberately not a literal book
 * cover or bookstore photo (per the design brief): this is pure vector
 * markup with zero network requests, so it costs nothing to load and stays
 * crisp at any size or screen density.
 */
export function BookSpines({ className }: { className?: string }) {
  // Every bar is bottom-anchored to a shared baseline (only height varies)
  // so this reads as books standing upright on a shelf, not a floating bar
  // chart -- the one detail that actually sells the "book spine" motif.
  const bars = [
    { h: 130, w: 22, hue: "oklch(0.55 0.16 265)" },
    { h: 165, w: 18, hue: "oklch(0.62 0.14 250)" },
    { h: 100, w: 26, hue: "oklch(0.58 0.18 30)" },
    { h: 180, w: 16, hue: "oklch(0.52 0.15 280)" },
    { h: 115, w: 24, hue: "oklch(0.65 0.12 240)" },
    { h: 150, w: 20, hue: "oklch(0.57 0.17 40)" },
    { h: 105, w: 22, hue: "oklch(0.6 0.15 260)" },
    { h: 170, w: 18, hue: "oklch(0.55 0.17 25)" },
    { h: 135, w: 24, hue: "oklch(0.63 0.13 255)" },
    { h: 95, w: 20, hue: "oklch(0.58 0.16 45)" },
    { h: 145, w: 20, hue: "oklch(0.6 0.14 210)" },
    { h: 120, w: 18, hue: "oklch(0.56 0.16 300)" },
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
            x={x}
            y={height - bar.h}
            width={bar.w}
            height={bar.h}
            rx={3}
            fill={bar.hue}
            opacity={0.75}
          />
        );
      })}
    </svg>
  );
}
