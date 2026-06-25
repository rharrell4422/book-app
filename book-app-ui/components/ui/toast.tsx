"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

export function Toast({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("toast", className)}
      {...props}
    />
  );
}

export function ToastTitle({ children }: { children: React.ReactNode }) {
  return <div className="toast-title">{children}</div>;
}

export function ToastDescription({ children }: { children: React.ReactNode }) {
  return <div className="toast-description">{children}</div>;
}

export function ToastClose() {
  return <button className="toast-close">×</button>;
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  return <div>{children}</div>;
}

export function ToastViewport() {
  return <div className="toast-viewport" />;
}
