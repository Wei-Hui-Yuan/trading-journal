# Trading Journal

A personal execution journal and analytics dashboard for trades placed
through Interactive Brokers, plus a separate long-term investment tracker.
Two questions, two schemas: a trading journal is built around a round trip
opened and closed (FIFO matching, R-multiples, entry slippage, a discipline
checklist), while the investment book is built around a holding bought over
time and valued against what the business is worth rather than against a
stop.

## What's here

One repo, two deployables:

- **`/`** — the frontend: Next.js 16 (App Router), React 19, TanStack Query
  v5, Tailwind v4, Clerk for auth.
- **`api/`** — the backend: FastAPI + async SQLAlchemy 2.0 on Python 3.12,
  talking to a Postgres database (developed against Supabase, but any
  Postgres 17 works). See [`api/README.md`](api/README.md) for everything
  backend-specific: migrations, the sync pipeline, response caching, and the
  standalone diagnostic scripts.

Broker sync is built for IBKR's Flex Web Service specifically; using a
different broker means either hand-logging trades through the journal's own
plan/review flow or writing a new sync path.

## Prerequisites

- Node 22+
- Python 3.12
- A Postgres 17 database ([Supabase](https://supabase.com) has a free tier
  and is what this project runs against)
- A [Clerk](https://clerk.com) application (optional for a quick local
  spin-up — see below)

## Setup

**1. Backend** — follow [`api/README.md`](api/README.md) first. It covers
installing dependencies, configuring `api/.env`, running migrations, and
starting the API. Come back here once `uvicorn main:app --reload` is
serving on `http://localhost:8000`.

**2. Frontend:**

```bash
npm install
cp .env.example .env.local   # then fill in real values -- see the file itself
npm run dev
```

The app is now at `http://localhost:3000`.

`.env.local` is optional for the very first run: with no Clerk keys set,
`next dev` auto-provisions a throwaway "keyless" Clerk instance so the app
boots and you can look around. Real, persistent sign-in needs your own Clerk
app — see the comments in `.env.example` for exactly which keys and where to
find them.

## Scripts

```bash
npm run dev          # start the dev server
npm run build        # production build
npm run typecheck    # tsc --noEmit
npm run lint         # eslint, capped at the current warning count
npm run test         # vitest run
npm run test:coverage
```

Backend equivalents (`cd api` first) are in
[`api/README.md`](api/README.md#tests).

## Deploying

Nothing here is tied to a specific host. This project runs on Vercel
(frontend) and Northflank (backend, via the included `api/Dockerfile`) with
Supabase Postgres, but the frontend is a standard Next.js app and the backend
is a standard containerized ASGI app — any equivalent hosts work. Whichever
you pick, the backend needs `CORS_ALLOW_ORIGINS` (or
`CORS_ALLOW_ORIGIN_REGEX`) set to wherever the frontend actually lives — see
`api/.env.example`.
