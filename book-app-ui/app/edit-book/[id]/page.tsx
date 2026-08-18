"use client";

import { Suspense, useEffect, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";

import { AddBookFormFields } from "@/components/books/add-book-form-fields";
import { BookFormPageChrome } from "@/components/books/book-form-page-chrome";
import { useToast } from "@/components/ui/use-toast";
import { useDeviceClass } from "@/hooks/use-device-class";
import { useEditBookForm } from "@/hooks/use-edit-book-form";
import { fetchApiWithFallback } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import { getDeviceClass } from "@/lib/device-class";
import { isSeriesReturnTo, parsePositiveId, safeReturnTo, seriesIdFromReturnTo, withPin } from "@/lib/return-to";

function EditBookPageInner() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const { toast } = useToast();
  const { role } = useAuth();
  const deviceClass = useDeviceClass();
  const canEdit = role === "owner";
  const bookId = Number(params?.id);
  const returnTo = safeReturnTo(searchParams.get("returnTo"));
  const lockSeriesId = parsePositiveId(searchParams.get("seriesId")) ?? seriesIdFromReturnTo(returnTo);
  const allowDelete = isSeriesReturnTo(returnTo);
  const [deleting, setDeleting] = useState(false);

  const {
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
    onStatusChange,
    onToggleLookupSummary,
    handleFindDetails,
    handleSave,
  } = useEditBookForm({
    bookId: Number.isFinite(bookId) && bookId > 0 ? bookId : null,
    enabled: deviceClass !== "desktop" && canEdit && Number.isFinite(bookId) && bookId > 0,
    lockSeriesId,
    onSuccess: (updatedBook) => {
      if (returnTo) {
        router.push(
          Number.isFinite(updatedBook.id) && updatedBook.id > 0
            ? withPin(returnTo, updatedBook.id)
            : returnTo,
        );
        return;
      }
      if (Number.isFinite(updatedBook.id) && updatedBook.id > 0) {
        router.push(`/books?pin=${updatedBook.id}`);
        return;
      }
      router.push("/books");
    },
  });

  useEffect(() => {
    if (getDeviceClass() === "desktop" || !canEdit) {
      router.replace(returnTo || "/books");
    }
  }, [canEdit, deviceClass, returnTo, router]);

  if (deviceClass === "desktop" || !canEdit) {
    return null;
  }

  const goBack = () => router.push(returnTo || "/books");

  async function handleDelete() {
    if (!Number.isFinite(bookId) || bookId <= 0) return;
    if (!confirm(`Delete "${form.title || "this book"}"? This cannot be undone.`)) return;

    setDeleting(true);
    try {
      const response = await fetchApiWithFallback(`/books/${bookId}`, { method: "DELETE" });
      if (!response.ok) {
        throw new Error(`Failed to delete book (${response.status})`);
      }
      toast({
        title: "Book deleted",
        description: form.title ? `Deleted ${form.title}.` : "The book was deleted.",
      });
      router.push(returnTo || "/books");
    } catch (error) {
      console.error(error);
      toast({
        title: "Delete failed",
        description: error instanceof Error ? error.message : "Unable to delete book right now.",
      });
    } finally {
      setDeleting(false);
    }
  }

  if (!Number.isFinite(bookId) || bookId <= 0 || notFound) {
    return (
      <BookFormPageChrome
        title="Edit Book"
        subtitle="This book could not be found."
        onCancel={goBack}
        onSave={goBack}
        saving={false}
        saveLabel="Back to library"
      >
        <p className="text-sm text-muted-foreground">The book may have been deleted, or the link is invalid.</p>
      </BookFormPageChrome>
    );
  }

  return (
    <BookFormPageChrome
      title="Edit Book"
      subtitle={seriesLocked ? "Update this book in the series." : "Update this book in your library."}
      onCancel={goBack}
      onSave={handleSave}
      saving={saving || deleting}
      saveLabel="Save changes"
      saveDisabled={loading}
      onDelete={allowDelete ? () => void handleDelete() : undefined}
    >
      {loading ? (
        <p className="text-sm text-muted-foreground">Loading book…</p>
      ) : (
        <AddBookFormFields
          fieldIdPrefix="edit-book"
          form={form}
          onFieldChange={updateForm}
          onStatusChange={onStatusChange}
          seriesList={seriesList}
          lookingUpBook={lookingUpBook}
          lookupResult={lookupResult}
          showLookupSummary={showLookupSummary}
          onToggleLookupSummary={onToggleLookupSummary}
          onFindDetails={handleFindDetails}
          seriesLocked={seriesLocked}
        />
      )}
    </BookFormPageChrome>
  );
}

export default function EditBookPage() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-muted-foreground">Loading book…</div>}>
      <EditBookPageInner />
    </Suspense>
  );
}
