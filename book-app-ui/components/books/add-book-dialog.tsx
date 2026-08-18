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
import { useAddBookForm, type CreatedBook } from "@/hooks/use-add-book-form";

/** Desktop Add Book surface. Phone/tablet use the /add-book page instead. */

export function AddBookDialog({
  open,
  onOpenChange,
  onSuccess,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: (createdBook: CreatedBook) => void | Promise<void>;
}) {
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
            Add a standalone book or start a new series by entering the first book you already own.
          </DialogDescription>
        </DialogHeader>

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

        <DialogFooter showCloseButton>
          <Button type="button" onClick={handleAddBook} disabled={saving}>
            {saving ? "Saving..." : "Save book"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
