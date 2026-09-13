import type { Metadata } from 'next';
import { ClerkProvider } from '@clerk/nextjs';
import './globals.css';
import { QueryProvider } from '@/components/QueryProvider';
import { AppNav } from '@/components/AppNav';

export const metadata: Metadata = {
  title: 'Trading Journal | Institutional Dashboard',
  description: 'Execution journal & behavioral analytics dashboard',
};

/**
 * Where the data comes from. Read here only to warm the connection to it.
 *
 * Falls back to a placeholder rather than a real host in this public copy --
 * see NEXT_PUBLIC_API_URL in .env.example. Upstream, this falls back to the
 * production host instead of localhost, since this value is inlined at build
 * time and a preconnect to a dev server that is not running would be a
 * wasted hint on every deployed page. With no env var set, this now falls
 * back to a placeholder host instead: the preconnect hint is simply wasted
 * rather than pointed at anything real, until NEXT_PUBLIC_API_URL is set.
 */
const API_ORIGIN = (() => {
  const raw = process.env.NEXT_PUBLIC_API_URL || 'https://your-api-host.example.com';
  try {
    return new URL(raw).origin;
  } catch {
    return null;
  }
})();

/**
 * Where Clerk itself lives — a THIRD origin, and the one everything waits on.
 *
 * The host is encoded in the publishable key rather than configured anywhere,
 * so nothing in this codebase names it and the browser cannot discover it
 * until `ClerkProvider` has booted and injected its script tag. By then the
 * page has already been parsed and the ~500ms handshake starts late, with the
 * whole session — and therefore every API request behind it — queued after it.
 *
 * The key is `pk_test_<base64>` / `pk_live_<base64>`, and the payload decodes
 * to the frontend API host with a single trailing '$'. That is Clerk's own
 * format, and this mirrors the validation in @clerk/shared's
 * `parsePublishableKey`: the '$' must be the last character and the only one,
 * and what remains must look like a host. Anything else yields null rather
 * than a guess, because a preconnect to a wrong origin is a wasted socket.
 */
const CLERK_ORIGIN = (() => {
  const key = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
  if (!key) return null;
  // The prefix is part of the format, not decoration. Without this check a
  // secret key pasted into the wrong variable still decodes to a real host
  // and gets hinted, which reads as "this is fine" about a misconfiguration
  // that is anything but.
  if (!key.startsWith('pk_test_') && !key.startsWith('pk_live_')) return null;
  try {
    const decoded = atob(key.split('_')[2] ?? '');
    if (!decoded.endsWith('$')) return null;
    const host = decoded.slice(0, -1);
    if (!host || host.includes('$') || !host.includes('.')) return null;
    return `https://${host}`;
  } catch {
    return null;
  }
})();

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <head>
        {/*
          No request reaches the API until Clerk has loaded, resolved a
          session and handed over a token — measured at ~2.1s after
          navigation, on a page whose shell is interactive at ~0.5s. The
          browser only then discovers it needs a connection to the API and
          pays DNS + TCP + TLS (~500ms) before the first byte is even
          requested, with all six dashboard queries queued behind it.

          These hints move that handshake to the start of the page load, in
          parallel with the Clerk work, so the token arrives to an open
          connection. dns-prefetch is the fallback for browsers that ignore
          preconnect; both are no-ops if the connection is never used.

          Clerk comes FIRST, and warming the API alone was half a fix. The
          2.1s above is mostly Clerk's own handshake, and until that resolves
          the warmed API socket sits idle — the wait was moved off the API and
          left sitting on the origin nobody had hinted. Ordered so the browser
          opens the blocking connection before the one queued behind it.

          crossOrigin="anonymous" is not decoration on either. Clerk injects
          its script with exactly that attribute (@clerk/shared, loadClerkJsScript)
          and the API is fetched with a bearer token, not cookies. A hint whose
          CORS mode disagrees with the real request warms a connection the
          request cannot use, and the browser quietly opens a second one.
        */}
        {CLERK_ORIGIN && (
          <>
            <link rel="preconnect" href={CLERK_ORIGIN} crossOrigin="anonymous" />
            <link rel="dns-prefetch" href={CLERK_ORIGIN} />
          </>
        )}
        {API_ORIGIN && (
          <>
            <link rel="preconnect" href={API_ORIGIN} crossOrigin="anonymous" />
            <link rel="dns-prefetch" href={API_ORIGIN} />
          </>
        )}
      </head>
      <body className="bg-obsidian-bg text-slate-100 antialiased selection:bg-win selection:text-obsidian-bg">
        <ClerkProvider>
          <QueryProvider>
            {/*
              Every page, not just the dashboard. This was already the stated
              intent -- AppNav mounts `useSyncRunWatcher` and the sync toast,
              both commented as relying on being present app-wide -- but it
              was only ever imported by the dashboard, so a sync started there
              and then navigated away from finished with nothing refreshing
              the ledger and no summary shown.

              Inside QueryProvider because the nav reads the pending-review
              count and the sync record from the query cache.
            */}
            <AppNav />
            {children}
          </QueryProvider>
        </ClerkProvider>
      </body>
    </html>
  );
}
