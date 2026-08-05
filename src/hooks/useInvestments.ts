'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  clearValuationOverride,
  createHolding,
  createInvestmentTransaction,
  deleteHolding,
  deleteInvestmentTransaction,
  deleteSectorColor,
  getInvestmentTransactions,
  getPortfolio,
  getSectorColors,
  refreshPrices,
  refreshValuations,
  setSectorColor,
  setValuationOverride,
  syncTransactions,
  updateHolding,
  updateInvestmentTransaction,
} from '@/lib/investmentsApi';
import type {
  Holding,
  HoldingPayload,
  InvestmentTransaction,
  Portfolio,
  PriceRefreshResult,
  RefreshResult,
  SectorColor,
  SyncResult,
  TransactionPayload,
  TransactionUpdatePayload,
  ValuationInputRow,
  ValuationOverridePayload,
} from '@/types/investments';

/**
 * Kept under an `investments` root so one invalidation covers the book, and so
 * nothing here can collide with the trading journal's keys.
 */
export const investmentKeys = {
  root: ['investments'] as const,
  portfolio: ['investments', 'portfolio'] as const,
  transactions: (ticker?: string) =>
    ['investments', 'transactions', ticker ?? 'all'] as const,
  sectorColors: ['investments', 'sector-colors'] as const,
};

/**
 * The whole book in one query.
 *
 * Every figure on the page comes from here, including portfolio weight, which
 * cannot be computed for a row without the total across every other row.
 * Splitting it per holding would just move that join into the browser.
 */
export function usePortfolio() {
  return useQuery<Portfolio, Error>({
    queryKey: investmentKeys.portfolio,
    queryFn: getPortfolio,
  });
}

export function useInvestmentTransactions(ticker?: string) {
  return useQuery<InvestmentTransaction[], Error>({
    queryKey: investmentKeys.transactions(ticker),
    queryFn: () => getInvestmentTransactions(ticker),
  });
}

/**
 * Quantity and average cost are derived from the ledger on read, so any write
 * to it changes the portfolio — hence the whole root is invalidated rather
 * than the transaction list alone.
 */
export function useCreateInvestmentTransaction() {
  const queryClient = useQueryClient();
  return useMutation<InvestmentTransaction, Error, TransactionPayload>({
    mutationFn: createInvestmentTransaction,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

/**
 * A correction, not a new fact -- editing a transaction changes the derived
 * quantity and average cost of everything after it in the ledger, so the
 * whole portfolio root is invalidated rather than just the transaction list.
 */
export function useUpdateInvestmentTransaction() {
  const queryClient = useQueryClient();
  return useMutation<
    InvestmentTransaction,
    Error,
    { id: string; payload: TransactionUpdatePayload }
  >({
    mutationFn: ({ id, payload }) => updateInvestmentTransaction(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

export function useDeleteInvestmentTransaction() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: deleteInvestmentTransaction,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

export function useCreateHolding() {
  const queryClient = useQueryClient();
  return useMutation<Holding, Error, HoldingPayload>({
    mutationFn: createHolding,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

export function useUpdateHolding() {
  const queryClient = useQueryClient();
  return useMutation<
    Holding,
    Error,
    { ticker: string; payload: Partial<HoldingPayload> }
  >({
    mutationFn: ({ ticker, payload }) => updateHolding(ticker, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

export function useDeleteHolding() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: deleteHolding,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

/**
 * Writing an override changes what the model is fed, so the recomputed
 * intrinsic value arrives from the server on the next fetch. The engine is
 * deliberately NOT duplicated in the browser — one implementation, and the
 * displayed number always comes from the same place the stored one would.
 */
export function useSetValuationOverride() {
  const queryClient = useQueryClient();
  return useMutation<
    ValuationInputRow,
    Error,
    { ticker: string; payload: ValuationOverridePayload }
  >({
    mutationFn: ({ ticker, payload }) => setValuationOverride(ticker, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

export function useClearValuationOverride() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: clearValuationOverride,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

/** Daily. One provider call per holding. */
export function useRefreshPrices() {
  const queryClient = useQueryClient();
  return useMutation<PriceRefreshResult, Error, void>({
    mutationFn: refreshPrices,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

/** Monthly, and slow: roughly ninety seconds for a thirteen-name book. */
export function useRefreshValuations() {
  const queryClient = useQueryClient();
  return useMutation<RefreshResult, Error, boolean | undefined>({
    mutationFn: (force) => refreshValuations(force ?? false),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

/**
 * Pull fills from the long-term book's own IBKR account -- a separate query
 * from the trading journal's ingest (see sync_investment_transactions in
 * main.py). Invalidates the whole root: a new fill changes quantity and
 * average cost, and can introduce a holding the book has not seen before.
 */
export function useSyncTransactions() {
  const queryClient = useQueryClient();
  return useMutation<SyncResult, Error, void>({
    mutationFn: syncTransactions,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.root });
    },
  });
}

/** Sparse -- only the sectors the trader has manually recolored. */
export function useSectorColors() {
  return useQuery<SectorColor[], Error>({
    queryKey: investmentKeys.sectorColors,
    queryFn: getSectorColors,
  });
}

export function useSetSectorColor() {
  const queryClient = useQueryClient();
  return useMutation<SectorColor, Error, { sector: string; color: string }>({
    mutationFn: ({ sector, color }) => setSectorColor(sector, color),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.sectorColors });
    },
  });
}

export function useDeleteSectorColor() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: deleteSectorColor,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: investmentKeys.sectorColors });
    },
  });
}
