"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useToast } from "@/components/ui/use-toast";
import { fetchApiWithFallback } from "@/lib/api-client";
import { useProfile } from "@/lib/profile-context";

const AUTO_DISCOVERY_COOLDOWN_MS = 7 * 24 * 60 * 60 * 1000;
const STATUS_POLL_INTERVAL_MS = 2000;
const STATUS_MAX_POLLS = 900; // 30 minutes

type AutoDiscoveryRunResponse = {
  status: "started" | "running" | "cooldown";
  job_id?: string | null;
  total?: number | null;
  completed?: number | null;
  remaining_seconds?: number | null;
  message?: string | null;
};

type AutoDiscoverySeriesResult = {
  series_id: number;
  series_name: string;
  outcome: "checked" | "skipped_already_running";
  new_books_found?: number;
  discovery_delta_count?: number;
};

type AutoDiscoveryStatusResponse = {
  status: "idle" | "running" | "completed" | "interrupted";
  job_id?: string | null;
  total?: number | null;
  completed?: number | null;
  updated_at?: string | null;
  results?: AutoDiscoverySeriesResult[] | null;
  new_books_found?: number | null;
  // Job-level total across every series swept this run (new inserts +
  // upcoming->available transitions) -- the same number each series'
  // durable notification row uses, so this popup's count and the
  // Notifications view can never disagree. See services/auto_discovery.py.
  discovery_delta_count?: number | null;
  message?: string | null;
};

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function formatCooldownRemaining(remainingMs: number): string {
  if (remainingMs <= 0) return "";
  const days = Math.floor(remainingMs / (24 * 60 * 60 * 1000));
  const hours = Math.floor((remainingMs % (24 * 60 * 60 * 1000)) / (60 * 60 * 1000));
  if (days > 0) return `${days} day${days === 1 ? "" : "s"}${hours > 0 ? ` ${hours}h` : ""}`;
  if (hours > 0) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return "less than an hour";
}

