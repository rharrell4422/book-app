import { Suspense } from "react";

import BooksClient from "./BooksClient";

export default function Page() {
  return (
    <Suspense fallback={<div className="p-6">Loading library…</div>}>
      <BooksClient />
    </Suspense>
  );
}
