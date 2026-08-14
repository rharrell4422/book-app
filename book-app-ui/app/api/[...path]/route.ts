import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

// Prefer a plain (non NEXT_PUBLIC_) server-side var here: this route only
// ever runs on the server, and NEXT_PUBLIC_ vars get statically inlined at
// *build time*, so if the var wasn't visible during that specific build,
// it'd be stuck wrong until a fresh rebuild. Reading API_BASE_URL directly
// at request time avoids that class of bug entirely.
const BACKEND_BASE_URL =
  process.env.API_BASE_URL || process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000";

async function proxyRequest(request: NextRequest, pathSegments: string[]) {
  // Preserve a trailing slash from the original request path. FastAPI routes
  // are often registered as e.g. "/series/" -- if we drop the slash here,
  // FastAPI issues a 307 redirect to add it back, and Node's fetch does not
  // reliably replay non-GET requests with a body across that redirect
  // (surfaces as an opaque "fetch failed" with no HTTP response at all).
  // Building the exact target path up front avoids that redirect entirely.
  const hadTrailingSlash = request.nextUrl.pathname.endsWith("/");
  let joinedPath = pathSegments.join("/");
  if (hadTrailingSlash && !joinedPath.endsWith("/")) {
    joinedPath += "/";
  }
  const targetUrl = new URL(joinedPath, `${BACKEND_BASE_URL.replace(/\/+$/, "")}/`);
  targetUrl.search = request.nextUrl.search;

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("content-length");

  const hasBody = !["GET", "HEAD"].includes(request.method);
  const body = hasBody ? await request.arrayBuffer() : undefined;
  const requestInit: RequestInit = {
    method: request.method,
    headers,
    body,
    redirect: "follow",
  };
  if (request.method === "GET") {
    requestInit.cache = "no-store";
  }

  let response: Response;
  try {
    response = await fetch(targetUrl.toString(), requestInit);
  } catch (error) {
    // Surface *why* the proxy failed (bad backend URL, DNS, connection
    // refused, etc.) instead of letting Next.js swallow it into an opaque
    // "Internal Server Error" with no diagnostic info.
    const message = error instanceof Error ? error.message : String(error);
    return NextResponse.json(
      {
        detail: "Proxy failed to reach backend",
        backendUrl: targetUrl.toString(),
        error: message,
      },
      { status: 502 },
    );
  }

  const responseHeaders = new Headers(response.headers);
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("transfer-encoding");

  return new NextResponse(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  });
}

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxyRequest(request, path);
}

export async function POST(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxyRequest(request, path);
}

export async function PUT(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxyRequest(request, path);
}

export async function PATCH(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxyRequest(request, path);
}

export async function DELETE(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxyRequest(request, path);
}

export async function OPTIONS(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxyRequest(request, path);
}