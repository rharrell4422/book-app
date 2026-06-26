"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/components/ui/use-toast";
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
  const [series, setSeries] = useState([]);
  const [loadingId, setLoadingId] = useState<number | null>(null);
  const [message, setMessage] = useState("");

  async function fetchSeries() {
    try {
      const response = await fetch("http://localhost:8000/series/", {
        cache: "no-store",
      });
      const data = await response.json();
      setSeries(data);
    } catch (error) {
      console.error("Error fetching series:", error);
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
        `Checked series ${seriesId}. Next upcoming: ${
          data.next_upcoming_book_number ?? "None"
        }`
      );

      toast({
        title: "Check complete",
        description: `Series ${seriesId} refreshed successfully.`,
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
      const response = await fetch(
        `http://localhost:8000/series/${seriesId}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ check_url: newUrl }),
        }
      );

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
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Series List</h1>

      {message && (
        <div className="mb-4 p-2 bg-blue-100 border border-blue-300 rounded">
          {message}
        </div>
      )}

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>ID</TableHead>
            <TableHead>Name</TableHead>
            <TableHead>Author</TableHead>
            <TableHead>Check URL</TableHead>
            <TableHead>Next Unread</TableHead>
            <TableHead>Next Upcoming</TableHead>
            <TableHead>Missing</TableHead>
            <TableHead>Total</TableHead>
            <TableHead>Last Checked</TableHead>
            <TableHead>Books</TableHead>
            <TableHead>Check Now</TableHead>
          </TableRow>
        </TableHeader>

        <TableBody>
          {Array.isArray(series) &&
            series.map((s: any) => (
              <TableRow key={s.id}>
                <TableCell>{s.id}</TableCell>
                <TableCell>{s.name}</TableCell>
                <TableCell>{s.author}</TableCell>

                <TableCell>
                  <div className="flex flex-col gap-1">
                    <span className="truncate max-w-[200px]">
                      {s.check_url ?? "—"}
                    </span>
                    <button
                      onClick={() => handleEditUrl(s.id, s.check_url)}
                      className="text-xs text-blue-600 underline"
                    >
                      Edit URL
                    </button>
                  </div>
                </TableCell>

                <TableCell>{s.next_unread_book_number ?? "—"}</TableCell>
                <TableCell>{s.next_upcoming_book_number ?? "—"}</TableCell>
                <TableCell>{s.missing_books ?? "—"}</TableCell>
                <TableCell>{s.series_total_books_final ?? "—"}</TableCell>
                <TableCell>{s.last_checked ?? "—"}</TableCell>

                <TableCell>
                  <a
                    href={`/books?series_id=${s.id}`}
                    className="text-blue-600 underline"
                  >
                    View Books
                  </a>
                </TableCell>

                <TableCell>
                  <button
                    onClick={() => handleCheckNow(s.id)}
                    disabled={loadingId === s.id}
                    className="px-3 py-1 bg-green-600 text-white rounded disabled:bg-gray-400"
                  >
                    {loadingId === s.id ? "Checking…" : "Check Now"}
                  </button>
                </TableCell>
              </TableRow>
            ))}
        </TableBody>
      </Table>
    </div>
  );
}
