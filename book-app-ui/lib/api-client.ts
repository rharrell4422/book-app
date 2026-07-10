/**
 * Shared client-side API fetch helper. Tries the Next.js API proxy route
 * first, then falls back to directly hitting the backend on common local
 * dev hosts/ports. Previously duplicated near-identically in BooksClient.tsx
 * and the series detail page.
 */

const STATIC_API_BASE_CANDIDATES = [
  process.env.NEXT_PUBLIC_API_BASE_URL,
  "http://localhost:8000",
  "http://127.0.0.1:8000",
].filter(Boolean) as string[];

export function normalizeBaseUrl(value: string) {
  return value.replace(/\/+$/, "");
}

export function getApiBaseCandidates() {
  const dynamicCandidates: string[] = [];
  if (typeof window !== "undefined") {
    dynamicCandidates.push(`${window.location.protocol}//${window.location.hostname}:8000`);
  }

  return Array.from(new Set([...STATIC_API_BASE_CANDIDATES, ...dynamicCandidates]));
}

export async function fetchApiWithFallback(path: string, init?: RequestInit) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const isSuggestGetRequest = (init?.method || "GET").toUpperCase() === "GET" && /\/suggest(?:\?|$)/.test(normalizedPath);
  const requestInit: RequestInit = isSuggestGetRequest
    ? { ...init, cache: "no-store" }
    : init ?? {};
  const baseCandidates = getApiBaseCandidates();
  const candidates = [
    `/api${normalizedPath}`,
    ...baseCandidates.map((base) => `${normalizeBaseUrl(base)}${normalizedPath}`),
  ];

  // If route includes a trailing slash, also try without it to avoid router mismatches.
  if (normalizedPath.endsWith("/")) {
    const trimmedPath = normalizedPath.slice(0, -1);
    candidates.push(`/api${trimmedPath}`);
    candidates.push(...baseCandidates.map((base) => `${normalizeBaseUrl(base)}${trimmedPath}`));
  }

  let lastError: Error | null = null;
  for (const url of candidates) {
    try {
      const response = await fetch(url, requestInit);
      if (response.ok) {
        return response;
      }
      lastError = new Error(`Failed to load ${normalizedPath} (${response.status})`);
    } catch (error) {
      lastError = error instanceof Error ? error : new Error("Network error");
    }
  }

  throw lastError ?? new Error(`Failed to load ${normalizedPath}`);
}
