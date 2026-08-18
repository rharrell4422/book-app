"use client";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { AddBookFormFields } from "@/components/books/add-book-form-fields";
import { useEditBookForm, type UpdatedBook } from "@/hooks/use-edit-book-form";

/** Desktop Edit Book surface. Phone/tablet use /edit-book/[id] instead. */

export function EditBookDialog({
  bookId,
  open,
  onOpenChange,
  onSuccess,
}: {
  bookId: number | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: (updatedBook: UpdatedBook) => void | Promise<void>;
}) {
  const {
    form,
    seriesList,
    saving,
    lookingUpBook,
    lookupResult,
    showLookupSummary,
    updateForm,
    onStatusChange,
    onToggleLookupSummary,
    handleFindDetails,
    handleSave,
  } = useEditBookForm({
    bookId,
    enabled: open && bookId !== null && bookId > 0,
    onSuccess: async (updatedBook) => {
      await onSuccess(updatedBook);
      onOpenChange(false);
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Edit Book</DialogTitle>
          <DialogDescription>
            Update core book metadata from the library without leaving this page.
          </DialogDescription>
        </DialogHeader>

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

        <DialogFooter showCloseButton>
          <Button type="button" onClick={handleSave} disabled={saving}>
            {saving ? "Saving..." : "Save changes"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
