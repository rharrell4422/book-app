import { Suspense } from "react";

import StandaloneBooksClient from "./StandaloneBooksClient";

export default function Page() {
  return (
    <Suspense fallback={<div className="p-6">Loading standalone books…</div>}>
      <StandaloneBooksClient />
    </Suspense>
  );
}
