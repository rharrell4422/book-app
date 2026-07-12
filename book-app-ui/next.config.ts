import type { NextConfig } from "next";

// Deliberately no rewrites() here for /api/*: that's handled by the custom
// route handler in app/api/[...path]/route.ts, which resolves the backend
// URL from API_BASE_URL (read at request time) instead of a hardcoded
// localhost address. A rewrites() rule for the same path would run before
// that dynamic route and silently shadow it in production.
const nextConfig: NextConfig = {};

export default nextConfig;
