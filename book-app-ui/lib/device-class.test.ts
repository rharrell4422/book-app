import { describe, expect, it, vi, afterEach } from "vitest";

import { getDeviceClass } from "./device-class";

function mockMatchMedia(matches: Record<string, boolean>) {
  const matchMedia = (query: string) =>
    ({
      matches: Boolean(matches[query]),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as MediaQueryList;

  vi.stubGlobal("window", { matchMedia });
}

describe("getDeviceClass", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns phone when width is under 768px", () => {
    mockMatchMedia({ "(max-width: 767px)": true });
    expect(getDeviceClass()).toBe("phone");
  });

  it("returns tablet for coarse pointer at tablet width (iPad)", () => {
    mockMatchMedia({
      "(max-width: 767px)": false,
      "(pointer: coarse)": true,
      "(hover: none)": false,
    });
    expect(getDeviceClass()).toBe("tablet");
  });

  it("returns tablet when hover is none at tablet width", () => {
    mockMatchMedia({
      "(max-width: 767px)": false,
      "(pointer: coarse)": false,
      "(hover: none)": true,
    });
    expect(getDeviceClass()).toBe("tablet");
  });

  it("returns desktop for fine pointer with hover at desktop width", () => {
    mockMatchMedia({
      "(max-width: 767px)": false,
      "(pointer: coarse)": false,
      "(hover: none)": false,
    });
    expect(getDeviceClass()).toBe("desktop");
  });
});
