"use client"

import * as React from "react"
import { Dialog as DialogPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { useIsMobile } from "@/hooks/use-mobile"
import { XIcon } from "lucide-react"

function Dialog({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="dialog" {...props} />
}

function DialogTrigger({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Trigger>) {
  return <DialogPrimitive.Trigger data-slot="dialog-trigger" {...props} />
}

function DialogPortal({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="dialog-portal" {...props} />
}

function DialogClose({
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Close>) {
  return <DialogPrimitive.Close data-slot="dialog-close" {...props} />
}

function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        "fixed inset-0 isolate z-50 bg-black/10 duration-100 supports-backdrop-filter:backdrop-blur-xs data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0",
        className
      )}
      {...props}
    />
  )
}

function DialogContent({
  className,
  children,
  showCloseButton = true,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  showCloseButton?: boolean
}) {
  // A centered dialog (`top-1/2 + translate(-50%,-50%)`, capped at 85dvh) is
  // still positioned against the *layout* viewport on real mobile browsers --
  // when the on-screen keyboard opens, iOS/Android don't recompute a fixed
  // element's `top: 50%` against the shrunk *visual* viewport, so the box can
  // end up centered partway behind the keyboard with no way to pan/scroll it
  // into view (fine in devtools viewport-resize emulation, broken on a real
  // device -- live bug: AddBookDialog's fields/buttons stayed unreachable
  // until the keyboard was dismissed). Switching to an edge-anchored
  // `inset-0` sheet on mobile sidesteps the centering math entirely: it's
  // sized with `dvh`, which real browsers DO shrink live as the keyboard
  // opens, so the scrollable area always matches the actually-visible space.
  const isMobile = useIsMobile()

  return (
    <DialogPortal>
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        className={cn(
          isMobile
            ? "fixed inset-0 z-50 flex h-[100dvh] w-full max-w-none flex-col gap-4 overflow-y-auto rounded-none bg-popover p-4 text-sm text-popover-foreground outline-none duration-100 data-open:animate-in data-open:fade-in-0 data-open:slide-in-from-bottom-4 data-closed:animate-out data-closed:fade-out-0 data-closed:slide-out-to-bottom-4"
            : "fixed top-1/2 left-1/2 z-50 grid w-full max-w-[calc(100%-2rem)] max-h-[85dvh] -translate-x-1/2 -translate-y-1/2 gap-4 overflow-y-auto rounded-xl bg-popover p-4 text-sm text-popover-foreground ring-1 ring-foreground/10 duration-100 outline-none sm:max-w-sm data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95",
          className
        )}
        {...props}
      >
        {children}
        {showCloseButton && (
          <DialogPrimitive.Close data-slot="dialog-close" asChild>
            <Button
              variant="ghost"
              // `fixed` (not `absolute`) on mobile: DialogContent has no
              // transform in the mobile branch, so `position: fixed` here
              // resolves against the real viewport and stays put on screen
              // as the sheet's own content scrolls underneath it -- an
              // `absolute` close button would scroll away with the content
              // it's a sibling of and become unreachable without scrolling
              // back to the top first.
              className={isMobile ? "fixed top-3 right-3 z-20" : "absolute top-2 right-2"}
              size="icon-sm"
            >
              <XIcon
              />
              <span className="sr-only">Close</span>
            </Button>
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPortal>
  )
}

function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-header"
      className={cn(
        // sticky top-0 + solid bg: pins the title while the rest of the
        // dialog scrolls (mirrors DialogFooter below), and gives the
        // `fixed` mobile close button (see DialogContent) an opaque backdrop
        // instead of scrolling content showing through underneath it.
        "sticky top-0 z-10 -mx-4 -mt-4 flex flex-col gap-2 bg-popover px-4 pt-4 pb-2",
        className
      )}
      {...props}
    />
  )
}

function DialogFooter({
  className,
  showCloseButton = false,
  children,
  ...props
}: React.ComponentProps<"div"> & {
  showCloseButton?: boolean
}) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn(
        // sticky bottom-0: DialogContent's own children (header + a dialog's
        // fields + this footer) are one flat scrolling column with no
        // separate "body" slot -- without pinning, this footer (which holds
        // the primary Save/Confirm action) scrolls off the bottom along with
        // everything else once content is taller than the visible viewport,
        // e.g. behind an open mobile keyboard (live bug: had to scroll all
        // the way down, sometimes after dismissing the keyboard first, just
        // to reach "Save book"). No-op when content already fits.
        "-mx-4 -mb-4 sticky bottom-0 flex flex-col-reverse gap-2 rounded-b-xl border-t bg-muted/50 p-4 sm:flex-row sm:justify-end",
        className
      )}
      {...props}
    >
      {children}
      {showCloseButton && (
        <DialogPrimitive.Close asChild>
          <Button variant="outline">Close</Button>
        </DialogPrimitive.Close>
      )}
    </div>
  )
}

function DialogTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn(
        "font-heading text-base leading-none font-medium",
        className
      )}
      {...props}
    />
  )
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn(
        "text-sm text-muted-foreground *:[a]:underline *:[a]:underline-offset-3 *:[a]:hover:text-foreground",
        className
      )}
      {...props}
    />
  )
}

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
}
