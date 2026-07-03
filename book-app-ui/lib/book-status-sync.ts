const BOOK_STATUS_SYNC_KEY = "book-status-sync-v1";
const BOOK_STATUS_SYNC_EVENT = "book-status-sync-event";

export type BookStatusSyncPayload = {
  id: number;
  is_read: boolean;
  read_status: string;
  read_date: string | null;
  release_date: string | null;
  publication_date: string | null;
  series_id: number | null;
  title?: string;
  author?: string;
  book_number?: number | null;
  series_order?: number | null;
  series_name?: string | null;
  updated_at: number;
};

type BookStatusInput = {
  id?: number | string | null;
  is_read?: boolean | null;
  read_status?: string | null;
  read_date?: string | null;
  release_date?: string | null;
  publication_date?: string | null;
  series_id?: number | string | null;
  title?: string | null;
  author?: string | null;
  book_number?: number | null;
  series_order?: number | null;
  series_name?: string | null;
};

function normalizePayload(book: BookStatusInput): BookStatusSyncPayload {
  return {
    id: Number(book?.id),
    is_read: Boolean(book?.is_read),
    read_status: String(book?.read_status || (book?.is_read ? "read" : "unread")),
    read_date: book?.read_date ? String(book.read_date) : null,
    release_date: book?.release_date ? String(book.release_date) : null,
    publication_date: book?.publication_date ? String(book.publication_date) : null,
    series_id: book?.series_id == null ? null : Number(book.series_id),
    title: book?.title == null ? undefined : String(book.title),
    author: book?.author == null ? undefined : String(book.author),
    book_number: typeof book?.book_number === "number" ? book.book_number : null,
    series_order: typeof book?.series_order === "number" ? book.series_order : null,
    series_name: book?.series_name == null ? null : String(book.series_name),
    updated_at: Date.now(),
  };
}

export function publishBookStatusUpdate(book: BookStatusInput) {
  if (typeof window === "undefined") return;

  const payload = normalizePayload(book);
  window.dispatchEvent(new CustomEvent(BOOK_STATUS_SYNC_EVENT, { detail: payload }));

  try {
    window.localStorage.setItem(BOOK_STATUS_SYNC_KEY, JSON.stringify(payload));
  } catch {
    // Ignore storage failures in restricted browsing modes.
  }
}

export function subscribeBookStatusUpdates(
  onUpdate: (payload: BookStatusSyncPayload) => void,
) {
  if (typeof window === "undefined") {
    return () => {};
  }

  const handleCustomEvent = (event: Event) => {
    const customEvent = event as CustomEvent<BookStatusSyncPayload>;
    if (!customEvent.detail) return;
    onUpdate(customEvent.detail);
  };

  const handleStorageEvent = (event: StorageEvent) => {
    if (event.key !== BOOK_STATUS_SYNC_KEY || !event.newValue) return;
    try {
      const payload = JSON.parse(event.newValue) as BookStatusSyncPayload;
      if (!payload || typeof payload.id !== "number") return;
      onUpdate(payload);
    } catch {
      // Ignore malformed payloads.
    }
  };

  window.addEventListener(BOOK_STATUS_SYNC_EVENT, handleCustomEvent as EventListener);
  window.addEventListener("storage", handleStorageEvent);

  return () => {
    window.removeEventListener(BOOK_STATUS_SYNC_EVENT, handleCustomEvent as EventListener);
    window.removeEventListener("storage", handleStorageEvent);
  };
}