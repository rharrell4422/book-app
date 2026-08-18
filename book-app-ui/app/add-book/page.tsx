"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { AddBookFormFields } from "@/components/books/add-book-form-fields";
import { BookFormPageChrome } from "@/components/books/book-form-page-chrome";
import { useAddBookForm } from "@/hooks/use-add-book-form";
import { useDeviceClass } from "@/hooks/use-device-class";
import { useAuth } from "@/lib/auth-context";
import { getDeviceClass } from "@/lib/device-class";

export default function AddBookPage() {
  const router = useRouter();
  const { role } = useAuth();
  const deviceClass = useDeviceClass();
  const canEdit = role === "owner";

  const {
    form,
    seriesList,
    saving,
    lookingUpBook,
    lookupResult,
    showLookupSummary,
    updateAddBookForm,
    onStatusChange,
    onToggleLookupSummary,
    handleFindDetails,
    handleAddBook,
  } = useAddBookForm({
    enabled: deviceClass !== "desktop" && canEdit,
    onSuccess: (createdBook) => {
      if (Number.isFinite(createdBook.id) && createdBook.id > 0) {
        router.push(`/books?pin=${createdBook.id}`);
        return;
      }
      router.push("/books");
    },
  });

  useEffect(() => {
    if (getDeviceClass() === "desktop" || !canEdit) {
      router.replace("/books");
    }
  }, [canEdit, deviceClass, router]);

  if (deviceClass === "desktop" || !canEdit) {
    return null;
  }

  return (
    <BookFormPageChrome
      title="Add Book"
      subtitle="Add a standalone book or start a new series."
      onCancel={() => router.push("/books")}
      onSave={handleAddBook}
      saving={saving}
      saveLabel="Save book"
    >
      <AddBookFormFields
        form={form}
        onFieldChange={updateAddBookForm}
        onStatusChange={onStatusChange}
        seriesList={seriesList}
        lookingUpBook={lookingUpBook}
        lookupResult={lookupResult}
        showLookupSummary={showLookupSummary}
        onToggleLookupSummary={onToggleLookupSummary}
        onFindDetails={handleFindDetails}
      />
    </BookFormPageChrome>
  );
}
