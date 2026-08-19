"use client";

import { useCallback, useEffect, useState } from "react";

import { useToast } from "@/components/ui/use-toast";
import { fetchApiWithFallback } from "@/lib/api-client";
import { type BookStatus, normalizeText } from "@/lib/book-format";
import {
  EMPTY_ADD_BOOK_FORM,
  normalizeLookupMatchedTitle,
  type AddBookFormState,
  type AddBookSeriesOption,
  type BookClassification,
  type LookupResultState,
} from "@/components/books/add-book-form-fields";

export type CreatedBook = {
  id: number;
  title?: string | null;
  read_status?: string | null;
};

export type AddBookFormInitialValues = Partial<AddBookFormState>;

function lockedAddDefaults(initialValues?: AddBookFormInitialValues): AddBookFormState {
  return {
    ...EMPTY_ADD_BOOK_FORM,
    // A locked add is always "part of this series" -- the toggle itself
    // is hidden in this context (see AddBookFormFields), but the
    // classification value still needs to be "series" so handleAddBook's
    // effective-classification check below doesn't have to special-case it.
    classification: "series",
    status: initialValues?.status ?? "upcoming",
    seriesName: initialValues?.seriesName ?? "",
    author: initialValues?.author ?? "",
    bookNumber: initialValues?.bookNumber ?? "",
  };
}

