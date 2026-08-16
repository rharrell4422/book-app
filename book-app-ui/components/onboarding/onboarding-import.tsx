"use client";

import { useCallback, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import Spinner from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useToast } from "@/components/ui/use-toast";
import { fetchApiWithFallback } from "@/lib/api-client";
import type { Profile } from "@/lib/profile-context";

type PreviewRow = {
  row_number: number;
  title: string | null;
  author: string | null;
  series_name: string | null;
  needs_series_confirmation: boolean;
};

type ValidationWarning = {
  row_number: number;
  title?: string | null;
  errors: string[];
};

type PreviewResult = {
  row_count: number;
  unknown_headers: string[];
  sample_rows: PreviewRow[];
  validation_warnings: ValidationWarning[];
  valid_row_count: number;
  series_confirmation_expected_count: number;
};

type ImportSummary = {
  imported_count: number;
  confirmation_required_count: number;
  failed_count: number;
  failed_rows: { row_number: number; title: string | null; error: string }[];
};

type ConfirmationItem = {
  book_id: number;
  title: string;
  author: string;
  candidate_series_name: string | null;
  reason: string | null;
};

type Step = "upload" | "previewing" | "preview" | "importing" | "resolve" | "done";

const ACCEPTED_EXTENSIONS = ".csv,.xlsx,.xls";

