'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  createSizingEntry,
  deleteSizingEntry,
  getSizingScratchpad,
  promoteSizingEntry,
  updateSizingEntry,
} from '@/lib/api';
import type { TradePlan } from '@/types/api';
import type {
  SizingEntryPayload,
  SizingEntryUpdatePayload,
  SizingScratchpadEntry,
} from '@/types/sizing';

import { queryKeys } from './useTradeInbox';

/** Kept under its own root, entirely apart from `queryKeys` above it, since
 * nothing in the scratchpad is read by anything the trading journal caches --
 * the one exception, promotion, invalidates `queryKeys.plansRoot` directly
 * rather than sharing a key with it. */
export const sizingKeys = {
  root: ['sizingScratchpad'] as const,
};

/**
 * Every note from the last three days, newest first.
 *
 * `staleTime: 0` (the default) is deliberate here, unlike most of this app's
 * reads: a note is worth re-fetching on every focus, because the whole point
 * of this list is that it is being edited across several quick visits while a
 * price is watched, not read once and left.
 */
export function useSizingScratchpad() {
  return useQuery<SizingScratchpadEntry[], Error>({
    queryKey: sizingKeys.root,
    queryFn: getSizingScratchpad,
  });
}

export function useCreateSizingEntry() {
  const queryClient = useQueryClient();
  return useMutation<SizingScratchpadEntry, Error, SizingEntryPayload>({
    mutationFn: createSizingEntry,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sizingKeys.root });
    },
  });
}

export function useUpdateSizingEntry() {
  const queryClient = useQueryClient();
  return useMutation<
    SizingScratchpadEntry,
    Error,
    { id: string; payload: SizingEntryUpdatePayload }
  >({
    mutationFn: ({ id, payload }) => updateSizingEntry(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sizingKeys.root });
    },
  });
}

export function useDeleteSizingEntry() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: deleteSizingEntry,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sizingKeys.root });
    },
  });
}

/**
 * Turn a note into a real plan.
 *
 * Invalidates BOTH roots, because the one action reaches into both domains:
 * the note is gone (`sizingKeys.root`) and a new OPEN plan exists
 * (`queryKeys.plansRoot`) — the Plan dock and the journal need to see it
 * exactly as if it had been entered through the Plan modal.
 */
export function usePromoteSizingEntry() {
  const queryClient = useQueryClient();
  return useMutation<TradePlan, Error, string>({
    mutationFn: promoteSizingEntry,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sizingKeys.root });
      queryClient.invalidateQueries({ queryKey: queryKeys.plansRoot });
    },
  });
}
