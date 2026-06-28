import type { Metadata } from "next";
import "./globals.css";

import { Providers } from "./providers";
import { Toaster } from "@/components/ui/toaster";

export const metadata: Metadata = {
  title: "Book App",
  description: "Library and series viewer",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">
        <Providers>
          {children}
        </Providers>

        {/* ⭐ REQUIRED for toast + UI updates */}
        <Toaster />
      </body>
    </html>
  );
}
