"use client";

import { useEffect, useRef } from "react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/use-toast";
import { useDeviceClass } from "@/hooks/use-device-class";
import { getBookActionVisual, type BookActionState } from "@/lib/book-action-icon-map";
import {
  DESTRUCTIVE_ACTION_DELAY_MS,
  getDefaultBookActionIconSize,
  resolvePhoneOrTabletTapPlan,
  type BookActionIconSize,
} from "@/lib/book-action-interaction";
import { createTapActionTimer, type TapActionTimer } from "@/lib/tap-action-timer";
import { cn } from "@/lib/utils";

export type BookActionIconProps = {
  state: BookActionState;
  onClick: () => void;
  disabled?: boolean;
  size?: BookActionIconSize;
  className?: string;
};

/**
 * Single icon-button action used across the books list, standalone books
 * list, and series detail view (desktop tables + phone/tablet card lists).
 *
 * - Desktop: Radix tooltip on hover/focus, onClick fires immediately.
 * - Phone/tablet: no tooltip. Navigation actions (view series/summary, check
 *   online, more by author, edit) fire immediately with no toast. The
 *   destructive/ambiguous actions (mark read/unread, delete) show a toast
 *   with the action's label and wait ~500ms before firing, so a stray tap
 *   is recoverable -- retapping during that window cancels and restarts it.
 *
 * Icon/label/aria-label/tooltip text and color styling are fully owned
 * internally based on `state` -- callers never pass icon/label directly.
 */
export function BookActionIcon({ state, onClick, disabled = false, size, className }: BookActionIconProps) {
  const deviceClass = useDeviceClass();
  const { toast, dismiss } = useToast();
  const visual = getBookActionVisual(state);
  const Icon = visual.icon;
  const resolvedSize = size ?? getDefaultBookActionIconSize(deviceClass);

  const pendingToastIdRef = useRef<string | null>(null);

  // onClick is almost always a fresh closure every render (e.g. `() =>
  // deleteBook(b.id)`) -- reading it through a ref (updated in an effect,
  // never during render) instead of recreating the timer on every render
  // means a parent re-render (e.g. another tab's book-status-sync update
  // touching a sibling row) can't cancel a delay that's already pending on
  // this row.
  const onClickRef = useRef(onClick);
  useEffect(() => {
    onClickRef.current = onClick;
  }, [onClick]);

  // Created inside an effect (mount only), never during render, per the
  // react-hooks/refs rule -- refs may only be read/written outside render.
  const timerRef = useRef<TapActionTimer | null>(null);
  useEffect(() => {
    const timer = createTapActionTimer({
      delayMs: DESTRUCTIVE_ACTION_DELAY_MS,
      onFire: () => {
        pendingToastIdRef.current = null;
        onClickRef.current();
      },
    });
    timerRef.current = timer;
    return () => {
      timer.cancel();
      timerRef.current = null;
    };
  }, []);

  function handleClick() {
    if (disabled) return;

    if (deviceClass === "desktop") {
      onClick();
      return;
    }

    const plan = resolvePhoneOrTabletTapPlan({ delayed: visual.delayed, disabled });
    if (plan.kind === "disabled") return;
    if (plan.kind === "immediate") {
      onClick();
      return;
    }

    // Re-tapping while a fire is already pending cancels + restarts rather
    // than stacking a second pending action or firing twice.
    if (pendingToastIdRef.current) {
      dismiss(pendingToastIdRef.current);
    }
    pendingToastIdRef.current = toast({ title: visual.label });
    timerRef.current?.trigger();
  }

  const button = (
    <Button
      type="button"
      variant={visual.buttonVariant}
      size={resolvedSize}
      aria-label={visual.ariaLabel}
      aria-disabled={disabled || undefined}
      className={cn(visual.extraClassName, disabled && "opacity-50", className)}
      onClick={handleClick}
    >
      <Icon />
    </Button>
  );

  if (deviceClass !== "desktop") {
    return button;
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>{button}</TooltipTrigger>
      <TooltipContent>{visual.tooltipLabel}</TooltipContent>
    </Tooltip>
  );
}