export default function SettingsPage() {
  const { activeProfile, refreshProfiles } = useProfile();
  const { toast } = useToast();

  const [running, setRunning] = useState(false);
  const [jobStatus, setJobStatus] = useState<AutoDiscoveryStatusResponse | null>(null);
  // "Current time" as state (updated from a timer callback, not read
  // directly during render) -- Date.now() is impure, so it's only ever
  // called from inside the interval callback below, never from the
  // render/useMemo body itself.
  const [now, setNow] = useState<number | null>(null);
  const pollCancelRef = useRef(false);

  useEffect(() => {
    const updateNow = () => setNow(Date.now());
    const rafId = window.requestAnimationFrame(updateNow);
    const interval = window.setInterval(updateNow, 60_000);
    return () => {
      window.cancelAnimationFrame(rafId);
      window.clearInterval(interval);
    };
  }, []);

  const cooldownRemainingMs = useMemo(() => {
    const lastRunAt = activeProfile?.last_full_discovery_run_at;
    if (!lastRunAt || now === null) return 0;
    const elapsed = now - new Date(lastRunAt).getTime();
    return Math.max(0, AUTO_DISCOVERY_COOLDOWN_MS - elapsed);
  }, [activeProfile?.last_full_discovery_run_at, now]);

  useEffect(() => {
    return () => {
      pollCancelRef.current = true;
    };
  }, []);

  async function pollJobStatus(jobId: string) {
    let pollCount = 0;
    let status: AutoDiscoveryStatusResponse = { status: "running", job_id: jobId };

    while (status.status === "running") {
      if (pollCancelRef.current) return;
      if (pollCount >= STATUS_MAX_POLLS) {
        setJobStatus({
          status: "running",
          job_id: jobId,
          message: "This run is taking a while -- it's still going in the background. Check back later.",
        });
        return;
      }

      await delay(STATUS_POLL_INTERVAL_MS);
      if (pollCancelRef.current) return;

      const response = await fetchApiWithFallback(`/discovery/auto_run_mvp/status?job_id=${encodeURIComponent(jobId)}`, {
        cache: "no-store",
      });
      status = await response.json();
      pollCount += 1;
      setJobStatus(status);
    }

    if (status.status === "completed") {
      // Ephemeral popup: session-only, driven directly by this response --
      // never a query against the notifications table. Multi-series
      // aggregate wording (no single series to name), using the same
      // discovery_delta_count each swept series' durable notification row
      // was written with, so this toast and the Notifications view can
      // never disagree.
      const discoveryDeltaCount = status.discovery_delta_count ?? 0;
      toast({
        title: "Full Auto Discovery complete",
        description:
          discoveryDeltaCount > 0
            ? `Checked ${status.total ?? 0} series and found ${discoveryDeltaCount} new book${discoveryDeltaCount === 1 ? "" : "s"}.`
            : `Checked ${status.total ?? 0} series. No new books this time.`,
      });
      await refreshProfiles();
    } else if (status.status === "interrupted") {
      toast({
        title: "Run may have been interrupted",
        description: "Check series for partial results or try again.",
      });
    }
  }

  async function handleRunFullAutoDiscovery() {
    setRunning(true);
    setJobStatus(null);
    pollCancelRef.current = false;

    try {
      const response = await fetchApiWithFallback("/discovery/auto_run_mvp", { method: "POST" });
      const data: AutoDiscoveryRunResponse = await response.json();

      if (data.status === "cooldown") {
        toast({
          title: "Full Auto Discovery is on cooldown",
          description: data.message || "Please try again later.",
        });
        await refreshProfiles();
        setRunning(false);
        return;
      }

      if (!data.job_id) {
        throw new Error("No job id returned.");
      }

      setJobStatus({ status: "running", job_id: data.job_id, total: data.total, completed: data.completed ?? 0 });
      await pollJobStatus(data.job_id);
    } catch (error) {
      toast({
        title: "Couldn't start Full Auto Discovery",
        description: error instanceof Error ? error.message : "Please try again.",
      });
    } finally {
      setRunning(false);
    }
  }

  const cooldownActive = cooldownRemainingMs > 0;
  const disabled = running || cooldownActive || (jobStatus?.status === "running");

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground">Settings</p>
          <h1 className="text-2xl font-bold">Auto Discovery</h1>
        </div>
        <Link href="/books">
          <Button variant="outline" size="sm">
            Back to Library
          </Button>
        </Link>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Full Auto Discovery (MVP)</CardTitle>
          <CardDescription>
            Sweeps every caught-up, finished-with-no-loose-ends series in your library for new books, in one batch.
            Skips series with unread/upcoming books, missing volumes, unverified metadata, or no confirmed author --
            those are better served by Check Now. Rate-limited to once every 7 days.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <Button onClick={handleRunFullAutoDiscovery} disabled={disabled}>
            {running || jobStatus?.status === "running" ? "Running..." : "Run Full Auto Discovery"}
          </Button>

          {cooldownActive ? (
            <p className="text-sm text-muted-foreground">
              Can be run again in about {formatCooldownRemaining(cooldownRemainingMs)}.
            </p>
          ) : null}

          {jobStatus ? (
            <div className="rounded-md border px-3 py-2 text-sm">
              {jobStatus.status === "running" ? (
                <p>
                  Checking {jobStatus.completed ?? 0}/{jobStatus.total ?? 0} eligible series...
                  {jobStatus.message ? ` ${jobStatus.message}` : ""}
                </p>
              ) : jobStatus.status === "completed" ? (
                <div className="flex flex-col gap-1">
                  <p className="font-medium">
                    Checked {jobStatus.total ?? 0} series -- found {jobStatus.discovery_delta_count ?? 0} new book
                    {jobStatus.discovery_delta_count === 1 ? "" : "s"}.
                  </p>
                  {jobStatus.message ? <p className="text-xs text-destructive">{jobStatus.message}</p> : null}
                </div>
              ) : jobStatus.status === "interrupted" ? (
                <p>This run may have been interrupted -- check series for partial results or try again.</p>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
