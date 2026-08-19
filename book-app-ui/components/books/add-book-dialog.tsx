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
import { useAddBookForm, type AddBookFormInitialValues, type CreatedBook } from "@/hooks/use-add-book-form";

/** Desktop Add Book surface. Phone/tablet use the /add-book page instead. */

export function AddBookDialog({
  open,
  onOpenChange,
  onSuccess,
  lockedSeriesId,
  initialValues,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: (createdBook: CreatedBook) => void | Promise<void>;
  lockedSeriesId?: number | null;
  initialValues?: AddBookFormInitialValues;
}) {
  const seriesLocked = Boolean(lockedSeriesId && lockedSeriesId > 0);
  const {
    form,
    seriesList,
    saving,
    lookingUpBook,
    lookupResult,
    showLookupSummary,
    updateAddBookForm,
    onClassificationChange,
    onStatusChange,
    onToggleLookupSummary,
    handleFindDetails,
    handleAddBook,
  } = useAddBookForm({
    enabled: open,
    lockedSeriesId,
    initialValues,
    onSuccess: async (createdBook) => {
      await onSuccess(createdBook);
      onOpenChange(false);
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Add Book</DialogTitle>
          <DialogDescription>
            {seriesLocked
              ? "Add a book to this series. Series membership is locked."
              : "Add a standalone book or start a new series by entering the first book you already own."}
          </DialogDescription>
        </DialogHeader>

        <AddBookFormFields
          form={form}
          onFieldChange={updateAddBookForm}
          onClassificationChange={onClassificationChange}
          onStatusChange={onStatusChange}
          seriesList={seriesList}
          lookingUpBook={lookingUpBook}
          lookupResult={lookupResult}
          showLookupSummary={showLookupSummary}
          onToggleLookupSummary={onToggleLookupSummary}
          onFindDetails={handleFindDetails}
          seriesLocked={seriesLocked}
        />

        <DialogFooter showCloseButton>
          <Button type="button" onClick={handleAddBook} disabled={saving}>
            {saving ? "Saving..." : "Save book"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
