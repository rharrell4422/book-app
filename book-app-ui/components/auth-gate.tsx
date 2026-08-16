"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { PencilIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/components/ui/use-toast";
import { useAuth } from "@/lib/auth-context";
import { useProfile } from "@/lib/profile-context";
import { fetchApiWithFallback } from "@/lib/api-client";
import { setNotifyListener } from "@/lib/notify";
import { OnboardingImport } from "@/components/onboarding/onboarding-import";

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
  const { profileId } = useProfile();
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
      // Pinning the current profile into the link itself (rather than
      // leaving it to whatever the viewer's browser happens to default to)
      // means "share my library" and "share my daughter's library" are two
      // different, unambiguous links -- see profile-context.tsx, which
      // reads this param independently of the `share` token.
      const params = new URLSearchParams({ share: data.share_token });
      if (profileId) {
        params.set("profile", profileId);
      }
      const url = `${window.location.origin}${window.location.pathname}?${params.toString()}`;
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

const ADD_PROFILE_SENTINEL = "__add_profile__";

function AddProfileDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { toast } = useToast();
  const { createProfile } = useProfile();
  const [displayName, setDisplayName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function slugify(value: string) {
    return value
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const id = slugify(displayName);
    if (!id) {
      setError("Enter a name for this profile.");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      // A brand-new profile has no books/series yet, so once this resolves
      // and AuthGate re-renders for the new active profile, has_data will
      // naturally be false and the onboarding import screen takes over.
      await createProfile(id, displayName.trim());
      setDisplayName("");
      onOpenChange(false);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Couldn't create that profile.";
      setError(message);
      toast({ title: "Couldn't create profile", description: message });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Add a new profile</DialogTitle>
          <DialogDescription>
            Each profile gets its own completely separate library -- perfect for tracking a family member&rsquo;s
            books alongside your own.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="new-profile-name">Name</Label>
            <Input
              id="new-profile-name"
              autoFocus
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="e.g. Daughter's Library"
            />
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
          <DialogFooter>
            <Button type="submit" disabled={submitting || !displayName.trim()}>
              {submitting ? "Creating..." : "Create profile"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RenameProfileForm({
  profileId,
  initialDisplayName,
  onOpenChange,
}: {
  profileId: string;
  initialDisplayName: string;
  onOpenChange: (open: boolean) => void;
}) {
  const { toast } = useToast();
  const { renameProfile } = useProfile();
  const [displayName, setDisplayName] = useState(initialDisplayName);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = displayName.trim();
    if (!trimmed) {
      setError("Enter a name for this profile.");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      await renameProfile(profileId, trimmed);
      onOpenChange(false);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Couldn't rename that profile.";
      setError(message);
      toast({ title: "Couldn't rename profile", description: message });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="rename-profile-name">Name</Label>
        <Input
          id="rename-profile-name"
          autoFocus
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
          placeholder="e.g. Daughter's Library"
        />
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <DialogFooter>
        <Button type="submit" disabled={submitting || !displayName.trim()}>
          {submitting ? "Saving..." : "Save name"}
        </Button>
      </DialogFooter>
    </form>
  );
}

function RenameProfileDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { activeProfile } = useProfile();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Rename this profile</DialogTitle>
          <DialogDescription>
            Call this library whatever you&rsquo;d like -- it won&rsquo;t affect anything already in it.
          </DialogDescription>
        </DialogHeader>
        {/* Keyed by profile id + open state so the form's local state is
            re-seeded from the current display name every time the dialog
            opens, instead of reacting to prop changes with an effect. */}
        {open && activeProfile ? (
          <RenameProfileForm
            key={activeProfile.id}
            profileId={activeProfile.id}
            initialDisplayName={activeProfile.display_name}
            onOpenChange={onOpenChange}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function ProfileSwitcher() {
  const { profileId, profiles, setProfileId } = useProfile();
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [renameDialogOpen, setRenameDialogOpen] = useState(false);

  function handleChange(value: string) {
    if (value === ADD_PROFILE_SENTINEL) {
      setAddDialogOpen(true);
      return;
    }
    setProfileId(value);
  }

  // Always show the switcher for owners, even with a single profile today,
  // so "+ Add profile" is discoverable without needing a second profile to
  // already exist.
  return (
    <>
      <div className="flex items-center gap-1">
        <select
          aria-label="Switch library"
          value={profileId ?? ""}
          onChange={(event) => handleChange(event.target.value)}
          className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground"
        >
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.display_name}
            </option>
          ))}
          <option value={ADD_PROFILE_SENTINEL}>+ Add profile…</option>
        </select>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Rename this profile"
          title="Rename this profile"
          disabled={!profileId}
          onClick={() => setRenameDialogOpen(true)}
        >
          <PencilIcon />
        </Button>
      </div>
      <AddProfileDialog open={addDialogOpen} onOpenChange={setAddDialogOpen} />
      <RenameProfileDialog open={renameDialogOpen} onOpenChange={setRenameDialogOpen} />
    </>
  );
}

function TopBar() {
  const { role, logout } = useAuth();
  // The landing page ("/") is a deliberately dark, full-bleed hero (see
  // components/landing/) -- this app's one visual departure from its
  // otherwise light, grayscale theme. Rather than teach the landing page
  // to render behind/under the bar, the bar itself goes transparent and
  // overlays it there, purely a style change (no auth logic touched).
  const isLanding = usePathname() === "/";

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
      <div
        className={
          isLanding
            ? "absolute inset-x-0 top-0 z-10 flex items-center justify-end gap-2 px-4 py-3 text-white/80"
            : "flex items-center justify-end gap-2 border-b bg-muted/40 px-4 py-1.5"
        }
      >
        <ProfileSwitcher />
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
  const { profileId, activeProfile, ready: profileReady, refreshProfiles } = useProfile();
  // Onboarding can be dismissed for the current profile (e.g. someone who
  // wants to add books manually instead of importing a spreadsheet) without
  // that choice persisting -- switching away and back re-evaluates has_data
  // fresh, same as a first visit.
  const [skippedProfileId, setSkippedProfileId] = useState<string | null>(null);

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

  // Only trigger onboarding once we've definitively confirmed (via
  // has_data from GET /profiles) that this profile has no library data --
  // if the /profiles fetch itself failed, activeProfile is null and we fail
  // open to the normal library views rather than guessing.
  const needsOnboarding = Boolean(
    profileReady && activeProfile && !activeProfile.has_data && profileId !== skippedProfileId
  );

  return (
    <div className="flex min-h-full flex-col">
      <NotifyBridge />
      <TopBar />
      {/* Keyed on the active profile so switching libraries fully remounts
          the page tree -- there's no query-cache layer in this app (every
          page just does useEffect(() => fetch..., []) on mount), so a full
          remount is what makes every page's own data fetch re-run against
          the newly-selected profile without touching each page individually. */}
      <div className="flex-1" key={profileId}>
        {needsOnboarding && activeProfile ? (
          <OnboardingImport
            profile={activeProfile}
            onSkip={() => setSkippedProfileId(profileId)}
            onComplete={() => {
              refreshProfiles();
            }}
          />
        ) : (
          children
        )}
      </div>
    </div>
  );
}
