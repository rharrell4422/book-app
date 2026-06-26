"use client";

import { use } from "react"; // ⭐ required for unwrapping params in Next.js 15
import { useEffect, useState } from "react";
import axios from "axios";
import Spinner from "@/components/ui/spinner";
import { toast } from "@/components/ui/use-toast";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import React from "react";

export default function SeriesDetailPage({ params }) {
  const { seriesId } = use(params); // ⭐ unwrap the Promise correctly

  const [series, setSeries] = useState<any>(null);
  const [books, setBooks] = useState<any[]>([]);
  const [loadingBookId, setLoadingBookId] = useState<number | null>(null);
  const [checkingNow, setCheckingNow] = useState(false);

  // collapsible sections
  const [showUnread, setShowUnread] = useState(true);
  const [showRead, setShowRead] = useState(true);
  const [showUpcoming, setShowUpcoming] = useState(true);

  // edit book number modal
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [selectedBook, setSelectedBook] = useState<any | null>(null);
  const [bookNumberInput, setBookNumberInput] = useState<string>("");


// ❌ OLD unwrap effect removed — params is no longer a Promise in Next.js 15
// ❌ No need to call setSeriesId — we now read seriesId directly from params
// ❌ No useEffect needed here at all

// ✅ Nothing goes here now — this section stays empty on purpose


  // fetch series once seriesId is available
  useEffect(() => {
    if (!seriesId) return;

    async function fetchSeries() {
      try {
        const response = await axios.get(
          `http://localhost:8000/series/${seriesId}`
        );
        setSeries(response.data);
        setBooks(response.data.books || []);
      } catch (error) {
        console.error("Error fetching series:", error);
      }
    }

    fetchSeries();
  }, [seriesId]);

  // helper: sort books by book_number (numbered first, then unnumbered)
  const sortBooks = (list: any[]) => {
    const numbered = list.filter((b) => b.book_number);
    const unnumbered = list.filter((b) => !b.book_number);

    numbered.sort((a, b) => (a.book_number || 0) - (b.book_number || 0));

    return [...numbered, ...unnumbered];
  };

  // optimistic toggle read/unread
const toggleRead = async (bookId: number) => {
  const originalBooks = [...books];
  const updatedBooks = books.map((b) =>
    b.id === bookId ? { ...b, is_read: !b.is_read } : b
  );

  setBooks(updatedBooks);
  setLoadingBookId(bookId);

  try {
    await axios.patch(`http://localhost:8000/books/${bookId}/toggle-read`);

    // Show toast confirmation
    toast({
      title: "Status updated",
      description: "Book status changed successfully.",
    });

    // Refresh series data so UI updates immediately
    const refreshed = await axios.get(`http://localhost:8000/series/${seriesId}`);
    setSeries(refreshed.data);
    setBooks(refreshed.data.books || []);
  } catch (error) {
    console.error("Toggle failed, rolling back", error);
    setBooks(originalBooks);
  } finally {
    setLoadingBookId(null);
  }
};

  // "Check Now" button logic
  const handleCheckNow = async () => {
    if (!seriesId) return;
    setCheckingNow(true);
    try {
      await axios.post(`http://localhost:8000/series/${seriesId}/check-now`);

      const refreshed = await axios.get(
        `http://localhost:8000/series/${seriesId}`
      );
      setSeries(refreshed.data);
      setBooks(refreshed.data.books || []);

      toast({
        title: "Series refreshed",
        description: "Latest book information retrieved.",
      });
    } catch (error) {
      console.error("Check Now failed:", error);
    } finally {
      setCheckingNow(false);
    }
  };

  // open edit dialog
  const openEditDialog = (book: any) => {
    setSelectedBook(book);
    setBookNumberInput(book.book_number ? String(book.book_number) : "");
    setEditDialogOpen(true);
  };

  // save book number
  const saveBookNumber = async () => {
    if (!selectedBook) return;

    const parsed = parseInt(bookNumberInput, 10);
    if (isNaN(parsed) || parsed <= 0) {
      toast({
        title: "Invalid number",
        description: "Book number must be a positive integer.",
      });
      return;
    }

    try {
      await axios.patch(`http://localhost:8000/books/${selectedBook.id}`, {
        book_number: parsed,
      });

      // refresh series after update
      if (seriesId) {
        const refreshed = await axios.get(
          `http://localhost:8000/series/${seriesId}`
        );
        setSeries(refreshed.data);
        setBooks(refreshed.data.books || []);
      }

      toast({
        title: "Book updated",
        description: "Book number saved successfully.",
      });

      setEditDialogOpen(false);
      setSelectedBook(null);
    } catch (error) {
      console.error("Error updating book number:", error);
      toast({
        title: "Update failed",
        description: "Could not save book number.",
      });
    }
  };

  if (!series) {
    return <div className="p-6">Loading series...</div>;
  }

  // grouping logic
  const readBooks = sortBooks(books.filter((b) => b.is_read));
  const unreadBooks = sortBooks(books.filter((b) => !b.is_read));

  const highestReadNumber = readBooks.length
    ? Math.max(...readBooks.map((b) => b.book_number || 0))
    : 0;

  const upcomingBooks = sortBooks(
    books.filter(
      (b) => b.book_number && b.book_number > highestReadNumber
    )
  );

  const percentRead =
    books.length > 0
      ? Math.round((readBooks.length / books.length) * 100)
      : 0;

  return (
    <>
      <div className="p-6 space-y-6">

        {/* HEADER */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold">{series.name}</h1>
            <p className="text-gray-600">By {series.author}</p>
          </div>

          <Button
            onClick={handleCheckNow}
            disabled={checkingNow}
            className="bg-green-700 text-white disabled:opacity-50 flex items-center gap-2"
          >
            {checkingNow ? <Spinner /> : "Check Now"}
          </Button>
        </div>

        {/* STATS CARD */}
        <div className="p-3 border rounded-lg shadow-sm bg-green-50">
          <h2 className="text-lg font-semibold mb-2 text-green-800">
            Reading Progress
          </h2>

          {/* Progress Bar */}
          <div className="w-full bg-gray-200 rounded-full h-3 mb-2 overflow-hidden">
            <div
              className="bg-green-600 h-3 rounded-full transition-all duration-500"
              style={{ width: `${percentRead}%` }}
            ></div>
          </div>

          <div className="grid grid-cols-2 gap-2 text-sm text-gray-700">
            <div>Total Books: {books.length}</div>
            <div>Read: {readBooks.length}</div>
            <div>Unread: {unreadBooks.length}</div>
            <div>Upcoming: {upcomingBooks.length}</div>
          </div>
        </div>

        {/* UNREAD SECTION */}
        <Section
          title="Unread Books"
          count={unreadBooks.length}
          stickyColor="text-green-700"
          show={showUnread}
          onToggle={() => setShowUnread((prev) => !prev)}
        >
          {showUnread && (
            <div className="space-y-1">
              {unreadBooks.map((book) => (
                <BookRow
                  key={book.id}
                  book={book}
                  loadingBookId={loadingBookId}
                  toggleRead={toggleRead}
                  openEditDialog={openEditDialog}
                />
              ))}
            </div>
          )}
        </Section>

        {/* READ SECTION */}
        <Section
          title="Read Books"
          count={readBooks.length}
          stickyColor="text-green-700"
          show={showRead}
          onToggle={() => setShowRead((prev) => !prev)}
        >
          {showRead && (
            <div className="space-y-1">
              {readBooks.map((book) => (
                <BookRow
                  key={book.id}
                  book={book}
                  loadingBookId={loadingBookId}
                  toggleRead={toggleRead}
                  openEditDialog={openEditDialog}
                />
              ))}
            </div>
          )}
        </Section>

        {/* UPCOMING SECTION */}
        <Section
          title="Upcoming Books"
          count={upcomingBooks.length}
          stickyColor="text-green-700"
          show={showUpcoming}
          onToggle={() => setShowUpcoming((prev) => !prev)}
        >
          {showUpcoming && (
            <div className="space-y-1">
              {upcomingBooks.map((book) => (
                <BookRow
                  key={book.id}
                  book={book}
                  loadingBookId={loadingBookId}
                  toggleRead={toggleRead}
                  openEditDialog={openEditDialog}
                />
              ))}
            </div>
          )}
        </Section>
      </div>

      {/* EDIT BOOK NUMBER DIALOG */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit Book Number</DialogTitle>
            <DialogDescription>
              Set the position of this book within the series.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label className="block mb-1">Title</Label>
              <div className="text-sm text-gray-700">
                {selectedBook?.title || ""}
              </div>
            </div>
            <div>
              <Label htmlFor="bookNumber" className="block mb-1">
                Book Number
              </Label>
              <Input
                id="bookNumber"
                type="number"
                min={1}
                value={bookNumberInput}
                onChange={(e) => setBookNumberInput(e.target.value)}
              />
            </div>
          </div>
          <DialogFooter className="mt-4 flex justify-end gap-2">
            <Button
              variant="outline"
              onClick={() => {
                setEditDialogOpen(false);
                setSelectedBook(null);
              }}
            >
              Cancel
            </Button>
            <Button onClick={saveBookNumber} className="bg-green-700 text-white">
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/* Sticky, collapsible section wrapper */
function Section({
  title,
  count,
  stickyColor,
  show,
  onToggle,
  children,
}: {
  title: string;
  count: number;
  stickyColor: string;
  show: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="sticky top-0 bg-white py-2 z-10 flex items-center justify-between border-b">
        <button
          onClick={onToggle}
          className="flex items-center gap-2 text-left"
        >
          <span className={stickyColor + " text-xl font-semibold"}>
            {title}
          </span>
          <span className="text-sm text-gray-600">({count})</span>
          <span className="text-sm text-gray-500">
            {show ? "▲" : "▼"}
          </span>
        </button>
      </div>
      {children}
    </div>
  );
}

/* Book Row Component (Condensed + Smart Number Logic + Edit Icon) */
function BookRow({
  book,
  loadingBookId,
  toggleRead,
  openEditDialog,
}: {
  book: any;
  loadingBookId: number | null;
  toggleRead: (id: number) => void;
  openEditDialog: (book: any) => void;
}) {
  const titleContainsNumber =
    book.book_number &&
    book.title.includes(book.book_number.toString());

  const showNumber =
    book.series_id && // only show numbers for series books
    book.book_number && // number exists
    !titleContainsNumber; // avoid redundancy

  const showUnknownNumber =
    book.series_id && // only for series books
    !book.book_number; // missing number

  return (
    <div className="flex items-center justify-between p-2 border rounded text-sm">
      <div className="space-y-1">
        <div className="font-medium">{book.title}</div>

        {showNumber && (
          <div className="flex items-center gap-2 text-xs text-gray-500">
            <span>Book {book.book_number}</span>
          </div>
        )}

        {showUnknownNumber && (
          <div className="flex items-center gap-2 text-xs text-gray-500">
            <span>Book #?</span>
            <button
              type="button"
              onClick={() => openEditDialog(book)}
              className="text-blue-600 hover:underline text-xs"
            >
              Edit
            </button>
          </div>
        )}
      </div>

      <Button
        onClick={() => toggleRead(book.id)}
        disabled={loadingBookId === book.id}
        className={`px-2 py-1 rounded text-white text-xs ${
          book.is_read ? "bg-green-700" : "bg-blue-600"
        } disabled:opacity-50 flex items-center gap-2`}
      >
        {loadingBookId === book.id ? (
          <Spinner />
        ) : book.is_read ? (
          "Unread"
        ) : (
          "Read"
        )}
      </Button>
    </div>
  );
}