export function OnboardingImport({
  profile,
  onComplete,
  onSkip,
}: {
  profile: Profile;
  onComplete: () => void;
  onSkip: () => void;
}) {
  const { toast } = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [step, setStep] = useState<Step>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [summary, setSummary] = useState<ImportSummary | null>(null);
  const [confirmations, setConfirmations] = useState<ConfirmationItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const resetToUpload = useCallback(() => {
    setFile(null);
    setPreview(null);
    setSummary(null);
    setConfirmations([]);
    setError(null);
    setStep("upload");
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, []);

  const handleFileChange = useCallback(async (event: React.ChangeEvent<HTMLInputElement>) => {
    const selected = event.target.files?.[0] ?? null;
    setError(null);
    if (!selected) return;

    setFile(selected);
    setStep("previewing");
    setBusy(true);
    try {
      const formData = new FormData();
      formData.append("file", selected);
      const response = await fetchApiWithFallback("/import/preview", { method: "POST", body: formData });
      const data: PreviewResult = await response.json();
      setPreview(data);
      setStep("preview");
    } catch (err) {
      setError(
        err instanceof Error
          ? `Couldn't read that file: ${err.message}`
          : "Couldn't read that file. Make sure it's a .csv, .xlsx, or .xls export."
      );
      setStep("upload");
    } finally {
      setBusy(false);
    }
  }, []);

  const handleConfirmImport = useCallback(async () => {
    if (!file) return;
    setStep("importing");
    setBusy(true);
    setError(null);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const response = await fetchApiWithFallback("/import/upload", { method: "POST", body: formData });
      const data = await response.json();
      const importSummary: ImportSummary = data.import_summary;
      setSummary(importSummary);

      if (importSummary.confirmation_required_count > 0) {
        const queueResponse = await fetchApiWithFallback("/import/series_confirmations");
        const queueData = await queueResponse.json();
        setConfirmations(queueData.items || []);
        setStep("resolve");
      } else {
        setStep("done");
      }
    } catch (err) {
      setError(err instanceof Error ? `Import failed: ${err.message}` : "Import failed. Please try again.");
      setStep("preview");
    } finally {
      setBusy(false);
    }
  }, [file]);

  const handleStartOver = useCallback(async () => {
    setBusy(true);
    try {
      // Safe here specifically because this profile has no real data yet
      // (that's why onboarding is showing) -- clears out whatever the
      // failed/unwanted attempt already wrote, so retrying doesn't double
      // anything.
      await fetchApiWithFallback("/import/reset_profile", { method: "POST" });
    } catch {
      // Best-effort -- even if the reset call fails, let them retry the upload.
    } finally {
      setBusy(false);
      resetToUpload();
    }
  }, [resetToUpload]);

  const resolveDecision = useCallback(
    async (bookId: number, decision: "yes" | "no") => {
      setBusy(true);
      try {
        await fetchApiWithFallback("/import/series_confirmations/resolve", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decisions: [{ book_id: bookId, decision }] }),
        });
        setConfirmations((prev) => {
          const next = prev.filter((item) => item.book_id !== bookId);
          if (next.length === 0) {
            setStep("done");
          }
          return next;
        });
      } catch {
        toast({ title: "Couldn't save that decision", description: "Please try again." });
      } finally {
        setBusy(false);
      }
    },
    [toast]
  );

  return (
    <div className="flex min-h-[70vh] items-center justify-center px-4 py-10">
      <Card className="w-full max-w-2xl">
        <CardHeader>
          <CardTitle>Import {profile.display_name}&rsquo;s library</CardTitle>
          <p className="text-sm text-muted-foreground">
            Upload a spreadsheet (CSV, Excel, or a Google Sheets export) and we&rsquo;ll build this library
            automatically.
          </p>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          {(step === "upload" || step === "previewing") && (
            <div className="flex flex-col gap-4">
              <label
                htmlFor="onboarding-file"
                className="flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-input bg-muted/30 px-6 py-10 text-center text-sm text-muted-foreground transition-colors hover:bg-muted/50"
              >
                {step === "previewing" ? (
                  <>
                    <Spinner />
                    <span>Reading {file?.name}…</span>
                  </>
                ) : (
                  <>
                    <span className="font-medium text-foreground">Click to choose a file</span>
                    <span>CSV, XLSX, or XLS &bull; up to 25 MB</span>
                  </>
                )}
              </label>
              <input
                ref={fileInputRef}
                id="onboarding-file"
                type="file"
                accept={ACCEPTED_EXTENSIONS}
                className="hidden"
                disabled={step === "previewing"}
                onChange={handleFileChange}
              />
              <div className="flex justify-center">
                <Button variant="ghost" size="sm" onClick={onSkip}>
                  Skip for now, I&rsquo;ll add books manually
                </Button>
              </div>
            </div>
          )}

          {step === "preview" && preview && (
            <div className="flex flex-col gap-4">
              <div className="flex flex-wrap gap-4 text-sm">
                <span>
                  <strong>{preview.row_count}</strong> row{preview.row_count === 1 ? "" : "s"} found
                </span>
                {preview.series_confirmation_expected_count > 0 && (
                  <span className="text-muted-foreground">
                    {preview.series_confirmation_expected_count} will need a quick series check after import
                  </span>
                )}
                {preview.validation_warnings.length > 0 && (
                  <span className="text-amber-600 dark:text-amber-400">
                    {preview.validation_warnings.length} row{preview.validation_warnings.length === 1 ? "" : "s"} will
                    be skipped (missing title/author)
                  </span>
                )}
              </div>

              {preview.unknown_headers.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  Columns we didn&rsquo;t recognize (kept but not used): {preview.unknown_headers.join(", ")}
                </p>
              )}

              {preview.sample_rows.length > 0 && (
                <div className="max-h-64 overflow-y-auto rounded-md border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Title</TableHead>
                        <TableHead>Author</TableHead>
                        <TableHead>Series</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {preview.sample_rows.map((row) => (
                        <TableRow key={row.row_number}>
                          <TableCell>{row.title || "(untitled)"}</TableCell>
                          <TableCell>{row.author || "—"}</TableCell>
                          <TableCell>{row.series_name || "—"}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}

              <div className="flex flex-wrap items-center justify-between gap-2">
                <Button variant="ghost" size="sm" onClick={resetToUpload} disabled={busy}>
                  Choose a different file
                </Button>
                <Button onClick={handleConfirmImport} disabled={busy || preview.valid_row_count === 0}>
                  Import {preview.valid_row_count} book{preview.valid_row_count === 1 ? "" : "s"}
                </Button>
              </div>
            </div>
          )}

          {step === "importing" && (
            <div className="flex flex-col items-center justify-center gap-3 py-10 text-sm text-muted-foreground">
              <Spinner />
              <span>Building {profile.display_name}&rsquo;s library…</span>
            </div>
          )}

          {step === "resolve" && (
            <div className="flex flex-col gap-4">
              <p className="text-sm text-muted-foreground">
                {confirmations.length} book{confirmations.length === 1 ? "" : "s"} need a quick check -- does this
                book belong to the series we guessed?
              </p>
              <div className="flex max-h-80 flex-col gap-2 overflow-y-auto">
                {confirmations.map((item) => (
                  <div
                    key={item.book_id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2 text-sm"
                  >
                    <div>
                      <div className="font-medium">{item.title}</div>
                      <div className="text-muted-foreground">
                        {item.author} &bull; candidate series: {item.candidate_series_name || "(none)"}
                      </div>
                    </div>
                    <div className="flex gap-1.5">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() => resolveDecision(item.book_id, "yes")}
                      >
                        Yes
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => resolveDecision(item.book_id, "no")}
                      >
                        No
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">
                You can always fix series links later from the library, so it&rsquo;s fine to finish this now.
              </p>
              <div className="flex justify-end">
                <Button variant="ghost" size="sm" onClick={() => setStep("done")}>
                  Finish the rest later
                </Button>
              </div>
            </div>
          )}

          {step === "done" && summary && (
            <div className="flex flex-col gap-4">
              <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-3 text-sm">
                Imported <strong>{summary.imported_count}</strong> book{summary.imported_count === 1 ? "" : "s"} into{" "}
                {profile.display_name}&rsquo;s library.
              </div>
              {summary.failed_count > 0 && (
                <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm">
                  <p className="font-medium">
                    {summary.failed_count} row{summary.failed_count === 1 ? "" : "s"} skipped:
                  </p>
                  <ul className="mt-1 list-inside list-disc text-xs text-muted-foreground">
                    {summary.failed_rows.map((row) => (
                      <li key={row.row_number}>
                        Row {row.row_number}
                        {row.title ? ` (${row.title})` : ""}: {row.error}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={handleStartOver} disabled={busy}>
                  Start over
                </Button>
                <Button onClick={onComplete}>Go to your library</Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
