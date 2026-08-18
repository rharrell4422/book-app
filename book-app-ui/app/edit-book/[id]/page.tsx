"use client";

import { Suspense, useEffect } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";

import { AddBookFormFields } from "@/components/books/add-book-form-fields";
import { BookFormPageChrome } from "@/components/books/book-form-page-chrome";
import { useDeviceClass } from "@/hooks/use-device-class";
import { useEditBookForm } from "@/hooks/use-edit-book-form";
import { useAuth } from "@/lib/auth-context";
import { getDeviceClass } from "@/lib/device-class";

function safeReturnTo(value: string | null) {
  if (!value || !value.startsWith("/") || value.startsWith("//")) {
    return null;
  }
  return value;
}

function EditBookPageInner() {
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const { role } = useAuth();
  const deviceClass = useDeviceClass();
  const canEdit = role === "owner";
  const bookId = Number(params?.id);
  const returnTo = safeReturnTo(searchParams.get("returnTo"));

  const {
    form,
    seriesList,
    saving,
    loading,
    notFound,
    lookingUpBook,
    lookupResult,
    showLookupSummary,
    updateForm,
    onStatusChange,
    onToggleLookupSummary,
    handleFindDetails,
    handleSave,
  } = useEditBookForm({
    bookId: Number.isFinite(bookId) && bookId > 0 ? bookId : null,
    enabled: deviceClass !== "desktop" && canEdit && Number.isFinite(bookId) && bookId > 0,
    onSuccess: (updatedBook) => {
      if (returnTo) {
        router.push(returnTo);
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
      subtitle="Update this book in your library."
      onCancel={goBack}
      onSave={handleSave}
      saving={saving}
      saveLabel="Save changes"
      saveDisabled={loading}
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
