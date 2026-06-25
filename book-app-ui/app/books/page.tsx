"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useToast } from "@/components/ui/use-toast";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export default function BooksPage() {
  const { toast } = useToast();
  const [books, setBooks] = useState<any[]>([]);
  const searchParams = useSearchParams();
  const seriesId = searchParams.get("series_id");

  async function fetchBooks() {
    try {
      const url = seriesId
        ? `http://127.0.0.1:8000/books/by_series/${seriesId}`
        : "http://127.0.0.1:8000/books/";

      const response = await fetch(url, { cache: "no-store" });
      const data = await response.json();
      setBooks(data);
    } catch (error) {
      console.error("Error fetching books:", error);
    }
  }

  useEffect(() => {
    fetchBooks();
  }, [seriesId]);

  async function toggleRead(book: any) {
    try {
      const response = await fetch(
        `http://127.0.0.1:8000/books/${book.id}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            is_read: !book.is_read,
            read_date: !book.is_read ? new Date().toISOString().split("T")[0] : null,
          }),
        }
      );

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
      const response = await fetch(
        `http://127.0.0.1:8000/books/${bookId}`,
        { method: "DELETE" }
      );

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
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">
        {seriesId ? `Books for Series ${seriesId}` : "All Books"}
      </h1>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>ID</TableHead>
            <TableHead>Title</TableHead>
            <TableHead>Author</TableHead>
            <TableHead>Series ID</TableHead>
            <TableHead>Book #</TableHead>
            <TableHead>Release Date</TableHead>
            <TableHead>Read?</TableHead>
            <TableHead>Actions</TableHead>
          </TableRow>
        </TableHeader>

        <TableBody>
          {Array.isArray(books) &&
            books.map((b) => (
              <TableRow key={b.id}>
                <TableCell>{b.id}</TableCell>
                <TableCell>{b.title}</TableCell>
                <TableCell>{b.author}</TableCell>
                <TableCell>{b.series_id ?? "—"}</TableCell>
                <TableCell>{b.book_number ?? "—"}</TableCell>
                <TableCell>{b.release_date ?? "—"}</TableCell>

                <TableCell>
                  <button
                    onClick={() => toggleRead(b)}
                    className="px-2 py-1 bg-blue-600 text-white rounded"
                  >
                    {b.is_read ? "Mark Unread" : "Mark Read"}
                  </button>
                </TableCell>

                <TableCell>
                  <button
                    onClick={() => deleteBook(b.id)}
                    className="px-2 py-1 bg-red-600 text-white rounded"
                  >
                    Delete
                  </button>
                </TableCell>
              </TableRow>
            ))}
        </TableBody>
      </Table>
    </div>
  );
}
