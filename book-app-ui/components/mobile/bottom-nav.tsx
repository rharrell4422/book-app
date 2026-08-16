"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LibraryIcon, ListTreeIcon, UserRoundIcon } from "lucide-react";

import { cn } from "@/lib/utils";

function NavLink({
  href,
  label,
  icon: Icon,
  active,
}: {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  active: boolean;
}) {
  return (
    <Link
      href={href}
      className={cn(
        "flex flex-1 flex-col items-center gap-0.5 py-1.5 text-[11px] font-medium",
        active ? "text-foreground" : "text-muted-foreground"
      )}
    >
      <Icon className={cn("h-5 w-5", active ? "text-foreground" : "text-muted-foreground")} />
      {label}
    </Link>
  );
}

function NavButton({
  label,
  icon: Icon,
  onClick,
}: {
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex flex-1 flex-col items-center gap-0.5 py-1.5 text-[11px] font-medium text-muted-foreground"
    >
      <Icon className="h-5 w-5 text-muted-foreground" />
      {label}
    </button>
  );
}

/**
 * Fixed bottom tab bar, mounted only for the mobile render branch (see
 * useIsMobile() in auth-gate.tsx). Only covers routes that exist today --
 * Search and Upcoming tabs land here once those pages are built.
 */
export function BottomNav({ onOpenProfile }: { onOpenProfile: () => void }) {
  const pathname = usePathname();

  return (
    <nav className="fixed inset-x-0 bottom-0 z-40 flex border-t bg-card/95 pb-[env(safe-area-inset-bottom)] backdrop-blur-sm">
      <NavLink href="/books" label="Library" icon={LibraryIcon} active={pathname.startsWith("/books")} />
      <NavLink href="/series" label="Series" icon={ListTreeIcon} active={pathname.startsWith("/series")} />
      <NavButton label="Profile" icon={UserRoundIcon} onClick={onOpenProfile} />
    </nav>
  );
}
