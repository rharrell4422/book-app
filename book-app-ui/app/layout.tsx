import type { Metadata } from "next";
import "./globals.css";

import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "ReaderPro",
  description: "Your personal book intelligence engine",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">
        {/* Toaster lives inside <Providers>, nested under ToastProvider so
            useToast() can actually see the shared toast state. */}
        <Providers>
          {children}
        </Providers>
      </body>
    </html>
  );
}
