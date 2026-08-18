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
  lockSeriesId,
  onDelete,
}: {
  bookId: number | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: (updatedBook: UpdatedBook) => void | Promise<void>;
  lockSeriesId?: number | null;
  onDelete?: () => void;
}) {
  const seriesLocked = Boolean(lockSeriesId && lockSeriesId > 0);
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
    lockSeriesId,
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
            {seriesLocked
              ? "Update this book in the series. Series membership is locked."
              : "Update core book metadata from the library without leaving this page."}
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
          seriesLocked={seriesLocked}
        />

        <DialogFooter showCloseButton className={onDelete ? "sm:justify-between" : undefined}>
          {onDelete ? (
            <Button type="button" variant="destructive" onClick={onDelete} disabled={saving} className="sm:mr-auto">
              Delete book
            </Button>
          ) : null}
          <Button type="button" onClick={handleSave} disabled={saving}>
            {saving ? "Saving..." : "Save changes"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
