/**
 * The investment book's React Query layer: which query key each hook reads,
 * and which key each mutation invalidates on success.
 *
 * `investmentsApi` is mocked wholesale -- the HTTP calls themselves are
 * covered in investmentsApi.test.ts, and duplicating that here would test the
 * same thing twice while missing the thing THIS file is actually responsible
 * for: wiring a mutation to the right cache key.
 *
 * That wiring is worth a real assertion rather than a glance, because eleven
 * of the thirteen mutations here invalidate the SAME key
 * (`investmentKeys.root`) for eleven DIFFERENT stated reasons -- a correction
 * changes cost basis, a new transaction changes quantity, a price refresh
 * changes market value. It would be easy for a new mutation to invalidate
 * nothing, or the wrong thing, and have every existing test still pass because
 * none of them are the one now broken.
 */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  investmentKeys,
  useClearValuationOverride,
  useCorrectBasis,
  useCreateHolding,
  useCreateInvestmentTransaction,
  useDeleteHolding,
  useDeleteInvestmentTransaction,
  useDeleteSectorColor,
  useInvestmentTransactions,
  usePortfolio,
  useRefreshPrices,
  useRefreshValuations,
  useSectorColors,
  useSetSectorColor,
  useSetValuationOverride,
  useSyncTransactions,
  useUpdateHolding,
  useUpdateInvestmentTransaction,
} from '@/hooks/useInvestments';

vi.mock('@/lib/investmentsApi', () => ({
  getPortfolio: vi.fn(),
  getInvestmentTransactions: vi.fn(),
  createInvestmentTransaction: vi.fn(),
  updateInvestmentTransaction: vi.fn(),
  deleteInvestmentTransaction: vi.fn(),
  createHolding: vi.fn(),
  updateHolding: vi.fn(),
  deleteHolding: vi.fn(),
  correctBasis: vi.fn(),
  setValuationOverride: vi.fn(),
  clearValuationOverride: vi.fn(),
  refreshPrices: vi.fn(),
  refreshValuations: vi.fn(),
  syncTransactions: vi.fn(),
  getSectorColors: vi.fn(),
  setSectorColor: vi.fn(),
  deleteSectorColor: vi.fn(),
}));

import * as investmentsApi from '@/lib/investmentsApi';

const api = investmentsApi as unknown as Record<string, ReturnType<typeof vi.fn>>;

let client: QueryClient;

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(QueryClientProvider, { client }, children);
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  Object.values(api).forEach((fn) => fn.mockReset());
});

describe('investmentKeys', () => {
  it('scopes transactions by ticker, with a stable segment when none is given', () => {
    // `?? 'all'` is what keeps "every transaction" and "transactions for a
    // ticker literally named all" from ever landing on the same cache entry --
    // an unlikely ticker, but the key would collide silently if one existed.
    expect(investmentKeys.transactions('VOO')).toEqual([
      'investments', 'transactions', 'VOO',
    ]);
    expect(investmentKeys.transactions()).toEqual([
      'investments', 'transactions', 'all',
    ]);
  });

  it('roots every other key under one prefix', () => {
    expect(investmentKeys.root).toEqual(['investments']);
    expect(investmentKeys.portfolio).toEqual(['investments', 'portfolio']);
    expect(investmentKeys.sectorColors).toEqual(['investments', 'sector-colors']);
  });
});

