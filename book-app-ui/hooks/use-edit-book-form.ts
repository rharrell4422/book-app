"use client";

import { useCallback, useEffect, useState } from "react";

import {
  EMPTY_ADD_BOOK_FORM,
  normalizeLookupMatchedTitle,
  type AddBookFormState,
  type AddBookSeriesOption,
  type BookClassification,
  type LookupResultState,
} from "@/components/books/add-book-form-fields";
import { useToast } from "@/components/ui/use-toast";
import { fetchApiWithFallback } from "@/lib/api-client";
import { type BookStatus, normalizeText, toIsoDateString } from "@/lib/book-format";
import { lockedSeriesEditDates } from "@/lib/locked-series-edit";
import { publishBookStatusUpdate } from "@/lib/book-status-sync";

export type UpdatedBook = {
  id: number;
  title?: string | null;
};

type BookRecord = {
  id?: number;
  title?: string | null;
  author?: string | null;
  series_id?: number | null;
  series_name?: string | null;
  book_number?: number | null;
  read_status?: string | null;
  is_read?: boolean | null;
  release_date?: string | null;
  publication_date?: string | null;
  read_date?: string | null;
  auto_summary?: string | null;
};

function dateField(value: string | null | undefined) {
  return toIsoDateString(value) || "";
}

function statusFromBook(book: BookRecord): BookStatus {
  const explicit = String(book.read_status || "").trim().toLowerCase();
  if (book.is_read || explicit === "read") return "read";
  if (explicit === "upcoming" || explicit === "available" || explicit === "unread") {
    return explicit;
  }
  return "unread";
}

function formFromBook(book: BookRecord, seriesList: AddBookSeriesOption[]): AddBookFormState {
  const seriesFromList = seriesList.find((series) => Number(series.id) === Number(book.series_id));
  return {
    title: String(book.title || ""),
    author: String(book.author || ""),
    // Initialize from the book's own series_id, per requirement -- not a
    // fixed default like Add Book's -- so opening the edit form for an
    // existing series book doesn't wrongly start on "Standalone" and hide
    // fields that already hold real values.
    classification: book.series_id ? "series" : "standalone",
    seriesName: String(book.series_name || seriesFromList?.name || ""),
    bookNumber: book.book_number !== null && book.book_number !== undefined ? String(book.book_number) : "",
    status: statusFromBook(book),
    releaseDate: dateField(book.release_date),
    publicationDate: dateField(book.publication_date),
    readDate: dateField(book.read_date),
    autoSummary: String(book.auto_summary || ""),
  };
}