export function useAddBookForm(options?: {
  onSuccess?: (createdBook: CreatedBook) => void | Promise<void>;
  enabled?: boolean;
  lockedSeriesId?: number | null;
  initialValues?: AddBookFormInitialValues;
}) {
  const { toast } = useToast();
  const onSuccess = options?.onSuccess;
  const enabled = options?.enabled ?? true;
  const lockedSeriesId = options?.lockedSeriesId ?? null;
  const initialValues = options?.initialValues;
  const seriesLocked = lockedSeriesId !== null && lockedSeriesId > 0;

  const [form, setForm] = useState<AddBookFormState>(
    seriesLocked ? lockedAddDefaults(initialValues) : EMPTY_ADD_BOOK_FORM,
  );
  const [seriesList, setSeriesList] = useState<AddBookSeriesOption[]>([]);
  const [saving, setSaving] = useState(false);
  const [lookingUpBook, setLookingUpBook] = useState(false);
  const [showLookupSummary, setShowLookupSummary] = useState(false);
  const [lookupResult, setLookupResult] = useState<LookupResultState | null>(null);
  const [createdBookId, setCreatedBookId] = useState<number | null>(null);

  const fetchSeriesList = useCallback(async () => {
    try {
      const response = await fetchApiWithFallback("/series/", { cache: "no-store" });
      const data = await response.json();
      setSeriesList(Array.isArray(data) ? data : []);
    } catch (error) {
      console.error("Error fetching series:", error);
    }
  }, []);

  useEffect(() => {
    if (!enabled || seriesLocked) return;
    // fetchSeriesList sets state synchronously before its first await --
    // standard "fetch on mount" pattern, not derived state.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void fetchSeriesList();
  }, [enabled, seriesLocked, fetchSeriesList]);

  useEffect(() => {
    if (!enabled || !seriesLocked) return;
    // Prefill locked series name/author as they arrive (e.g. after GET /series/:id).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setForm((prev) => ({
      ...prev,
      status:
        prev.status === "unread" && !prev.title && !prev.bookNumber
          ? (initialValues?.status ?? "upcoming")
          : prev.status,
      seriesName: initialValues?.seriesName || prev.seriesName,
      author: prev.author.trim() ? prev.author : (initialValues?.author || ""),
      bookNumber: prev.bookNumber || initialValues?.bookNumber || "",
    }));
  }, [enabled, seriesLocked, initialValues?.seriesName, initialValues?.author, initialValues?.status, initialValues?.bookNumber]);

  function updateAddBookForm<K extends keyof AddBookFormState>(key: K, value: AddBookFormState[K]) {
    if (seriesLocked && key === "seriesName") return;
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function onClassificationChange(classification: BookClassification) {
    // Locked adds never show the toggle, so this shouldn't fire, but guard
    // anyway rather than let a stray call flip a locked add to standalone.
    if (seriesLocked) return;
    setForm((prev) => ({
      ...prev,
      classification,
      // "Hide and clear" -- switching to Standalone must blank the
      // underlying values, not just stop rendering the inputs, otherwise a
      // previously-filled seriesName/bookNumber would still ride along in
      // the POST body after the toggle "hid" them.
      seriesName: classification === "standalone" ? "" : prev.seriesName,
      bookNumber: classification === "standalone" ? "" : prev.bookNumber,
    }));
  }

  function resetAddBookForm() {
    setForm(seriesLocked ? lockedAddDefaults(initialValues) : EMPTY_ADD_BOOK_FORM);
    setLookupResult(null);
    setShowLookupSummary(false);
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
          description: "No match was found. You can still add the book manually.",
        });
        return;
      }

      setForm((prev) => ({
        ...prev,
        title: normalizeLookupMatchedTitle(data.matched_title) || prev.title,
        author: data.matched_author?.trim() || prev.author,
        autoSummary: data.summary || prev.autoSummary,
      }));
      setShowLookupSummary(false);

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

  async function handleAddBook() {
    const title = form.title.trim();
    const author = form.author.trim() || (seriesLocked ? "Unknown author" : "");
    const seriesName = form.seriesName.trim();
    const bookNumberText = form.bookNumber.trim();

    if (!title || !author) {
      toast({
        title: "Missing info",
        description: "Title and author are required.",
      });
      return;
    }

    // Locked adds are always "series" (the toggle is hidden in that
    // context); otherwise trust the toggle itself as the authoritative
    // signal for whether this submission should end up with a series_id.
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
      // Standalone clears bookNumber on toggle, so this is just a safety
      // net against stale state rather than the normal path.
      toast({
        title: "Invalid book number",
        description: "Book number must be numeric when provided.",
      });
      return;
    }

    setSaving(true);

    try {
      let resolvedSeriesId: number | null = null;

      // Standalone skips series create-or-match entirely -- series_id
      // stays null, and no /series/ request is made on its behalf.
      if (effectiveClassification === "series") {
        if (seriesLocked) {
          resolvedSeriesId = lockedSeriesId;
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
              body: JSON.stringify({
                name: seriesName,
                author,
              }),
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
      const readDate = readStatus === "read"
        ? (form.readDate || new Date().toISOString().split("T")[0])
        : null;
      const releaseDate = readStatus !== "read" ? form.releaseDate.trim() : "";
      // Belt-and-suspenders alongside onClassificationChange's clearing of
      // bookNumber -- Standalone never sends a book number, matching the
      // backend's own book_number-requires-series_id rejection.
      const effectiveBookNumber = effectiveClassification === "series" ? parsedBookNumber : null;

      const createBookResponse = await fetchApiWithFallback("/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          author,
          series_id: resolvedSeriesId,
          series_order: effectiveBookNumber,
          book_number: effectiveBookNumber,
          release_date: releaseDate || undefined,
          publication_date: form.publicationDate || undefined,
          read_date: readDate || undefined,
          read_status: readStatus,
          is_read: isRead,
          auto_summary: form.autoSummary || undefined,
        }),
      });

      if (!createBookResponse.ok) {
        throw new Error(`Failed to create book (${createBookResponse.status})`);
      }

      const createdBook = await createBookResponse.json();
      const nextCreatedBookId = Number(createdBook?.id ?? null);
      setCreatedBookId(Number.isFinite(nextCreatedBookId) ? nextCreatedBookId : null);
      if (!seriesLocked) {
        await fetchSeriesList();
      }
      resetAddBookForm();
      toast({
        title: "Book added",
        description: resolvedSeriesId
          ? `Added ${createdBook.title} and attached it to a series.`
          : `Added ${createdBook.title} to your library.`,
      });
      try {
        await onSuccess?.({
          id: nextCreatedBookId,
          title: createdBook.title,
          read_status: createdBook.read_status,
        });
      } catch (error) {
        console.error("Error after adding book:", error);
      }
    } catch (error) {
      console.error("Error adding book:", error);
      toast({
        title: "Error",
        description: error instanceof Error ? error.message : "Failed to add book.",
      });
    } finally {
      setSaving(false);
    }
  }

  return {
    form,
    seriesList,
    saving,
    lookingUpBook,
    lookupResult,
    showLookupSummary,
    createdBookId,
    seriesLocked,
    updateAddBookForm,
    onClassificationChange,
    resetAddBookForm,
    onStatusChange,
    onToggleLookupSummary: () => setShowLookupSummary((prev) => !prev),
    handleFindDetails,
    handleAddBook,
  };
}