describe('usePortfolio', () => {
  it('reads from the portfolio key and returns the fetched value', async () => {
    const portfolio = { holdings: [{ ticker: 'VOO' }], total_market_value: 7421.4 };
    api.getPortfolio.mockResolvedValue(portfolio);

    const { result } = renderHook(() => usePortfolio(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBe(portfolio);
  });
});

describe('useInvestmentTransactions', () => {
  it('keys the query by ticker, so two tickers cache independently', async () => {
    api.getInvestmentTransactions.mockImplementation((ticker?: string) =>
      Promise.resolve(ticker ? [{ ticker }] : [])
    );

    const { result: voo } = renderHook(() => useInvestmentTransactions('VOO'), {
      wrapper,
    });
    const { result: all } = renderHook(() => useInvestmentTransactions(), {
      wrapper,
    });

    await waitFor(() => expect(voo.current.isSuccess).toBe(true));
    await waitFor(() => expect(all.current.isSuccess).toBe(true));

    expect(voo.current.data).toEqual([{ ticker: 'VOO' }]);
    expect(all.current.data).toEqual([]);
    expect(client.getQueryData(investmentKeys.transactions('VOO'))).toEqual([
      { ticker: 'VOO' },
    ]);
    expect(client.getQueryData(investmentKeys.transactions())).toEqual([]);
  });
});

/**
 * The eleven mutations that all invalidate `investmentKeys.root` for
 * different stated reasons. Table-driven, because the interesting claim is
 * identical eleven times over: call the api function with the right
 * arguments, then invalidate the root.
 */
const ROOT_INVALIDATING_MUTATIONS: {
  name: string;
  useHook: () => { mutate: (variables: never) => void };
  apiFn: string;
  variables: unknown;
  expectedCallArgs: unknown[];
}[] = [
  {
    name: 'useCreateInvestmentTransaction',
    useHook: useCreateInvestmentTransaction,
    apiFn: 'createInvestmentTransaction',
    variables: { ticker: 'VOO', transaction_type: 'BUY' },
    expectedCallArgs: [{ ticker: 'VOO', transaction_type: 'BUY' }],
  },
  {
    name: 'useUpdateInvestmentTransaction',
    useHook: useUpdateInvestmentTransaction,
    apiFn: 'updateInvestmentTransaction',
    variables: { id: 't1', payload: { note: 'fixed' } },
    expectedCallArgs: ['t1', { note: 'fixed' }],
  },
  {
    name: 'useDeleteInvestmentTransaction',
    useHook: useDeleteInvestmentTransaction,
    apiFn: 'deleteInvestmentTransaction',
    variables: 't1',
    expectedCallArgs: ['t1'],
  },
  {
    name: 'useCreateHolding',
    useHook: useCreateHolding,
    apiFn: 'createHolding',
    variables: { ticker: 'VOO', category: 'ETF' },
    expectedCallArgs: [{ ticker: 'VOO', category: 'ETF' }],
  },
  {
    name: 'useUpdateHolding',
    useHook: useUpdateHolding,
    apiFn: 'updateHolding',
    variables: { ticker: 'VOO', payload: { sector: 'ETF' } },
    expectedCallArgs: ['VOO', { sector: 'ETF' }],
  },
  {
    name: 'useDeleteHolding',
    useHook: useDeleteHolding,
    apiFn: 'deleteHolding',
    variables: 'VOO',
    expectedCallArgs: ['VOO'],
  },
  {
    name: 'useCorrectBasis',
    useHook: useCorrectBasis,
    apiFn: 'correctBasis',
    variables: { ticker: 'VOO', payload: { quantity: 10 } },
    expectedCallArgs: ['VOO', { quantity: 10 }],
  },
  {
    name: 'useSetValuationOverride',
    useHook: useSetValuationOverride,
    apiFn: 'setValuationOverride',
    variables: { ticker: 'VOO', payload: { base_flow: 1000 } },
    expectedCallArgs: ['VOO', { base_flow: 1000 }],
  },
  {
    name: 'useClearValuationOverride',
    useHook: useClearValuationOverride,
    apiFn: 'clearValuationOverride',
    variables: 'VOO',
    expectedCallArgs: ['VOO'],
  },
];

/**
 * `refreshPrices` and `syncTransactions` take no arguments of their own, so
 * the table above cannot give them a meaningful `expectedCallArgs` -- an
 * empty slice would pass whether or not the mutation variable leaked through.
 * Covered here instead, asserting the one thing that IS worth pinning: the
 * caller's `mutate(undefined)` does not appear as the api function's first
 * argument.
 */
describe.each([
  { name: 'useRefreshPrices', useHook: useRefreshPrices, apiFn: 'refreshPrices' as const },
  { name: 'useSyncTransactions', useHook: useSyncTransactions, apiFn: 'syncTransactions' as const },
])('$name', ({ useHook, apiFn }) => {
  it('calls the API function with no variable of its own, and invalidates the root', async () => {
    api[apiFn].mockResolvedValue({ ok: true });
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');

    const { result } = renderHook(() => useHook(), { wrapper });
    result.current.mutate();

    await waitFor(() => expect(api[apiFn]).toHaveBeenCalled());
    expect(api[apiFn].mock.calls[0][0]).toBeUndefined();

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: investmentKeys.root })
    );
  });
});

