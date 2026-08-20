"use client";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export type DiscoveryHealth = "never_checked" | "healthy" | "stale" | "very_stale";

const HEALTH_META: Record<DiscoveryHealth, { dotClassName: string; label: string }> = {
  never_checked: { dotClassName: "bg-muted-foreground/40", label: "Never checked" },
  healthy: { dotClassName: "bg-emerald-500", label: "Checked recently" },
  stale: { dotClassName: "bg-amber-500", label: "Getting stale" },
  very_stale: { dotClassName: "bg-rose-600", label: "Discovery may be broken" },
};

function monthsAgoLabel(lastChecked: string | null | undefined): string {
  if (!lastChecked) return "Never checked for new books.";
  const parsed = new Date(lastChecked);
  if (Number.isNaN(parsed.getTime())) return "Never checked for new books.";

  const days = Math.max(0, Math.floor((Date.now() - parsed.getTime()) / (1000 * 60 * 60 * 24)));
  const months = Math.round(days / 30.44);
  if (months <= 0) return "Last checked recently.";
  if (months === 1) return "Last checked 1 month ago.";
  return `Last checked ${months} months ago.`;
}

/**
 * Discovery Health Indicator (Auto Discovery MVP spec, §1) -- a small dot
 * beside the series name reflecting Series.last_checked. Suppressed
 * entirely for finished series by the caller (not here), since "finished"
 * is a per-series fact the badge itself has no way to know without also
 * being passed is_finished.
 */
export function DiscoveryHealthBadge({
  health,
  lastChecked,
  className,
}: {
  health: DiscoveryHealth | null | undefined;
  lastChecked?: string | null;
  className?: string;
}) {
  const resolvedHealth: DiscoveryHealth = health ?? "never_checked";
  const meta = HEALTH_META[resolvedHealth] ?? HEALTH_META.never_checked;
  const tooltipText = `${meta.label} -- ${monthsAgoLabel(lastChecked)}`;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          role="img"
          aria-label={tooltipText}
          className={cn("inline-block h-2 w-2 shrink-0 rounded-full", meta.dotClassName, className)}
        />
      </TooltipTrigger>
      <TooltipContent>{tooltipText}</TooltipContent>
    </Tooltip>
  );
}
