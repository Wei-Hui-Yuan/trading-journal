'use client';

import React, { useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ReactQueryDevtools } from '@tanstack/react-query-devtools';

import { PendingActionProvider } from './PendingActionProvider';

/**
 * React Query context for the App Router.
 *
 * The QueryClient is created inside `useState` rather than at module scope:
 * a module-level client would be shared across requests on the server and leak
 * one user's cached data into another's response.
 */
export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Trading data is not real-time; a short window avoids refetching
            // on every remount while keeping the dashboard current.
            staleTime: 30_000,
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      {/* Inside the query client, because a deferred delete commits through a
          mutation and must be able to invalidate the cache when it lands. */}
      <PendingActionProvider>{children}</PendingActionProvider>
      {process.env.NODE_ENV === 'development' && (
        <ReactQueryDevtools initialIsOpen={false} />
      )}
    </QueryClientProvider>
  );
}

export default QueryProvider;