describe.each(ROOT_INVALIDATING_MUTATIONS)(
  '$name',
  ({ useHook, apiFn, variables, expectedCallArgs }) => {
    it('calls the API function with the right arguments and invalidates the root on success', async () => {
      api[apiFn].mockResolvedValue({ ok: true });
      const invalidateSpy = vi.spyOn(client, 'invalidateQueries');

      const { result } = renderHook(() => useHook(), { wrapper });

      result.current.mutate(variables as never);

      await waitFor(() => expect(api[apiFn]).toHaveBeenCalled());

      // Sliced to the leading arguments rather than a full toHaveBeenCalledWith:
      // several of these hooks pass `apiFn` straight through as `mutationFn`
      // rather than wrapping it in a lambda, and TanStack Query v5 always
      // invokes a mutationFn as `(variables, context)` -- so a direct-reference
      // hook's underlying call carries a trailing QueryClient/meta/mutationKey
      // object the api function itself declares no parameter for and ignores.
      // Comparing only the arguments this test actually has an opinion about
      // is what keeps that implementation detail from being asserted on by
      // accident.
      const actualArgs = api[apiFn].mock.calls[0].slice(0, expectedCallArgs.length);
      expect(actualArgs).toEqual(expectedCallArgs);

      await waitFor(() =>
        expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: investmentKeys.root })
      );
    });
  }
);

describe('useRefreshValuations', () => {
  it('defaults force to false when the caller passes nothing', async () => {
    api.refreshValuations.mockResolvedValue({ ok: true });
    const { result } = renderHook(() => useRefreshValuations(), { wrapper });

    result.current.mutate(undefined);

    await waitFor(() => expect(api.refreshValuations).toHaveBeenCalledWith(false));
  });

  it('passes an explicit true through unchanged', async () => {
    api.refreshValuations.mockResolvedValue({ ok: true });
    const { result } = renderHook(() => useRefreshValuations(), { wrapper });

    result.current.mutate(true);

    await waitFor(() => expect(api.refreshValuations).toHaveBeenCalledWith(true));
  });

  it('invalidates the root, same as every other mutation in the book', async () => {
    api.refreshValuations.mockResolvedValue({ ok: true });
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    const { result } = renderHook(() => useRefreshValuations(), { wrapper });

    result.current.mutate(undefined);

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: investmentKeys.root })
    );
  });
});

describe('useSectorColors', () => {
  it('reads from the sector-colors key', async () => {
    const colors = [{ sector: 'Technology', color: '#123456' }];
    api.getSectorColors.mockResolvedValue(colors);

    const { result } = renderHook(() => useSectorColors(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBe(colors);
  });
});

/**
 * The two sector-colour mutations, which are the deliberate exception: they
 * invalidate `sectorColors`, NOT `root` -- a palette pick does not change the
 * portfolio's money figures, so refetching the whole book for it would be
 * pure waste. Kept separate from the table above rather than folded in, so
 * that distinction stays visible as two tests instead of one shared branch.
 */
describe('useSetSectorColor', () => {
  it('calls the API with sector and colour, and invalidates sectorColors only', async () => {
    api.setSectorColor.mockResolvedValue({ sector: 'Technology', color: '#123456' });
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');

    const { result } = renderHook(() => useSetSectorColor(), { wrapper });
    result.current.mutate({ sector: 'Technology', color: '#123456' });

    await waitFor(() =>
      expect(api.setSectorColor).toHaveBeenCalledWith('Technology', '#123456')
    );
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: investmentKeys.sectorColors,
      })
    );
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: investmentKeys.root });
  });
});

describe('useDeleteSectorColor', () => {
  it('calls the API with the sector, and invalidates sectorColors only', async () => {
    api.deleteSectorColor.mockResolvedValue(undefined);
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');

    const { result } = renderHook(() => useDeleteSectorColor(), { wrapper });
    result.current.mutate('Technology');

    // `deleteSectorColor` is `mutationFn`'s direct value here, so TanStack
    // Query calls it as `(variables, context)` -- checked against just the
    // leading argument for the same reason as the table above.
    await waitFor(() => expect(api.deleteSectorColor).toHaveBeenCalled());
    expect(api.deleteSectorColor.mock.calls[0][0]).toBe('Technology');
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({
        queryKey: investmentKeys.sectorColors,
      })
    );
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: investmentKeys.root });
  });
});
