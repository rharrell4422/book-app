"use client";

import { createContext, useContext, useState } from "react";

type ToastData = {
  id: string;
  title?: string;
  description?: string;
  action?: React.ReactNode;
};

const ToastContext = createContext<{
  toasts: ToastData[];
  toast: (data: Omit<ToastData, "id">) => void;
}>({
  toasts: [],
  toast: () => {},
});

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastData[]>([]);

  function toast(data: Omit<ToastData, "id">) {
    setToasts((prev) => [...prev, { id: Math.random().toString(), ...data }]);
  }

  return (
    <ToastContext.Provider value={{ toasts, toast }}>
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
