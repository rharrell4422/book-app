import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createTapActionTimer } from "./tap-action-timer";

describe("createTapActionTimer", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("fires onFire after delayMs", () => {
    const onFire = vi.fn();
    const timer = createTapActionTimer({ delayMs: 500, onFire });

    timer.trigger();
    expect(onFire).not.toHaveBeenCalled();

    vi.advanceTimersByTime(499);
    expect(onFire).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1);
    expect(onFire).toHaveBeenCalledTimes(1);
  });

  it("reports isPending while waiting and not once fired", () => {
    const timer = createTapActionTimer({ delayMs: 500, onFire: () => {} });

    expect(timer.isPending()).toBe(false);
    timer.trigger();
    expect(timer.isPending()).toBe(true);

    vi.advanceTimersByTime(500);
    expect(timer.isPending()).toBe(false);
  });

  it("a second trigger during the delay cancels the first and restarts the countdown", () => {
    const onFire = vi.fn();
    const timer = createTapActionTimer({ delayMs: 500, onFire });

    timer.trigger();
    vi.advanceTimersByTime(400);
    timer.trigger(); // double-tap: restart from zero
    vi.advanceTimersByTime(400);
    expect(onFire).not.toHaveBeenCalled(); // would have fired at 500 without the restart

    vi.advanceTimersByTime(100);
    expect(onFire).toHaveBeenCalledTimes(1);
  });

  it("cancel() prevents a pending fire", () => {
    const onFire = vi.fn();
    const timer = createTapActionTimer({ delayMs: 500, onFire });

    timer.trigger();
    timer.cancel();
    vi.advanceTimersByTime(1000);

    expect(onFire).not.toHaveBeenCalled();
    expect(timer.isPending()).toBe(false);
  });

  it("cancel() is a no-op when nothing is pending", () => {
    const timer = createTapActionTimer({ delayMs: 500, onFire: () => {} });
    expect(() => timer.cancel()).not.toThrow();
  });
});
