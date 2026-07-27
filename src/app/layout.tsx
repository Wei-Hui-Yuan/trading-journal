import type { Metadata } from 'next';
import { ClerkProvider } from '@clerk/nextjs';
import './globals.css';
import { QueryProvider } from '@/components/QueryProvider';

export const metadata: Metadata = {
  title: 'Trading Journal | Institutional Dashboard',
  description: 'Execution journal & behavioral analytics dashboard',
};

/**
 * Where the data comes from. Read here only to warm the connection to it.
 *
 * Falls back to the production host rather than localhost: this value is
 * inlined at build time, and a preconnect to a dev server that is not running
 * would be a wasted hint on every deployed page.
 */
const API_ORIGIN = (() => {
  const raw = process.env.NEXT_PUBLIC_API_URL || 'https://p01--host--5zs8snzvhrhl.code.run';
  try {
    return new URL(raw).origin;
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
        */}
        {API_ORIGIN && (
          <>
            <link rel="preconnect" href={API_ORIGIN} crossOrigin="anonymous" />
            <link rel="dns-prefetch" href={API_ORIGIN} />
          </>
        )}
      </head>
      <body className="bg-obsidian-bg text-slate-100 antialiased selection:bg-win selection:text-obsidian-bg">
        <ClerkProvider>
          <QueryProvider>{children}</QueryProvider>
        </ClerkProvider>
      </body>
    </html>
  );
}
