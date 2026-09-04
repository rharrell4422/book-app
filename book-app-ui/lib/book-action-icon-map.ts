import {
  BookOpenIcon,
  CheckIcon,
  ExternalLinkIcon,
  FileTextIcon,
  PencilIcon,
  RotateCcwIcon,
  SearchIcon,
  Trash2Icon,
  type LucideIcon,
} from "lucide-react";

/**
 * Every book/series action icon this app renders, across the books list,
 * standalone books list, and series detail view. `state` reflects the
 * *current data condition* (e.g. "book is read", "no source_url yet") --
 * <BookActionIcon> resolves that into the icon/label/color for the action
 * the button actually performs (e.g. "read" -> show "Mark unread").
 */
export type BookActionState =
  | "read"
  | "unread"
  | "series"
  | "findPublicationDate"
  | "delete"
  | "edit"
  | "moreByAuthor"
  | "summarySeries"
  | "summaryStandaloneHasContent"
  | "summaryStandaloneEmpty";

/** Semantic color bucket, matching the spec's variant vocabulary. */
export type BookActionColorVariant = "neutral" | "positive" | "negative" | "warning";

/** Concrete <Button> variant needed to reproduce today's visual styling. */
export type BookActionButtonVariant = "ghost" | "outline" | "destructive";

export type BookActionVisual = {
  icon: LucideIcon;
  /** Short label used for the tap-toast (phone/tablet) and as a label fallback. */
  label: string;
  ariaLabel: string;
  /** Text shown in the desktop hover/focus tooltip -- same as label unless noted. */
  tooltipLabel: string;
  colorVariant: BookActionColorVariant;
  buttonVariant: BookActionButtonVariant;
  /** Extra classes needed for states whose color isn't fully captured by buttonVariant alone. */
  extraClassName?: string;
  /**
   * Destructive/ambiguous actions get a tap-toast + short delay on phone/tablet
   * before onClick fires. Navigation actions fire immediately with no toast.
   */
  delayed: boolean;
};

const BOOK_ACTION_VISUALS: Record<BookActionState, BookActionVisual> = {
  series: {
    icon: BookOpenIcon,
    label: "View books in this series",
    ariaLabel: "View books in this series",
    tooltipLabel: "View books in this series",
    colorVariant: "neutral",
    buttonVariant: "ghost",
    delayed: false,
  },
  summarySeries: {
    icon: FileTextIcon,
    label: "View/edit summary and notes",
    ariaLabel: "View/edit summary and notes",
    tooltipLabel: "View/edit summary and notes",
    colorVariant: "neutral",
    buttonVariant: "ghost",
    delayed: false,
  },
  summaryStandaloneHasContent: {
    icon: FileTextIcon,
    label: "View/edit summary and notes",
    ariaLabel: "View/edit summary and notes",
    tooltipLabel: "View/edit summary and notes",
    colorVariant: "neutral",
    buttonVariant: "ghost",
    delayed: false,
  },
  summaryStandaloneEmpty: {
    icon: FileTextIcon,
    label: "Fetch an AI summary for this book",
    ariaLabel: "Fetch an AI summary for this book",
    tooltipLabel: "Fetch an AI summary for this book",
    colorVariant: "neutral",
    buttonVariant: "ghost",
    delayed: false,
  },
  // 2026-09-03: replaces the old three-state "check online" action
  // (hasSourceUrl/missingSourceUrl/unconfirmedDate), which jumped straight
  // to a book's saved source_url when present. That URL could be Amazon,
  // Goodreads, Google, or anything else depending on which provider
  // originally supplied the book -- no consistency across books, and
  // clicking through rarely surfaced the one piece of data actually
  // wanted (the publication date). Deliberately one single state now,
  // same icon/label/behavior for every book regardless of source_url or
  // date-confirmed status: always a Google search for "<title> <author>
  // publication date" (see book-format.ts's getFindPublicationDateUrl).
  // The separate amber "date unconfirmed" warning badge next to the
  // status chip (BooksClient.tsx etc., driven by hasUnconfirmedReleaseDate)
  // is unrelated to this icon and is unaffected by this change.
  findPublicationDate: {
    icon: ExternalLinkIcon,
    label: "Find publication date",
    ariaLabel: "Find publication date",
    tooltipLabel: "Find publication date",
    colorVariant: "neutral",
    buttonVariant: "ghost",
    delayed: false,
  },
  moreByAuthor: {
    icon: SearchIcon,
    label: "More by this author",
    ariaLabel: "More by this author",
    tooltipLabel: "More by this author",
    colorVariant: "neutral",
    buttonVariant: "ghost",
    delayed: false,
  },
  edit: {
    icon: PencilIcon,
    label: "Edit book",
    ariaLabel: "Edit book",
    tooltipLabel: "Edit book",
    colorVariant: "neutral",
    buttonVariant: "outline",
    delayed: false,
  },
  unread: {
    icon: CheckIcon,
    label: "Mark read",
    ariaLabel: "Mark read",
    tooltipLabel: "Mark read",
    colorVariant: "positive",
    buttonVariant: "outline",
    extraClassName: "border-emerald-300 text-emerald-700 hover:bg-emerald-50",
    delayed: true,
  },
  read: {
    icon: RotateCcwIcon,
    label: "Mark unread",
    ariaLabel: "Mark unread",
    tooltipLabel: "Mark unread",
    colorVariant: "negative",
    buttonVariant: "outline",
    extraClassName: "border-rose-300 text-rose-700 hover:bg-rose-50",
    delayed: true,
  },
  delete: {
    icon: Trash2Icon,
    label: "Delete book",
    ariaLabel: "Delete book",
    tooltipLabel: "Delete book",
    colorVariant: "negative",
    buttonVariant: "destructive",
    delayed: true,
  },
};

export function getBookActionVisual(state: BookActionState): BookActionVisual {
  return BOOK_ACTION_VISUALS[state];
}

export function isDelayedBookAction(state: BookActionState): boolean {
  return BOOK_ACTION_VISUALS[state].delayed;
}
