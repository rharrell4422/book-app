/** Small ambient floating-dot effect for the landing hero. Positions are
 * hardcoded (not Math.random()) so this stays a plain server component --
 * random values generated at render time would differ between the server
 * render and client hydration and trigger a hydration mismatch. Capped at
 * 12 elements, animated via transform/opacity only (see globals.css
 * "float" keyframe), which keeps this compositor-only and cheap even on
 * low-end devices.
 */
const DOTS = [
  { left: "8%", top: "20%", size: 3, duration: "6s", delay: "0s", color: "bg-blue-300" },
  { left: "18%", top: "65%", size: 2, duration: "8s", delay: "1.2s", color: "bg-cyan-300" },
  { left: "27%", top: "35%", size: 4, duration: "7s", delay: "0.5s", color: "bg-teal-300" },
  { left: "38%", top: "15%", size: 2, duration: "9s", delay: "2s", color: "bg-blue-200" },
  { left: "47%", top: "70%", size: 3, duration: "6.5s", delay: "0.8s", color: "bg-cyan-200" },
  { left: "58%", top: "28%", size: 2, duration: "7.5s", delay: "1.6s", color: "bg-teal-200" },
  { left: "67%", top: "55%", size: 4, duration: "8.5s", delay: "0.3s", color: "bg-blue-300" },
  { left: "74%", top: "18%", size: 2, duration: "6s", delay: "2.4s", color: "bg-cyan-300" },
  { left: "83%", top: "62%", size: 3, duration: "7s", delay: "1s", color: "bg-teal-300" },
  { left: "91%", top: "30%", size: 2, duration: "9s", delay: "0.2s", color: "bg-blue-200" },
  { left: "14%", top: "45%", size: 2, duration: "8s", delay: "1.8s", color: "bg-teal-200" },
  { left: "62%", top: "80%", size: 3, duration: "6.5s", delay: "2.2s", color: "bg-cyan-200" },
] as const;

export function Particles() {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden" role="presentation" aria-hidden="true">
      {DOTS.map((dot, index) => (
        <span
          key={index}
          className={`absolute rounded-full opacity-60 motion-reduce:hidden animate-float ${dot.color}`}
          style={{
            left: dot.left,
            top: dot.top,
            width: dot.size,
            height: dot.size,
            animationDuration: dot.duration,
            animationDelay: dot.delay,
          }}
        />
      ))}
    </div>
  );
}
