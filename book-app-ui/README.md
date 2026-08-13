# Book App UI

Next.js (App Router) frontend for the Book App personal library tracker. Talks
to the FastAPI backend in the repo root via a same-origin API proxy route
(`app/api/[...path]/route.ts`).

## Local development

The backend must be running first (see the repo root `README`/`.env.example`
for `OWNER_PASSWORD`, `AUTH_SECRET_KEY`, etc.), typically on
`http://127.0.0.1:8000`.

```bash
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The dev server proxies
`/api/*` requests to the backend using `API_BASE_URL` (falls back to
`http://127.0.0.1:8000` if unset).

## Environment variables

| Variable | Where | Purpose |
|---|---|---|
| `API_BASE_URL` | server (proxy route) | Backend URL used server-side. Set this on the deployed frontend service. |
| `NEXT_PUBLIC_API_BASE_URL` | client + server | Fallback backend URL, used if a direct (non-proxied) call is needed. |

## Scripts

- `npm run dev` – start the dev server
- `npm run build` / `npm run start` – production build/serve
- `npm run lint` – ESLint
- `npm test` – Vitest unit tests

## Deployment

This app is deployed on **Railway** as a separate service from the backend
(not Vercel, despite the `create-next-app` defaults still lingering in a few
config comments). Point `API_BASE_URL` at the backend service's Railway URL.

## Stack

Next.js 16 (App Router), React 19, Tailwind CSS v4, shadcn/ui (Radix
primitives), TypeScript in strict mode.
