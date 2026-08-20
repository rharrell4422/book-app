"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { AddBookFormFields } from "@/components/books/add-book-form-fields";
import { BookFormPageChrome } from "@/components/books/book-form-page-chrome";
import { useAddBookForm } from "@/hooks/use-add-book-form";
import { useDeviceClass } from "@/hooks/use-device-class";
import { fetchApiWithFallback } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import { getDeviceClass } from "@/lib/device-class";
import { parsePositiveId, safeReturnTo, seriesDetailPath, withPin } from "@/lib/return-to";

function AddBookPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { role } = useAuth();
  const deviceClass = useDeviceClass();
  const canEdit = role === "owner";
  const lockedSeriesId = parsePositiveId(searchParams.get("seriesId"));
  const returnTo = safeReturnTo(searchParams.get("returnTo"));
  const fallbackHref = lockedSeriesId ? seriesDetailPath(lockedSeriesId) : "/books";
  const cancelHref = returnTo || fallbackHref;

  const [lockedSeries, setLockedSeries] = useState<{ name: string; author: string } | null>(null);

  useEffect(() => {
    if (!lockedSeriesId) return;

    let cancelled = false;
    void (async () => {
      try {
        const response = await fetchApiWithFallback(`/series/${lockedSeriesId}`, { cache: "no-store" });
        if (!response.ok || cancelled) return;
        const data = await response.json();
        if (cancelled) return;
        // No "Unknown author" placeholder fallback -- GET /series/:id already
        // resolves this to one of the series' own books' authors when the
        // series row itself has none (see crud/series.py's series_author
        // computation). If it's still empty here, the series genuinely has
        // no author signal anywhere, and the form should require the user
        // to type one rather than writing a placeholder that would poison
        // cross-series author matching (see services/identity.py).
        setLockedSeries({
          name: String(data?.name || ""),
          author: String(data?.author || "").trim(),
        });
      } catch (error) {
        console.error("Error loading series for add book:", error);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [lockedSeriesId]);

  const {
    form,
    seriesList,
    saving,
    lookingUpBook,
    lookupResult,
    findResult,
    selectedCandidateId,
    showLookupSummary,
    seriesLocked,
    updateAddBookForm,
    onClassificationChange,
    onStatusChange,
    onToggleLookupSummary,
    handleFindDetails,
    applyFindCandidate,
    declineFindCandidates,
    handleAddBook,
  } = useAddBookForm({
    enabled: deviceClass !== "desktop" && canEdit,
    lockedSeriesId,
    initialValues:
      lockedSeriesId && lockedSeries
        ? {
            seriesName: lockedSeries.name,
            author: lockedSeries.author,
            status: "upcoming",
          }
        : undefined,
    onSuccess: (createdBook) => {
      if (Number.isFinite(createdBook.id) && createdBook.id > 0) {
        router.push(withPin(cancelHref, createdBook.id));
        return;
      }
      router.push(cancelHref);
    },
  });

  useEffect(() => {
    if (getDeviceClass() === "desktop" || !canEdit) {
      router.replace(cancelHref);
    }
  }, [canEdit, deviceClass, cancelHref, router]);

  if (deviceClass === "desktop" || !canEdit) {
    return null;
  }

  return (
    <BookFormPageChrome
      title="Add Book"
      subtitle={
        seriesLocked
          ? `Adding to ${lockedSeries?.name || "this series"}.`
          : "Add a standalone book or start a new series."
      }
      onCancel={() => router.push(cancelHref)}
      onSave={handleAddBook}
      saving={saving}
      saveLabel="Save book"
    >
      <AddBookFormFields
        form={form}
        onFieldChange={updateAddBookForm}
        onClassificationChange={onClassificationChange}
        onStatusChange={onStatusChange}
        seriesList={seriesList}
        lookingUpBook={lookingUpBook}
        lookupResult={lookupResult}
        findResult={findResult}
        selectedCandidateId={selectedCandidateId}
        onSelectCandidate={applyFindCandidate}
        onDeclineCandidates={declineFindCandidates}
        showLookupSummary={showLookupSummary}
        onToggleLookupSummary={onToggleLookupSummary}
        onFindDetails={handleFindDetails}
        seriesLocked={seriesLocked}
      />
    </BookFormPageChrome>
  );
}

export default function AddBookPage() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-muted-foreground">Loading…</div>}>
      <AddBookPageInner />
    </Suspense>
  );
}
