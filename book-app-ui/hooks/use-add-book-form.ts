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
  type LookupResultState,
} from "@/components/books/add-book-form-fields";

export type CreatedBook = {
  id: number;
  title?: string | null;
};

export function useAddBookForm(options?: {
  onSuccess?: (createdBook: CreatedBook) => void | Promise<void>;
  enabled?: boolean;
}) {
  const { toast } = useToast();
  const onSuccess = options?.onSuccess;
  const enabled = options?.enabled ?? true;

  const [form, setForm] = useState<AddBookFormState>(EMPTY_ADD_BOOK_FORM);
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
    if (!enabled) return;
    // fetchSeriesList sets state synchronously before its first await --
    // standard "fetch on mount" pattern, not derived state.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void fetchSeriesList();
  }, [enabled, fetchSeriesList]);

  function updateAddBookForm<K extends keyof AddBookFormState>(key: K, value: AddBookFormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function resetAddBookForm() {
    setForm(EMPTY_ADD_BOOK_FORM);
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

    const parsedBookNumber = bookNumberText ? Number(bookNumberText) : null;
    if (bookNumberText && !Number.isFinite(parsedBookNumber)) {
      toast({
        title: "Invalid book number",
        description: "Book number must be numeric when provided.",
      });
      return;
    }

    setSaving(true);

    try {
      let resolvedSeriesId: number | null = null;

      if (seriesName) {
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

      const readStatus = form.status;
      const isRead = readStatus === "read";
      const readDate = readStatus === "read"
        ? (form.readDate || new Date().toISOString().split("T")[0])
        : null;
      const releaseDate = readStatus !== "read" ? form.releaseDate.trim() : "";

      const createBookResponse = await fetchApiWithFallback("/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          author,
          series_id: resolvedSeriesId,
          series_order: parsedBookNumber,
          book_number: parsedBookNumber,
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
      await fetchSeriesList();
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
    updateAddBookForm,
    resetAddBookForm,
    onStatusChange,
    onToggleLookupSummary: () => setShowLookupSummary((prev) => !prev),
    handleFindDetails,
    handleAddBook,
  };
}