export function useEditBookForm(options: {
  bookId: number | null;
  enabled?: boolean;
  onSuccess?: (updatedBook: UpdatedBook) => void | Promise<void>;
  lockSeriesId?: number | null;
}) {
  const { toast } = useToast();
  const bookId = options.bookId;
  const enabled = options.enabled ?? true;
  const onSuccess = options.onSuccess;
  const lockSeriesId = options.lockSeriesId ?? null;
  const seriesLocked = lockSeriesId !== null && lockSeriesId > 0;

  const [form, setForm] = useState<AddBookFormState>(EMPTY_ADD_BOOK_FORM);
  const [seriesList, setSeriesList] = useState<AddBookSeriesOption[]>([]);
  const [loadedBook, setLoadedBook] = useState<BookRecord | null>(null);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [lookingUpBook, setLookingUpBook] = useState(false);
  const [showLookupSummary, setShowLookupSummary] = useState(false);
  const [lookupResult, setLookupResult] = useState<LookupResultState | null>(null);

  const fetchSeriesList = useCallback(async () => {
    try {
      const response = await fetchApiWithFallback("/series/", { cache: "no-store" });
      const data = await response.json();
      return Array.isArray(data) ? (data as AddBookSeriesOption[]) : [];
    } catch (error) {
      console.error("Error fetching series:", error);
      return [] as AddBookSeriesOption[];
    }
  }, []);

  const loadBook = useCallback(async () => {
    if (!bookId || bookId <= 0) return;
    setLoading(true);
    setNotFound(false);
    try {
      const [series, bookResponse] = await Promise.all([
        fetchSeriesList(),
        fetchApiWithFallback(`/books/${bookId}`, { cache: "no-store" }),
      ]);
      setSeriesList(series);

      if (bookResponse.status === 404) {
        setNotFound(true);
        setLoadedBook(null);
        setForm(EMPTY_ADD_BOOK_FORM);
        return;
      }
      if (!bookResponse.ok) {
        throw new Error(`Failed to load book (${bookResponse.status})`);
      }

      const book: BookRecord = await bookResponse.json();
      const nextForm = formFromBook(book, series);
      if (seriesLocked) {
        const lockedSeries = series.find((item) => Number(item.id) === lockSeriesId);
        nextForm.seriesName = String(book.series_name || lockedSeries?.name || nextForm.seriesName);
        // A locked edit is always "part of this series" -- the toggle
        // itself is hidden in this context (see AddBookFormFields), same
        // as the locked Add Book default.
        nextForm.classification = "series";
      }
      setLoadedBook(book);
      setForm(nextForm);
      setLookupResult(null);
      setShowLookupSummary(Boolean(nextForm.autoSummary));
    } catch (error) {
      console.error("Error loading book:", error);
      toast({
        title: "Error",
        description: error instanceof Error ? error.message : "Unable to load this book.",
      });
    } finally {
      setLoading(false);
    }
  }, [bookId, fetchSeriesList, toast, seriesLocked, lockSeriesId]);

  useEffect(() => {
    if (!enabled) return;
    // loadBook sets loading/form state before its first await.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadBook();
  }, [enabled, loadBook]);

  function updateForm<K extends keyof AddBookFormState>(key: K, value: AddBookFormState[K]) {
    if (seriesLocked && key === "seriesName") return;
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function onClassificationChange(classification: BookClassification) {
    // Locked edits never show the toggle, so this shouldn't fire, but
    // guard anyway rather than let a stray call detach a locked edit from
    // its series.
    if (seriesLocked) return;
    setForm((prev) => ({
      ...prev,
      classification,
      // Same "hide and clear" rule as Add Book -- switching an existing
      // series book to Standalone must blank seriesName/bookNumber so
      // handleSave's series_id resolution (which is driven by whether
      // seriesName is non-empty) actually detaches it, not just visually
      // hides the fields while their old values still get submitted.
      seriesName: classification === "standalone" ? "" : prev.seriesName,
      bookNumber: classification === "standalone" ? "" : prev.bookNumber,
    }));
  }

  function onStatusChange(nextStatus: BookStatus) {
    setForm((prev) => ({
      ...prev,
      status: nextStatus,
      readDate: nextStatus === "read" ? prev.readDate : "",
      releaseDate: nextStatus === "upcoming" ? prev.releaseDate : "",
    }));
  }

  async function handleFindDetails() {
    const title = form.title.trim();
    const author = form.author.trim();

    if (!title) {
      toast({
        title: "Need a title",
        description: "Enter at least the book title before using Find details.",
      });
      return;
    }

    setLookingUpBook(true);

    try {
      const params = new URLSearchParams();
      params.set("title", title);
      if (author) {
        params.set("author", author);
      }

      const response = await fetchApiWithFallback(`/books/lookup?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`Lookup failed (${response.status})`);
      }

      const data: LookupResultState = await response.json();
      setLookupResult(data);

      if (!data.found) {
        toast({
          title: "No details found",
          description: "No match was found. You can still save the book manually.",
        });
        return;
      }

      setForm((prev) => ({
        ...prev,
        title: normalizeLookupMatchedTitle(data.matched_title) || prev.title,
        author: data.matched_author?.trim() || prev.author,
        autoSummary: data.summary || prev.autoSummary,
      }));
      setShowLookupSummary(Boolean(data.summary) || Boolean(form.autoSummary));

      toast({
        title: "Details found",
        description: "Matched title and author were applied to the form.",
      });
    } catch (error) {
      console.error("Error looking up book:", error);
      toast({
        title: "Lookup error",
        description: error instanceof Error ? error.message : "Unable to look up book details.",
      });
    } finally {
      setLookingUpBook(false);
    }
  }

  async function handleSave() {
    if (!bookId || bookId <= 0) return;

    const title = form.title.trim();
    const author = form.author.trim();
    const seriesName = form.seriesName.trim();
    const bookNumberText = form.bookNumber.trim();

    if (!title || !author) {
      toast({
        title: "Missing info",
        description: "Title and author are required.",
      });
      return;
    }

    const effectiveClassification: BookClassification = seriesLocked ? "series" : form.classification;
    const parsedBookNumber = bookNumberText ? Number(bookNumberText) : null;

    if (effectiveClassification === "series") {
      if (!seriesLocked && !seriesName) {
        toast({
          title: "Missing info",
          description: "Series name is required for a book marked as part of a series.",
        });
        return;
      }
      if (!Number.isFinite(parsedBookNumber) || parsedBookNumber === null || parsedBookNumber <= 0) {
        toast({
          title: "Invalid book number",
          description: "Book number is required and must be a positive number for a series book.",
        });
        return;
      }
    } else if (bookNumberText && !Number.isFinite(parsedBookNumber)) {
      toast({
        title: "Invalid book number",
        description: "Book number must be numeric when provided.",
      });
      return;
    }

    setSaving(true);

    try {
      let resolvedSeriesId: number | null = null;

      // Standalone detaches from any series entirely -- series_id resolves
      // to null and no create-or-match request runs, even if this book was
      // previously attached to a series (see recalculate_intelligence's
      // handling of previous_series_id in crud/books.py.update_book, which
      // already re-syncs the old series once its series_id changes away).
      if (effectiveClassification === "series") {
        if (seriesLocked) {
          resolvedSeriesId = lockSeriesId;
        } else if (seriesName) {
          const normalizedSeriesName = normalizeText(seriesName);
          const normalizedAuthor = normalizeText(author);
          const matchedSeries = seriesList.find((series) => {
            if (normalizeText(series.name) !== normalizedSeriesName) return false;
            const existingAuthor = normalizeText(series.author);
            return !existingAuthor || !normalizedAuthor || existingAuthor === normalizedAuthor;
          });

          if (matchedSeries) {
            resolvedSeriesId = Number(matchedSeries.id);
          } else {
            const createSeriesResponse = await fetchApiWithFallback("/series/", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ name: seriesName, author }),
            });
            if (!createSeriesResponse.ok) {
              throw new Error(`Failed to create series (${createSeriesResponse.status})`);
            }
            const createdSeries = await createSeriesResponse.json();
            resolvedSeriesId = Number(createdSeries.id);
          }
        }
      }

      const readStatus = form.status;
      const isRead = readStatus === "read";
      const libraryReadDate = readStatus === "read"
        ? (form.readDate || new Date().toISOString().split("T")[0])
        : null;
      const libraryReleaseDate = readStatus !== "read" ? form.releaseDate.trim() : "";

      const lockedDates = seriesLocked
        ? lockedSeriesEditDates({
            status: form.status,
            releaseDate: form.releaseDate,
            readDate: form.readDate,
            existingReleaseDate: loadedBook?.release_date,
            existingPublicationDate: loadedBook?.publication_date,
          })
        : null;

      // Belt-and-suspenders alongside onClassificationChange's clearing of
      // bookNumber -- Standalone never sends a book number, matching the
      // backend's own book_number-requires-series_id rejection.
      const effectiveBookNumber = effectiveClassification === "series" ? parsedBookNumber : null;

      const response = await fetchApiWithFallback(`/books/${bookId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          author,
          series_id: resolvedSeriesId,
          series_order: effectiveBookNumber,
          book_number: effectiveBookNumber,
          release_date: lockedDates ? lockedDates.release_date : (libraryReleaseDate || null),
          publication_date: form.publicationDate || null,
          read_date: lockedDates ? lockedDates.read_date : libraryReadDate,
          read_status: lockedDates ? lockedDates.read_status : readStatus,
          is_read: lockedDates ? lockedDates.is_read : isRead,
          auto_summary: form.autoSummary || null,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to update book (${response.status})`);
      }

      const updatedBook = await response.json();
      publishBookStatusUpdate(updatedBook);
      toast({
        title: "Book updated",
        description: `Saved changes for ${updatedBook.title}.`,
      });
      try {
        await onSuccess?.({
          id: Number(updatedBook.id),
          title: updatedBook.title,
        });
      } catch (error) {
        console.error("Error after updating book:", error);
      }
    } catch (error) {
      console.error("Error updating book:", error);
      toast({
        title: "Error",
        description: error instanceof Error ? error.message : "Failed to update book.",
      });
    } finally {
      setSaving(false);
    }
  }

  return {
    form,
    seriesList,
    saving,
    loading,
    notFound,
    lookingUpBook,
    lookupResult,
    showLookupSummary,
    seriesLocked,
    updateForm,
    onClassificationChange,
    onStatusChange,
    onToggleLookupSummary: () => setShowLookupSummary((prev) => !prev),
    handleFindDetails,
    handleSave,
  };
}
