'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { runDataAudit } from '@/lib/api';
import type { AuditResult } from '@/types/api';

const auditKey = ['data-audit', 'last'] as const;

/**
 * The last audit run THIS SESSION, or undefined if none.
 *
 * Same shape as `useLastSync`, and for the same reason: never fetched, never
 * persisted. After a reload the honest answer is "not since you opened this",
 * because the ledger can have changed underneath a remembered verdict and a
 * stale green badge is worse than no badge -- it asserts a health nothing has
 * checked.
 */
export function useLastAudit(): AuditResult | undefined {
  const queryClient = useQueryClient();
  const { data } = useQuery<AuditResult | null>({
    queryKey: auditKey,
    // Never fetched; the mutation below is the only writer.
    queryFn: () => queryClient.getQueryData<AuditResult>(auditKey) ?? null,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  return data ?? undefined;
}

/**
 * Run the audit on demand. Read-only on the server -- nothing is invalidated
 * here because nothing changed; the result is written to the cache key the
 * badge above reads.
 */
export function useRunDataAudit() {
  const queryClient = useQueryClient();
  return useMutation<AuditResult, Error, void>({
    mutationFn: runDataAudit,
    onSuccess: (result) => {
      queryClient.setQueryData(auditKey, result);
    },
  });
}
