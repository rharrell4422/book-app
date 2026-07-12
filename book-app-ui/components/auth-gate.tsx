"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/use-toast";
import { useAuth } from "@/lib/auth-context";
import { fetchApiWithFallback } from "@/lib/api-client";
import { setNotifyListener } from "@/lib/notify";

function NotifyBridge() {
  const { toast } = useToast();

  useEffect(() => {
    setNotifyListener(toast);
    return () => setNotifyListener(null);
  }, [toast]);

  return null;
}

function LoginScreen() {
  const { login } = useAuth();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    const result = await login(password);
    setSubmitting(false);
    if (!result.ok) {
      setError(result.error || "Login failed");
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 px-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Sign in to your library</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="owner-password">Password</Label>
              <Input
                id="owner-password"
                type="password"
                autoFocus
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="Enter your password"
              />
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <Button type="submit" disabled={submitting || !password}>
              {submitting ? "Signing in..." : "Sign in"}
            </Button>
            <p className="text-xs text-muted-foreground">
              Have a shared view-only link instead? Open it directly -- it
              signs you in automatically as a read-only viewer.
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}

function ShareLinkButton() {
  const { toast } = useToast();
  const [loading, setLoading] = useState(false);

  async function handleShare() {
    setLoading(true);
    try {
      const response = await fetchApiWithFallback("/auth/share_link");
      const data = await response.json();
      if (!data.enabled || !data.share_token) {
        toast({
          title: "Sharing isn't configured",
          description: "Set SHARE_VIEW_TOKEN on the server to enable read-only links.",
        });
        return;
      }
      const url = `${window.location.origin}${window.location.pathname}?share=${data.share_token}`;
      await navigator.clipboard.writeText(url);
      toast({
        title: "Read-only link copied",
        description: "Anyone with this link can view your library but can't change anything.",
      });
    } catch {
      toast({ title: "Couldn't get the share link", description: "Please try again." });
    } finally {
      setLoading(false);
    }
  }

  return (
    <Button variant="ghost" size="sm" onClick={handleShare} disabled={loading}>
      {loading ? "Copying..." : "Copy read-only share link"}
    </Button>
  );
}

function TopBar() {
  const { role, logout } = useAuth();

  if (role === "viewer") {
    return (
      <div className="flex items-center justify-center gap-2 bg-amber-100 px-4 py-2 text-center text-sm text-amber-900 dark:bg-amber-900/30 dark:text-amber-200">
        <span>
          You&rsquo;re viewing a shared, read-only copy of this library. Nothing you do here will be saved.
        </span>
      </div>
    );
  }

  if (role === "owner") {
    return (
      <div className="flex items-center justify-end gap-2 border-b bg-muted/40 px-4 py-1.5">
        <ShareLinkButton />
        <Button variant="ghost" size="sm" onClick={logout}>
          Sign out
        </Button>
      </div>
    );
  }

  return null;
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  const { role, ready } = useAuth();

  if (!ready) {
    return null;
  }

  if (!role) {
    return (
      <>
        <NotifyBridge />
        <LoginScreen />
      </>
    );
  }

  return (
    <div className="flex min-h-full flex-col">
      <NotifyBridge />
      <TopBar />
      <div className="flex-1">{children}</div>
    </div>
  );
}
