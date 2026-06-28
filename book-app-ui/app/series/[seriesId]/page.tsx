"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString();
}

function getBookStatus(book: any) {
  return book.read_status ?? (book.is_read ? "read" : "upcoming");
}

function getBookDate(book: any) {
  const status = getBookStatus(book);
  return status === "upcoming" ? book.release_date || book.read_date : book.read_date || book.release_date;
}

export default function SeriesDetailPage() {
  const params = useParams();
  const seriesId = params.seriesId as string;
  const [series, setSeries] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [summaryLoadingId, setSummaryLoadingId] = useState<number | null>(null);
  const [missingSuggestions, setMissingSuggestions] = useState<Record<string, any[]>>({});
  const [missingSuggestionLoading, setMissingSuggestionLoading] = useState<string | null>(null);

  useEffect(() => {
    async function fetchSeries() {
      setLoading(true);
      setError(null);

      try {
        const response = await fetch(`http://localhost:8000/series/${seriesId}`, {
          cache: "no-store",
        });

        if (!response.ok) {
          throw new Error(`Failed to load series (${response.status})`);
        }

        const data = await response.json();
        setSeries(data);

        if (data.missing_books?.length) {
          const suggestions: Record<string, any[]> = {};
          await Promise.all(
            data.missing_books.map(async (order: string) => {
              suggestions[order] = await fetchSuggestionForMissingBook(order);
            })
          );
          setMissingSuggestions(suggestions);
        }
      } catch (error) {
        setError("Unable to load this series right now.");
        console.error("Error fetching series:", error);
      } finally {
        setLoading(false);
      }
    }

    if (seriesId) {
      fetchSeries();
    }
  }, [seriesId]);

  if (loading) {
    return <div className="p-6">Loading series...</div>;
  }

  if (error) {
    return <div className="p-6 text-red-600">{error}</div>;
  }

  if (!series) {
    return <div className="p-6">Series not found.</div>;
  }

  const books: any[] = Array.isArray(series.books) ? series.books : [];
  const missingOrders: string[] = Array.isArray(series.missing_books)
    ? series.missing_books
    : [];
  const totalBooks = series.total_books ?? books.length;
  const readCount = books.filter((book) => book.is_read).length;
  const upcomingCount = books.filter((book) => getBookStatus(book) === "upcoming").length;
  const unreadCount = books.filter((book) => !book.is_read).length;
  const displayAuthor = series.author || books.find((book) => book.author)?.author || "Unknown author";

  function buildSearchUrl(query: string) {
    const encoded = encodeURIComponent(query);
    return `https://www.goodreads.com/search?q=${encoded}`;
  }

  function handleOpenSearch(query: string) {
    window.open(buildSearchUrl(query), "_blank");
  }

  async function handleFetchSummary(bookId: number, title: string, author?: string | null) {
    setSummaryLoadingId(bookId);
    try {
      const response = await fetch(`http://localhost:8000/books/${bookId}/summary`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(`Failed to fetch summary (${response.status})`);
      }

      const data = await response.json();
      const updatedBook = data.book;
      setSeries((prev: any) => ({
        ...prev,
        books: prev.books.map((book: any) =>
          book.id === updatedBook.id ? updatedBook : book
        ),
      }));
    } catch (err) {
      console.error(err);
      alert("Unable to fetch a summary for this book right now.");
    } finally {
      setSummaryLoadingId(null);
    }
  }

  async function fetchSuggestionForMissingBook(bookNumber: string) {
    try {
      const params = new URLSearchParams();
      params.set("series_name", series.name);
      params.set("book_number", bookNumber);
      const suggestAuthor = series.author || books.find((book) => book.author)?.author;
      if (suggestAuthor) {
        params.set("author", suggestAuthor);
      }

      const response = await fetch(`http://localhost:8000/books/suggest?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`Failed to lookup suggestions (${response.status})`);
      }

      const data = await response.json();
      return data.results || [];
    } catch (err) {
      console.error(err);
      return [];
    }
  }

  async function handleSuggestMissingBook(bookNumber: string) {
    setMissingSuggestionLoading(bookNumber);
    try {
      const results = await fetchSuggestionForMissingBook(bookNumber);
      setMissingSuggestions((prev) => ({
        ...prev,
        [bookNumber]: results,
      }));
    } catch (err) {
      console.error(err);
      alert("Unable to suggest a title for this missing book right now.");
    } finally {
      setMissingSuggestionLoading(null);
    }
  }

  async function handleAddSuggestion(bookNumber: string, suggestion: any) {
    try {
      const response = await fetch("http://localhost:8000/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: suggestion.title,
          author: suggestion.author || series.author || "Unknown author",
          series_id: Number(series.id),
          series_order: Number(bookNumber),
          book_number: Number(bookNumber),
          read_status: "unread",
          is_read: false,
          publication_date: suggestion.year ? `${suggestion.year}-01-01` : undefined,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to add suggested book (${response.status})`);
      }

      const newBook = await response.json();
      setSeries((prev: any) => ({
        ...prev,
        books: [...prev.books, newBook],
      }));
      alert(`Added suggestion '${suggestion.title}' for book ${bookNumber}. Refresh page to reorder.`);
    } catch (err) {
      console.error(err);
      alert("Unable to add the suggested book.");
    }
  }

  async function handleAddMissingBook(bookNumber: string) {
    const title = prompt(`Title for book ${bookNumber}:`, `Book ${bookNumber}`);
    if (!title) {
      return;
    }

    try {
      const response = await fetch("http://localhost:8000/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          author: series.author || "Unknown author",
          series_id: Number(series.id),
          series_order: Number(bookNumber),
          book_number: Number(bookNumber),
          read_status: "unread",
          is_read: false,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to add book ${bookNumber}`);
      }

      const updatedBook = await response.json();
      setSeries((prev: any) => ({
        ...prev,
        books: [...books, updatedBook],
      }));
      alert(`Added missing book ${bookNumber}. Refresh the page to see it in series order.`);
    } catch (error) {
      console.error(error);
      alert("Could not add the missing book. Check the console for details.");
    }
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div className="space-y-3">
          <p className="text-sm uppercase tracking-[0.2em] text-muted-foreground">Series detail</p>
          <div>
            <h1 className="text-3xl font-bold">{series.name}</h1>
            <p className="text-sm text-muted-foreground">{series.author || "Unknown author"}</p>
          </div>
          {series.description && (
            <p className="max-w-3xl text-sm leading-6 text-muted-foreground">{series.description}</p>
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          <Link href="/books">
            <Button variant="outline">Back to Library</Button>
          </Link>
          <Link href="/series">
            <Button variant="secondary">View all series</Button>
          </Link>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Total books</p>
          <p className="text-2xl font-semibold">{totalBooks}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Read</p>
          <p className="text-2xl font-semibold">{readCount}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Unread</p>
          <p className="text-2xl font-semibold">{unreadCount}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Upcoming</p>
          <p className="text-2xl font-semibold">{upcomingCount}</p>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 text-sm text-muted-foreground">
        <div>Series status: {series.series_status || "Unknown"}</div>
        <div>Next unread: {series.next_unread_book_number ?? "—"}</div>
        <div>Next upcoming: {series.next_upcoming_book_number ?? "—"}</div>
        <div>Total missing: {missingOrders.length}</div>
      </div>

      {missingOrders.length > 0 && (
        <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="text-sm font-semibold text-yellow-900">Missing books detected</p>
              <p className="text-sm text-muted-foreground">
                These books are not in your library yet. Add them if you want to track them here.
              </p>
            </div>
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {missingOrders.map((order) => (
              <div key={order} className="rounded-lg border bg-white p-3 shadow-sm">
                <p className="text-sm text-muted-foreground">Missing book</p>
                <p className="text-xl font-semibold">#{order}</p>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleOpenSearch(`${series.name} ${order}`)}
                  >
                    Search Goodreads
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleSuggestMissingBook(order)}
                    disabled={missingSuggestionLoading === order}
                  >
                    {missingSuggestionLoading === order ? "Finding…" : "Suggest title"}
                  </Button>
                </div>
                {missingSuggestions[order] && missingSuggestions[order].length > 0 ? (
                  <div className="mt-3 space-y-2 rounded border bg-slate-50 p-3 text-sm">
                    {missingSuggestions[order].map((suggestion, idx) => (
                      <div key={idx} className="space-y-1">
                        <div className="font-medium">{suggestion.title}</div>
                        <div className="text-xs text-muted-foreground">
                          {suggestion.author || "Unknown author"}
                          {suggestion.year ? ` • ${suggestion.year}` : ""}
                        </div>
                        <div className="flex flex-wrap gap-2">
                          <Button
                            variant="outline"
                            size="xs"
                            onClick={() => handleAddSuggestion(order, suggestion)}
                          >
                            Add suggestion
                          </Button>
                          {suggestion.source_url ? (
                            <a
                              href={suggestion.source_url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-xs text-blue-600 underline"
                            >
                              View on OpenLibrary
                            </a>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : missingSuggestions[order] ? (
                  <p className="mt-3 text-sm text-muted-foreground">No suggestions found.</p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      )}

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Title</TableHead>
            <TableHead>Author</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Date</TableHead>
            <TableHead>Book #</TableHead>
            <TableHead>Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {books.map((book) => {
            const status = getBookStatus(book);
            const displayDate = getBookDate(book);
            const summary = book.auto_summary;
            return (
              <TableRow key={book.id}>
                <TableCell>
                  <div>{book.title}</div>
                  {summary ? (
                    <p className="text-xs text-muted-foreground line-clamp-2">
                      {summary}
                    </p>
                  ) : null}
                </TableCell>
                <TableCell>{book.author || "—"}</TableCell>
                <TableCell className="capitalize">{status}</TableCell>
                <TableCell>{formatDate(displayDate)}</TableCell>
                <TableCell>{book.book_number ?? "—"}</TableCell>
                <TableCell className="space-x-2 whitespace-nowrap">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      handleOpenSearch(
                        `${book.title} ${book.author || ""}`.trim()
                      )
                    }
                  >
                    Search
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleFetchSummary(book.id, book.title, book.author)}
                    disabled={summaryLoadingId === book.id}
                  >
                    {summary ? "Refresh summary" : "Fetch summary"}
                  </Button>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
