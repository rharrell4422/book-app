"use client";

import { useState } from "react";
import { ListFilterIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

function normalizeForSearch(value: unknown): string {
  return String(value ?? "").trim().toLowerCase();
}

export type ValueFilterMenuProps = {
  /** Column name shown in the popover title, e.g. "Title". */
  label: string;
  options: string[];
  selectedValues: string[];
  onApplyValues: (values: string[]) => void;
  onClear: () => void;
  searchValue: string;
  onSearchChange: (value: string) => void;
};

/**
 * Excel-style multi-select column filter. Renders as a small icon button
 * (so it doesn't dominate the column header) and opens a Radix Popover,
 * which portals its content to the document body -- this is what keeps the
 * checkbox list from being clipped by or visually overlapping table rows
 * inside a scrollable table container, unlike a hand-rolled absolutely
 * positioned <div>.
 */
export function ValueFilterMenu({
  label,
  options,
  selectedValues,
  onApplyValues,
  onClear,
  searchValue,
  onSearchChange,
}: ValueFilterMenuProps) {
  const [open, setOpen] = useState(false);
  const [draftValues, setDraftValues] = useState<string[]>(selectedValues);
  const isActive = selectedValues.length > 0;
  const normalizedSearch = normalizeForSearch(searchValue);
  const visibleOptions = options.filter((option) => normalizeForSearch(option).includes(normalizedSearch));

  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (nextOpen) {
          setDraftValues(selectedValues);
        }
      }}
    >
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant={isActive ? "secondary" : "ghost"}
          size="icon-xs"
          aria-label={`Filter ${label}${isActive ? ` (${selectedValues.length} selected)` : ""}`}
          title={`Filter ${label}`}
          className="relative"
        >
          <ListFilterIcon />
          {isActive ? (
            <span className="absolute -right-1 -top-1 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-primary px-0.5 text-[9px] font-semibold leading-none text-primary-foreground">
              {selectedValues.length}
            </span>
          ) : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent>
        <p className="mb-1.5 px-1 text-xs font-semibold text-foreground">Filter by {label}</p>
        <input
          value={searchValue}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="Search values"
          className="mb-2 h-7 w-full rounded border bg-background px-2 text-xs"
        />
        <div className="max-h-48 space-y-1 overflow-auto pr-1">
          {visibleOptions.map((option) => {
            const checked = draftValues.includes(option);
            return (
              <label key={option} className="flex items-center gap-2 rounded px-1 py-0.5 text-xs hover:bg-muted/60">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => {
                    setDraftValues((prev) =>
                      prev.includes(option) ? prev.filter((item) => item !== option) : [...prev, option]
                    );
                  }}
                />
                <span className="truncate">{option || "(blank)"}</span>
              </label>
            );
          })}
          {visibleOptions.length === 0 ? (
            <p className="px-1 text-xs text-muted-foreground">No matching values.</p>
          ) : null}
        </div>
        <div className="mt-2 flex justify-end gap-1 border-t pt-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              onClear();
              setOpen(false);
            }}
          >
            Clear
          </Button>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => {
              onApplyValues(draftValues);
              setOpen(false);
            }}
          >
            Apply
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
