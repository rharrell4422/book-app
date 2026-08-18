import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  MAX_VISIBLE_TOASTS,
  TOAST_AUTO_DISMISS_MS,
  addToastRecord,
  removeToastRecord,
  scheduleToastAutoDismiss,
  type ToastRecord,
} from "./toast-scheduler";

describe("addToastRecord", () => {
  it("appends a toast to the list", () => {
    const toasts: ToastRecord[] = [{ id: "a" }];
    expect(addToastRecord(toasts, { id: "b" })).toEqual([{ id: "a" }, { id: "b" }]);
  });

  it("caps the list at MAX_VISIBLE_TOASTS, dropping the oldest first (prevents accumulation)", () => {
    let toasts: ToastRecord[] = [];
    for (let i = 0; i < MAX_VISIBLE_TOASTS + 5; i++) {
      toasts = addToastRecord(toasts, { id: String(i) });
    }
    expect(toasts).toHaveLength(MAX_VISIBLE_TOASTS);
    // The most recent MAX_VISIBLE_TOASTS ids should be kept, oldest dropped.
    expect(toasts.map((t) => t.id)).toEqual(["5", "6", "7"]);
  });
});

describe("removeToastRecord", () => {
  it("removes a toast by id", () => {
    const toasts: ToastRecord[] = [{ id: "a" }, { id: "b" }];
    expect(removeToastRecord(toasts, "a")).toEqual([{ id: "b" }]);
  });

  it("is a no-op if the id isn't present", () => {
    const toasts: ToastRecord[] = [{ id: "a" }];
    expect(removeToastRecord(toasts, "missing")).toEqual(toasts);
  });
});

describe("scheduleToastAutoDismiss", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("calls onDismiss after the default auto-dismiss duration", () => {
    const onDismiss = vi.fn();
    scheduleToastAutoDismiss(onDismiss);

    vi.advanceTimersByTime(TOAST_AUTO_DISMISS_MS - 1);
    expect(onDismiss).not.toHaveBeenCalled();

    vi.advanceTimersByTime(1);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("the returned canceler prevents onDismiss from firing", () => {
    const onDismiss = vi.fn();
    const cancel = scheduleToastAutoDismiss(onDismiss, 1000);

    cancel();
    vi.advanceTimersByTime(2000);

    expect(onDismiss).not.toHaveBeenCalled();
  });
});
