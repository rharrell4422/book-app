"use client";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";

export type TitleNormalizationWizardMode =
  | "keep_original"
  | "clean_up"
  | "new_clean_title"
  | "match_other_titles"
  | "custom";

export type CustomTitlePatternPreset = {
  id: string;
  label: string;
  pattern: string;
};

export type TitleNormalizationOption = {
  mode: TitleNormalizationWizardMode;
  label: string;
  description: string;
  note: string;
  sampleRows: Array<{ before: string; after: string }>;
};

export type TitleNormalizationPreviewRow = {
  id: number;
  currentTitle: string;
  normalizedTitle: string;
  skipped: boolean;
  skipReason: "upcoming" | "unnumbered" | null;
};

export function NormalizeTitlesDialog({
  open,
  onOpenChange,
  options,
  wizardMode,
  onWizardModeChange,
  customPresets,
  customPreset,
  onCustomPresetSelect,
  customPattern,
  onCustomPatternChange,
  excludeUpcoming,
  onExcludeUpcomingChange,
  previewRows,
  applicableCount,
  skippedUpcomingCount,
  skippedUnnumberedCount,
  onApply,
  applying,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  options: TitleNormalizationOption[];
  wizardMode: TitleNormalizationWizardMode;
  onWizardModeChange: (mode: TitleNormalizationWizardMode) => void;
  customPresets: readonly CustomTitlePatternPreset[];
  customPreset: string;
  onCustomPresetSelect: (preset: CustomTitlePatternPreset) => void;
  customPattern: string;
  onCustomPatternChange: (pattern: string) => void;
  excludeUpcoming: boolean;
  onExcludeUpcomingChange: (value: boolean) => void;
  previewRows: TitleNormalizationPreviewRow[];
  applicableCount: number;
  skippedUpcomingCount: number;
  skippedUnnumberedCount: number;
  onApply: () => void;
  applying: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-5xl">
        <DialogHeader>
          <DialogTitle>Optional Title Normalization</DialogTitle>
          <DialogDescription>
            Purely cosmetic and reversible -- this only changes how titles display in this app, not the book&apos;s
            actual published title. Pick a mode, review real examples from this series, then apply once with Accept Changes.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="grid gap-2 md:grid-cols-2">
            {options.map((option) => {
              const selected = wizardMode === option.mode;
              return (
                <button
                  key={option.mode}
                  type="button"
                  className={`rounded border p-3 text-left ${selected ? "border-emerald-500 bg-emerald-50" : "border-slate-200 bg-white hover:border-slate-300"}`}
                  onClick={() => onWizardModeChange(option.mode)}
                >
                  <div className="mb-1 flex items-center justify-between gap-2">
                    <p className="text-sm font-semibold text-foreground">{option.label}</p>
                    {selected ? <span className="text-xs font-semibold text-emerald-700">Selected</span> : null}
                  </div>
                  <p className="text-xs text-muted-foreground">{option.description}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{option.note}</p>
                  <div className="mt-2 space-y-1 rounded border bg-slate-50 p-2">
                    {option.sampleRows.length > 0 ? (
                      option.sampleRows.map((row, index) => (
                        <div key={`${option.mode}-${index}`} className="grid grid-cols-[1fr_auto_1fr] gap-1 text-xs">
                          <span className="truncate text-slate-700">{row.before}</span>
                          <span className="text-slate-400" aria-hidden="true">-&gt;</span>
                          <span className="truncate text-emerald-700">{row.after}</span>
                        </div>
                      ))
                    ) : (
                      <p className="text-xs text-muted-foreground">No sample titles available.</p>
                    )}
                  </div>
                </button>
              );
            })}
          </div>

          {wizardMode === "custom" ? (
            <div className="space-y-1 rounded border bg-slate-50 p-3">
              <Label htmlFor="normalize-custom-preset">Custom style preset</Label>
              <select
                id="normalize-custom-preset"
                value={customPreset}
                onChange={(event) => {
                  const selectedPreset = customPresets.find((preset) => preset.id === event.target.value);
                  if (!selectedPreset) return;
                  onCustomPresetSelect(selectedPreset);
                }}
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              >
                {customPresets.map((preset) => (
                  <option key={preset.id} value={preset.id}>{preset.label}</option>
                ))}
              </select>
              <Label htmlFor="normalize-custom-pattern">Custom pattern</Label>
              <input
                id="normalize-custom-pattern"
                value={customPattern}
                onChange={(event) => onCustomPatternChange(event.target.value)}
                className="h-9 w-full rounded border bg-white px-2 text-sm"
                placeholder="{book_title} ({series_name} Book {book_number})"
              />
              <p className="text-xs text-muted-foreground">
                Tokens: {"{book_title}"}, {"{book_subtitle}"}, {"{series_name}"}, {"{book_number}"}, {"{original_title}"}
              </p>
              <p className="text-xs text-muted-foreground">
                Each token is replaced with that book&apos;s value. If a token is blank (e.g. no subtitle), it&apos;s
                simply left empty and any leftover dash, colon, or empty parentheses next to it is cleaned up
                automatically.
              </p>
            </div>
          ) : null}

          <label className="flex items-start gap-2 rounded border bg-slate-50 px-3 py-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={excludeUpcoming}
              onChange={(event) => onExcludeUpcomingChange(event.target.checked)}
              className="mt-0.5"
            />
            <span>Exclude UPCOMING books with publication_date in the future.</span>
          </label>
        </div>

        {previewRows.length > 0 ? (
          <div className="max-h-[38vh] overflow-auto rounded border bg-white text-xs sm:max-h-[30rem]">
            <div className="grid grid-cols-[1fr_auto_1fr] gap-2 border-b bg-slate-50 px-3 py-2 font-semibold text-muted-foreground">
              <div>Current title</div>
              <div />
              <div>Normalized title</div>
            </div>
            {previewRows.map((row) => (
              <div key={row.id} className="grid grid-cols-[1fr_auto_1fr] items-center gap-2 border-b px-3 py-2 last:border-b-0">
                <div className="min-w-0">
                  <p className="truncate font-medium text-foreground">{row.currentTitle}</p>
                </div>
                <div className="px-1 text-sm text-muted-foreground" aria-hidden="true">
                  →
                </div>
                <div className="min-w-0">
                  {row.skipReason === "upcoming" ? (
                    <p className="truncate font-medium text-amber-700">Skipped (upcoming + future publication)</p>
                  ) : row.skipReason === "unnumbered" ? (
                    <p className="truncate font-medium text-amber-700">Skipped (no book number - protects future discovery matching)</p>
                  ) : (
                    <p className="truncate font-medium text-emerald-700">{row.normalizedTitle}</p>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">
            No title normalization changes are needed for this selection.
          </p>
        )}

        <div className="rounded border bg-slate-50 px-3 py-2 text-xs text-muted-foreground">
          Ready to apply: {applicableCount} change{applicableCount === 1 ? "" : "s"}
          {skippedUpcomingCount > 0 ? ` • Skipped upcoming: ${skippedUpcomingCount}` : ""}
          {skippedUnnumberedCount > 0 ? ` • Skipped (no book #): ${skippedUnnumberedCount}` : ""}
        </div>

        <DialogFooter showCloseButton>
          <Button
            type="button"
            variant="secondary"
            onClick={onApply}
            disabled={applying || applicableCount === 0}
          >
            {applying ? "Applying..." : `Accept Changes (${applicableCount})`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
