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

export type ConfirmDialogState = {
  title: string;
  description?: string;
  confirmLabel?: string;
  destructive?: boolean;
  onConfirm: () => void;
};

/**
 * Single reusable confirm dialog. Pages hold their own `ConfirmDialogState |
 * null` and pass it here rather than each rolling its own confirm modal (or,
 * previously, calling the blocking `window.confirm`).
 */
export function ConfirmDialog({
  state,
  onOpenChange,
}: {
  state: ConfirmDialogState | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={Boolean(state)} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{state?.title}</DialogTitle>
          {state?.description ? <DialogDescription>{state.description}</DialogDescription> : null}
        </DialogHeader>
        <DialogFooter showCloseButton>
          <Button
            type="button"
            variant={state?.destructive ? "destructive" : "default"}
            onClick={() => {
              const action = state?.onConfirm;
              onOpenChange(false);
              action?.();
            }}
          >
            {state?.confirmLabel || "Confirm"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
