export const SERIES_CHECK_RESET_DELAY_MS = 1500;

export function isSeriesCheckTerminalStatus(status: string | null | undefined): boolean {
  const normalized = String(status || "").trim().toLowerCase();
  return normalized === "catalog_fallback" || normalized === "complete";
}

export function scheduleSeriesCheckReset(
  status: string | null | undefined,
  reset: () => void,
  schedule: (cb: () => void, delayMs: number) => number,
  delayMs: number = SERIES_CHECK_RESET_DELAY_MS,
): number | null {
  if (!isSeriesCheckTerminalStatus(status)) {
    return null;
  }
  return schedule(reset, delayMs);
}
