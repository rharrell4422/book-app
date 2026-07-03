import { describe, expect, it, vi } from "vitest";

import { isSeriesCheckTerminalStatus, scheduleSeriesCheckReset } from "./series-check-progress";

describe("series check progress reset", () => {
  it("detects terminal statuses", () => {
    expect(isSeriesCheckTerminalStatus("catalog_fallback")).toBe(true);
    expect(isSeriesCheckTerminalStatus("complete")).toBe(true);
    expect(isSeriesCheckTerminalStatus("running")).toBe(false);
  });

  it("resets progress and button state for catalog_fallback within 2 seconds", () => {
    vi.useFakeTimers();

    const uiState = {
      progress: 100,
      status: "catalog_fallback",
      buttonState: "checking",
    } as {
      progress: number;
      status: string;
      buttonState: "checking" | "idle";
    };

    const reset = vi.fn(() => {
      uiState.progress = 0;
      uiState.status = "idle";
      uiState.buttonState = "idle";
    });

    const timeoutId = scheduleSeriesCheckReset(
      uiState.status,
      reset,
      (cb, delay) => setTimeout(cb, delay) as unknown as number,
    );

    expect(timeoutId).not.toBeNull();
    expect(uiState.progress).toBe(100);
    expect(uiState.buttonState).toBe("checking");

    vi.advanceTimersByTime(1400);
    expect(reset).not.toHaveBeenCalled();

    vi.advanceTimersByTime(600);
    expect(reset).toHaveBeenCalledTimes(1);
    expect(uiState.progress).toBe(0);
    expect(uiState.status).toBe("idle");
    expect(uiState.buttonState).toBe("idle");

    vi.useRealTimers();
  });

  it("does not schedule reset for running status", () => {
    const reset = vi.fn();
    const timeoutId = scheduleSeriesCheckReset("running", reset, () => 1);
    expect(timeoutId).toBeNull();
    expect(reset).not.toHaveBeenCalled();
  });
});
