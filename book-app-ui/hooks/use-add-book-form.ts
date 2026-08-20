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
  type FindCandidate,
  type FindResultState,
  type LookupResultState,
} from "@/components/books/add-book-form-fields";

// What a confirmed FIND selection contributes to the eventual POST /books/
// body -- see services/metadata_provenance.py's provenance_for_find_bind,
// which this mirrors on the frontend side. Cleared (see updateAddBookForm)
// the moment the user edits title/author away from what was applied, so a
// stale "provider verified" stamp can never ride along with hand-edited
// data.
type SelectedFindCandidate = {
  candidateId: string;
  confidence: "high" | "medium" | "low";
  canonicalTitle: string | null;
  isbn13: string | null;
  sourceUrl: string | null;
  appliedTitle: string;
  appliedAuthor: string;
};

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
  const [findResult, setFindResult] = useState<FindResultState | null>(null);
  const [selectedCandidate, setSelectedCandidate] = useState<SelectedFindCandidate | null>(null);
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
    // A confirmed FIND selection stamps metadata_source="provider" on
    // submit (see handleAddBook) -- that claim stops being true the moment
    // the user hand-edits title/author away from what was actually applied,
    // so drop the binding rather than let a stale "provider verified" stamp
    // ride along with edited data.
    if ((key === "title" || key === "author") && selectedCandidate) {
      const nextValue = String(value ?? "").trim();
      const stillMatches =
        key === "title" ? nextValue === selectedCandidate.appliedTitle : nextValue === selectedCandidate.appliedAuthor;
      if (!stillMatches) {
        setSelectedCandidate(null);
      }
    }
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
    setFindResult(null);
    setSelectedCandidate(null);
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
    const seriesName = form.seriesName.trim();
    const bookNumberText = form.bookNumber.trim();

    if (!title) {
      toast({
        title: "Need a title",
        description: "Enter at least the book title before using Find details.",
      });
      return;
    }

    setLookingUpBook(true);
    setFindResult(null);
    setSelectedCandidate(null);

    try {
      const params = new URLSearchParams();
      params.set("title", title);
      if (author) {
        params.set("author", author);
      }
      // Forwarding book_number/series_name lets FIND (and the summary
      // lookup it's layered over) disambiguate same-titled results from a
      // different volume -- see routers/books.py's /find and /lookup.
      if (bookNumberText && Number.isFinite(Number(bookNumberText))) {
        params.set("book_number", bookNumberText);
      }
      if (seriesName) {
        params.set("series_name", seriesName);
      }

      const response = await fetchApiWithFallback(`/books/find?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`Find failed (${response.status})`);
      }

      const data: FindResultState = await response.json();
      setFindResult(data);

      if (!data.candidates.length) {
        toast({
          title: "No details found",
          description: "No match was found. You can still add the book manually.",
        });
        return;
      }

      toast({
        title: "Matches found",
        description: "Review the matches below and pick one, or dismiss to keep what you typed.",
      });
    } catch (error) {
      console.error("Error finding book details:", error);
      toast({
        title: "Find error",
        description: error instanceof Error ? error.message : "Unable to find book details.",
      });
    } finally {
      setLookingUpBook(false);
    }
  }

  function applyFindCandidate(candidate: FindCandidate) {
    // Fills gaps only -- never overwrites text the user already typed, and
    // never touches form.title itself (the user's own entry is preserved
    // forever; the candidate's title becomes canonical_title instead, sent
    // alongside title on submit -- see models.Book.canonical_title and
    // handleAddBook below).
    setForm((prev) => ({
      ...prev,
      title: prev.title.trim() || normalizeLookupMatchedTitle(candidate.title) || prev.title,
      author: prev.author.trim() || candidate.author?.trim() || prev.author,
      autoSummary: candidate.description || prev.autoSummary,
    }));
    setSelectedCandidate({
      candidateId: candidate.candidate_id,
      confidence: candidate.confidence,
      canonicalTitle: normalizeLookupMatchedTitle(candidate.title) || null,
      isbn13: candidate.isbn13,
      sourceUrl: candidate.source_url,
      appliedTitle: (form.title.trim() || normalizeLookupMatchedTitle(candidate.title) || "").trim(),
      appliedAuthor: (form.author.trim() || candidate.author?.trim() || "").trim(),
    });
    setFindResult(null);
    setShowLookupSummary(Boolean(candidate.description));
  }

  function declineFindCandidates() {
    // Explicitly "keep what I typed" -- clears the picker without touching
    // the form at all, so nothing the user already entered is disturbed.
    setFindResult(null);
  }

  async function handleAddBook() {
    const title = form.title.trim();
    // No placeholder fallback here -- an authorless locked series should
    // force the user to supply one (see the Missing-info check right below),
    // not silently write a value like "Unknown author" that normalizes to a
    // non-empty token and can poison cross-series author matching. The
    // series-locked add flow prefills this field from the series' own
    // resolved author (see lockedSeries in add-book/page.tsx and
    // AddBookDialog's initialValues) whenever that's actually known.
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

      // A FIND selection only counts as still-bound if the form's title/
      // author still match what was actually applied -- updateAddBookForm
      // already clears selectedCandidate the moment either drifts, but this
      // is a second, cheap belt-and-suspenders check right at submit time.
      const boundCandidate =
        selectedCandidate && selectedCandidate.appliedTitle === title && selectedCandidate.appliedAuthor === author
          ? selectedCandidate
          : null;

      const createBookResponse = await fetchApiWithFallback("/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          canonical_title: boundCandidate?.canonicalTitle || undefined,
          // metadata_source/needs_reresolution are derived server-side from
          // this alone (see crud.create_book / services/
          // metadata_provenance.py) -- the frontend never sends those two
          // directly, so it can't misrepresent a hand-typed entry as
          // provider-verified.
          find_confidence: boundCandidate?.confidence || undefined,
          isbn13: boundCandidate?.isbn13 || undefined,
          source_url: boundCandidate?.sourceUrl || undefined,
          author,
          series_id: resolvedSeriesId,
          series_order: effectiveBookNumber,
          // book_number_source is intentionally omitted here -- crud.
          // create_book's own _book_payload already stamps "user" whenever
          // book_number is present in the request (see crud/books.py), so
          // there's no need to duplicate that rule on the frontend too.
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
    findResult,
    selectedCandidateId: selectedCandidate?.candidateId ?? null,
    showLookupSummary,
    createdBookId,
    seriesLocked,
    updateAddBookForm,
    onClassificationChange,
    resetAddBookForm,
    onStatusChange,
    onToggleLookupSummary: () => setShowLookupSummary((prev) => !prev),
    handleFindDetails,
    applyFindCandidate,
    declineFindCandidates,
    handleAddBook,
  };
}
