"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { useToast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function getBookStatus(book: any) {
  return book.read_status ?? (book.is_read ? "read" : "upcoming");
}

function getDisplayDate(book: any) {
  const status = getBookStatus(book);
  return status === "upcoming"
    ? book.release_date || book.read_date
    : book.read_date || book.release_date;
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString();
}

export default function BooksClient() {
  const { toast } = useToast();
  const [books, setBooks] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const searchParams = useSearchParams();
  const router = useRouter();
  const seriesId = searchParams.get("series_id");

  const totalBooks = books.length;
  const readBooks = books.filter((book) => book.is_read).length;
  const unreadBooks = books.filter((book) => !book.is_read).length;
  const upcomingBooks = books.filter((book) => getBookStatus(book) === "upcoming").length;

  async function fetchBooks() {
    setLoading(true);
    try {
      const url = seriesId
        ? `http://localhost:8000/books/by_series/${seriesId}`
        : "http://localhost:8000/books/";

      const response = await fetch(url, { cache: "no-store" });
      const data = await response.json();
      setBooks(data);
    } catch (error) {
      console.error("Error fetching books:", error);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchBooks();
  }, [seriesId]);

  async function toggleRead(book: any) {
    try {
      const response = await fetch(`http://localhost:8000/books/${book.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          is_read: !book.is_read,
          read_date: !book.is_read ? new Date().toISOString().split("T")[0] : null,
        }),
      });

      if (response.ok) {
        toast({
          title: "Updated",
          description: `Marked book ${book.id} as ${!book.is_read ? "read" : "unread"}.`,
        });
        fetchBooks();
      } else {
        toast({
          title: "Error",
          description: "Failed to update book.",
        });
      }
    } catch (error) {
      console.error("Error updating book:", error);
    }
  }

  async function deleteBook(bookId: number) {
    if (!confirm("Delete this book?")) return;

    try {
      const response = await fetch(`http://localhost:8000/books/${bookId}`, {
        method: "DELETE",
      });

      if (response.ok) {
        toast({
          title: "Deleted",
          description: `Book ${bookId} removed.`,
        });
        fetchBooks();
      } else {
        toast({
          title: "Error",
          description: "Failed to delete book.",
        });
      }
    } catch (error) {
      console.error("Error deleting book:", error);
    }
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <p className="text-sm uppercase tracking-[0.2em] text-muted-foreground">
            Library
          </p>
          <h1 className="text-3xl font-bold">
            {seriesId ? `Series ${seriesId} books` : "All books"}
          </h1>
          <p className="max-w-2xl text-sm leading-6 text-muted-foreground">
            Browse the collection with read status, release dates, and series links.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <Link href="/books">
            <Button type="button" variant="outline">All Books</Button>
          </Link>
          <Link href="/series">
            <Button type="button" variant="secondary">Series</Button>
          </Link>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Total books</p>
          <p className="text-2xl font-semibold">{totalBooks}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Read</p>
          <p className="text-2xl font-semibold">{readBooks}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Unread</p>
          <p className="text-2xl font-semibold">{unreadBooks}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Upcoming</p>
          <p className="text-2xl font-semibold">{upcomingBooks}</p>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border bg-card/80">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>Title</TableHead>
              <TableHead>Author</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Date</TableHead>
              <TableHead>Series</TableHead>
              <TableHead>Book #</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {Array.isArray(books) &&
              books.map((b) => {
                const status = getBookStatus(b);
                return (
                  <TableRow key={b.id}>
                    <TableCell>{b.id}</TableCell>
                    <TableCell>{b.title}</TableCell>
                    <TableCell>{b.author || "—"}</TableCell>
                    <TableCell className="capitalize">{status}</TableCell>
                    <TableCell>{formatDate(getDisplayDate(b))}</TableCell>
                    <TableCell>{b.series_name || "—"}</TableCell>
                    <TableCell>{b.book_number ?? "—"}</TableCell>
                    <TableCell className="flex flex-wrap gap-2">
                      {b.series_id ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => router.push(`/series/${b.series_id}`)}
                      >
                        Series
                      </Button>
                    ) : null}
                    <Button type="button" variant="secondary" size="sm" onClick={() => toggleRead(b)}>
                      {b.is_read ? "Mark unread" : "Mark read"}
                    </Button>
                    <Button type="button" variant="destructive" size="sm" onClick={() => deleteBook(b.id)}>
                      Delete
                    </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
          </TableBody>
        </Table>
      </div>
      {loading && <p className="text-sm text-muted-foreground">Loading books…</p>}
    </div>
  );
}
