import { type BookStatus, isFutureDate, isPastOrTodayDate, toIsoDateString } from "./book-format";

/** Series Edit date/status coupling: future date → upcoming, past/today → available. */
export function lockedSeriesEditDates(args: {
  status: BookStatus;
  releaseDate: string;
  readDate: string;
  existingReleaseDate?: string | null;
  existingPublicationDate?: string | null;
}): {
  read_status: BookStatus;
  is_read: boolean;
  read_date: string | null;
  release_date: string | null;
} {
  const today = new Date().toISOString().split("T")[0];
  const normalizedRelease = args.releaseDate.trim() ? toIsoDateString(args.releaseDate) : null;
  const normalizedRead = args.readDate.trim() ? toIsoDateString(args.readDate) : null;
  const existingDate = toIsoDateString(args.existingReleaseDate || args.existingPublicationDate);

  let readStatus: BookStatus = args.status;
  let isRead = args.status === "read";
  let readDate: string | null = null;
  let releaseDate: string | null | undefined;

  if (args.status === "read") {
    readDate = normalizedRead || today;
    releaseDate = null;
  } else if (args.status === "unread") {
    readDate = null;
    releaseDate = normalizedRelease;
  } else if (args.status === "upcoming") {
    readDate = null;
    releaseDate = normalizedRelease || existingDate || null;
  } else {
    readDate = null;
    releaseDate = !existingDate || isPastOrTodayDate(existingDate)
      ? normalizedRelease || null
      : normalizedRelease || existingDate;
  }

  const effectiveReleaseDate = String(releaseDate || "").trim() || existingDate;
  if (!readDate && effectiveReleaseDate) {
    if (isFutureDate(effectiveReleaseDate)) {
      readStatus = "upcoming";
      isRead = false;
      releaseDate = effectiveReleaseDate;
    } else if (isPastOrTodayDate(effectiveReleaseDate)) {
      readStatus = "available";
      isRead = false;
      releaseDate = effectiveReleaseDate;
    }
  }

  return {
    read_status: readStatus,
    is_read: isRead,
    read_date: readDate,
    release_date: releaseDate ?? null,
  };
}
