"use client";

import * as React from "react";

type ToastProps = {
  id: number;
  title?: string;
  description?: string;
};

const ToastContext = React.createContext<{
  toasts: ToastProps[];
  toast: (props: Omit<ToastProps, "id">) => void;
}>({
  toasts: [],
  toast: () => {},
});

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<ToastProps[]>([]);

  const toast = (props: Omit<ToastProps, "id">) => {
    const id = Date.now();
    setToasts((prev) => [...prev, { id, ...props }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  };

  return (
    <ToastContext.Provider value={{ toasts, toast }}>
      {children}
    </ToastContext.Provider>
  );
}

export function useToast() {
  return React.useContext(ToastContext);
}
