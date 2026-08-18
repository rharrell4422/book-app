"use client";

import { createContext, useCallback, useContext, useRef, useState } from "react";

import { addToastRecord, removeToastRecord, scheduleToastAutoDismiss } from "@/lib/toast-scheduler";

type ToastData = {
  id: string;
  title?: string;
  description?: string;
  action?: React.ReactNode;
};

const ToastContext = createContext<{
  toasts: ToastData[];
  toast: (data: Omit<ToastData, "id">) => string;
  dismiss: (id: string) => void;
}>({
  toasts: [],
  toast: () => "",
  dismiss: () => {},
});

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastData[]>([]);
  // Tracks each toast's auto-dismiss canceler so a manual dismiss() (or the
  // MAX_VISIBLE_TOASTS cap dropping an older one) doesn't leave a stray
  // timer that fires later against an id that's no longer in the list.
  const dismissTimersRef = useRef(new Map<string, () => void>());

  const dismiss = useCallback((id: string) => {
    dismissTimersRef.current.get(id)?.();
    dismissTimersRef.current.delete(id);
    setToasts((prev) => removeToastRecord(prev, id));
  }, []);

  const toast = useCallback(
    (data: Omit<ToastData, "id">) => {
      const id = Math.random().toString(36).slice(2);
      setToasts((prev) => addToastRecord(prev, { id, ...data }));
      dismissTimersRef.current.set(
        id,
        scheduleToastAutoDismiss(() => dismiss(id)),
      );
      return id;
    },
    [dismiss],
  );

  return (
    <ToastContext.Provider value={{ toasts, toast, dismiss }}>
      {children}
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

// This is the named export your page expects
export const toast = () => {
  console.warn("toast() called outside provider — this is a placeholder.");
};
