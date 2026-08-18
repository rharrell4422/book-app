"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { AddBookFormFields } from "@/components/books/add-book-form-fields";
import { Button } from "@/components/ui/button";
import { useAddBookForm } from "@/hooks/use-add-book-form";
import { useDeviceClass } from "@/hooks/use-device-class";
import { useVisualViewportBottomInset } from "@/hooks/use-visual-viewport-bottom-inset";
import { useAuth } from "@/lib/auth-context";
import { getDeviceClass } from "@/lib/device-class";

export default function AddBookPage() {
  const router = useRouter();
  const { role } = useAuth();
  const deviceClass = useDeviceClass();
  const canEdit = role === "owner";
  const keyboardInset = useVisualViewportBottomInset();

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
    // Click-time / post-hydrate classifier: server snapshot is always
    // "desktop", so a phone/tablet first paint must wait for matchMedia.
    if (getDeviceClass() === "desktop" || !canEdit) {
      router.replace("/books");
    }
  }, [canEdit, deviceClass, router]);

  if (deviceClass === "desktop" || !canEdit) {
    return null;
  }

  const saveBarPadding = `max(0.75rem, env(safe-area-inset-bottom))`;
  const contentBottomPad = `calc(5.5rem + env(safe-area-inset-bottom) + ${keyboardInset}px)`;

  return (
    <div className="relative min-h-full">
      <header className="sticky top-0 z-20 border-b bg-background/95 px-4 pt-[env(safe-area-inset-top)] backdrop-blur-sm">
        <div className="flex items-center gap-3 py-2">
          <Button type="button" variant="ghost" size="sm" onClick={() => router.push("/books")}>
            Cancel
          </Button>
          <div className="min-w-0">
            <h1 className="text-base font-semibold leading-tight">Add Book</h1>
            <p className="truncate text-xs text-muted-foreground">
              Add a standalone book or start a new series.
            </p>
          </div>
        </div>
      </header>

      <div className="space-y-3 px-4 py-4" style={{ paddingBottom: contentBottomPad }}>
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
      </div>

      <div
        className="fixed inset-x-0 z-30 border-t bg-background/95 px-4 pt-3 backdrop-blur-sm"
        style={{
          bottom: keyboardInset,
          paddingBottom: saveBarPadding,
        }}
      >
        <Button type="button" className="w-full" onClick={handleAddBook} disabled={saving}>
          {saving ? "Saving..." : "Save book"}
        </Button>
      </div>
    </div>
  );
}
