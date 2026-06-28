"use client";

import { useEffect, useState } from "react";
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

export default function SeriesPage() {
  const { toast } = useToast();
  const [series, setSeries] = useState<any[]>([]);
  const [loadingId, setLoadingId] = useState<number | null>(null);
  const [message, setMessage] = useState("");

  const totalBooks = series.reduce(
    (sum, s) => sum + (s.series_total_books_final ?? 0),
    0
  );

  async function fetchSeries() {
    try {
      const response = await fetch("http://localhost:8000/series/", {
        cache: "no-store",
      });
      const data = await response.json();
      setSeries(data);
    } catch (error) {
      console.error("Error fetching series:", error);
      setMessage("Unable to load series right now.");
    }
  }

  useEffect(() => {
    fetchSeries();
  }, []);

  async function handleCheckNow(seriesId: number) {
    setLoadingId(seriesId);
    setMessage("");

    try {
      const response = await fetch(
        `http://localhost:8000/series/${seriesId}/check`,
        { method: "POST" }
      );
      const data = await response.json();

      setMessage(
        `Series ${seriesId} refreshed. Next upcoming: ${
          data.next_upcoming_book_number ?? "None"
        }.`
      );

      toast({
        title: "Series refreshed",
        description: `Series ${seriesId} has been updated.`,
      });

      fetchSeries();
    } catch (error) {
      console.error("Error checking series:", error);
      setMessage("Error checking series.");
    }

    setLoadingId(null);
  }

  async function handleEditUrl(seriesId: number, currentUrl: string) {
    const newUrl = prompt("Enter new check URL:", currentUrl || "");
    if (newUrl === null) return;

    try {
      const response = await fetch(`http://localhost:8000/series/${seriesId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ check_url: newUrl }),
      });

      if (response.ok) {
        toast({
          title: "URL updated",
          description: `Series ${seriesId} check URL saved.`,
        });
        fetchSeries();
      } else {
        toast({
          title: "Error",
          description: "Failed to update URL.",
        });
      }
    } catch (error) {
      console.error("Error updating URL:", error);
      toast({
        title: "Error",
        description: "Network or server issue while updating URL.",
      });
    }
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div className="space-y-2">
          <p className="text-sm uppercase tracking-[0.2em] text-muted-foreground">
            Series library
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-3xl font-bold">Series</h1>
            <span className="rounded-full bg-muted px-3 py-1 text-xs uppercase tracking-[0.2em] text-muted-foreground">
              {series.length} tracked
            </span>
          </div>
          <p className="max-w-2xl text-sm leading-6 text-muted-foreground">
            Browse your tracked series, update check URLs, and refresh status for each series.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <Link href="/books">
            <Button variant="outline">View Library</Button>
          </Link>
          <Link href="/series/[seriesId]">
            <Button variant="secondary">Series detail</Button>
          </Link>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Series tracked</p>
          <p className="text-2xl font-semibold">{series.length}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Books tracked</p>
          <p className="text-2xl font-semibold">{totalBooks}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Ready to refresh</p>
          <p className="text-2xl font-semibold">
            {loadingId ? "Refreshing…" : "Awaiting action"}
          </p>
        </div>
      </div>

      {message && (
        <div className="rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-900">
          {message}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border bg-card/80">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>Name</TableHead>
              <TableHead>Author</TableHead>
              <TableHead>Next unread</TableHead>
              <TableHead>Next upcoming</TableHead>
              <TableHead>Total</TableHead>
              <TableHead>Last checked</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>

          <TableBody>
            {series.map((s) => (
              <TableRow key={s.id}>
                <TableCell>{s.id}</TableCell>
                <TableCell>{s.name}</TableCell>
                <TableCell>{s.author || "—"}</TableCell>
                <TableCell>{s.next_unread_book_number ?? "—"}</TableCell>
                <TableCell>{s.next_upcoming_book_number ?? "—"}</TableCell>
                <TableCell>{s.series_total_books_final ?? "—"}</TableCell>
                <TableCell>{s.last_checked ?? "—"}</TableCell>
                <TableCell className="space-x-2 whitespace-nowrap">
                  <Link href={`/books?series_id=${s.id}`}>
                    <Button variant="ghost" size="sm">
                      View books
                    </Button>
                  </Link>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleEditUrl(s.id, s.check_url)}
                  >
                    Edit URL
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleCheckNow(s.id)}
                    disabled={loadingId === s.id}
                  >
                    {loadingId === s.id ? "Checking…" : "Refresh"}
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
