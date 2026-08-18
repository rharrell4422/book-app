"use client";

import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { useVisualViewportBottomInset } from "@/hooks/use-visual-viewport-bottom-inset";

export function BookFormPageChrome({
  title,
  subtitle,
  onCancel,
  onSave,
  saving,
  saveLabel,
  savingLabel,
  saveDisabled,
  children,
}: {
  title: string;
  subtitle?: string;
  onCancel: () => void;
  onSave: () => void;
  saving: boolean;
  saveLabel: string;
  savingLabel?: string;
  saveDisabled?: boolean;
  children: ReactNode;
}) {
  const keyboardInset = useVisualViewportBottomInset();
  const saveBarPadding = `max(0.75rem, env(safe-area-inset-bottom))`;
  const contentBottomPad = `calc(5.5rem + env(safe-area-inset-bottom) + ${keyboardInset}px)`;

  return (
    <div className="relative min-h-full">
      <header className="sticky top-0 z-20 border-b bg-background/95 px-4 pt-[env(safe-area-inset-top)] backdrop-blur-sm">
        <div className="flex items-center gap-3 py-2">
          <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <div className="min-w-0">
            <h1 className="text-base font-semibold leading-tight">{title}</h1>
            {subtitle ? <p className="truncate text-xs text-muted-foreground">{subtitle}</p> : null}
          </div>
        </div>
      </header>

      <div className="space-y-3 px-4 py-4" style={{ paddingBottom: contentBottomPad }}>
        {children}
      </div>

      <div
        className="fixed inset-x-0 z-30 border-t bg-background/95 px-4 pt-3 backdrop-blur-sm"
        style={{
          bottom: keyboardInset,
          paddingBottom: saveBarPadding,
        }}
      >
        <Button type="button" className="w-full" onClick={onSave} disabled={saving || saveDisabled}>
          {saving ? savingLabel || "Saving..." : saveLabel}
        </Button>
      </div>
    </div>
  );
}
