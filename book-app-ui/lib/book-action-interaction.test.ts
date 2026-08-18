import { describe, expect, it } from "vitest";

import {
  DESTRUCTIVE_ACTION_DELAY_MS,
  getDefaultBookActionIconSize,
  resolvePhoneOrTabletTapPlan,
} from "./book-action-interaction";

describe("getDefaultBookActionIconSize", () => {
  it("gives desktop the smallest icon size", () => {
    expect(getDefaultBookActionIconSize("desktop")).toBe("icon-xs");
  });

  it("gives tablet a medium-small size", () => {
    expect(getDefaultBookActionIconSize("tablet")).toBe("icon-sm");
  });

  it("gives phone the largest size for reliable touch targets", () => {
    expect(getDefaultBookActionIconSize("phone")).toBe("icon-md");
  });
});

describe("resolvePhoneOrTabletTapPlan (device-class branching + disabled behavior)", () => {
  it("disabled actions never toast or fire, regardless of delayed", () => {
    expect(resolvePhoneOrTabletTapPlan({ delayed: false, disabled: true })).toEqual({ kind: "disabled" });
    expect(resolvePhoneOrTabletTapPlan({ delayed: true, disabled: true })).toEqual({ kind: "disabled" });
  });

  it("navigation actions (delayed: false) fire immediately with no toast", () => {
    expect(resolvePhoneOrTabletTapPlan({ delayed: false, disabled: false })).toEqual({ kind: "immediate" });
  });

  it("destructive/ambiguous actions (delayed: true) get a toast + short delay", () => {
    expect(resolvePhoneOrTabletTapPlan({ delayed: true, disabled: false })).toEqual({
      kind: "delayed",
      delayMs: DESTRUCTIVE_ACTION_DELAY_MS,
    });
  });
});
