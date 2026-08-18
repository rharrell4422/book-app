import { describe, expect, it } from "vitest";

import { lockedSeriesEditDates } from "./locked-series-edit";

describe("lockedSeriesEditDates", () => {
  it("uses today when marking a book read without a read date", () => {
    const today = new Date().toISOString().split("T")[0];
    const result = lockedSeriesEditDates({
      status: "read",
      releaseDate: "2030-01-01",
      readDate: "",
    });
    expect(result.read_status).toBe("read");
    expect(result.is_read).toBe(true);
    expect(result.read_date).toBe(today);
    expect(result.release_date).toBeNull();
  });

  it("forces upcoming when the effective release date is in the future", () => {
    const result = lockedSeriesEditDates({
      status: "available",
      releaseDate: "2099-01-01",
      readDate: "",
    });
    expect(result.read_status).toBe("upcoming");
    expect(result.is_read).toBe(false);
    expect(result.release_date).toBe("2099-01-01");
  });

  it("forces available when the effective release date is in the past", () => {
    const result = lockedSeriesEditDates({
      status: "upcoming",
      releaseDate: "2001-01-01",
      readDate: "",
    });
    expect(result.read_status).toBe("available");
    expect(result.is_read).toBe(false);
    expect(result.release_date).toBe("2001-01-01");
  });

  it("keeps an existing future date when upcoming is saved without a new date", () => {
    const result = lockedSeriesEditDates({
      status: "upcoming",
      releaseDate: "",
      readDate: "",
      existingReleaseDate: "2099-06-01",
    });
    expect(result.read_status).toBe("upcoming");
    expect(result.release_date).toBe("2099-06-01");
  });
});
