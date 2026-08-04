/**
 * HTTP for the long-term book.
 *
 * Shares `apiClient` with the trading journal — that carries the base URL and
 * the Clerk token interceptor, which is infrastructure rather than trading
 * logic — but nothing else. Kept in its own module so the two books' request
 * layers cannot drift into each other.
 */

import { apiClient } from './api';
import type {
  Holding,
  HoldingPayload,
  InvestmentTransaction,
  Portfolio,
  PriceRefreshResult,
  RefreshResult,
  TransactionPayload,
  ValuationInputRow,
  ValuationOverridePayload,
} from '@/types/investments';

/** GET /api/investments/portfolio — every holding, valued, in one call. */
export async function getPortfolio(): Promise<Portfolio> {
  const { data } = await apiClient.get<Portfolio>('/investments/portfolio');
  return data;
}

/** GET /api/investments/transactions — the ledger, newest first. */
export async function getInvestmentTransactions(
  ticker?: string
): Promise<InvestmentTransaction[]> {
  const { data } = await apiClient.get<InvestmentTransaction[]>(
    '/investments/transactions',
    { params: ticker ? { ticker } : undefined }
  );
  return data;
}

export async function createInvestmentTransaction(
  payload: TransactionPayload
): Promise<InvestmentTransaction> {
  const { data } = await apiClient.post<InvestmentTransaction>(
    '/investments/transactions',
    payload
  );
  return data;
}

export async function deleteInvestmentTransaction(id: string): Promise<void> {
  await apiClient.delete(`/investments/transactions/${id}`);
}

export async function createHolding(payload: HoldingPayload): Promise<Holding> {
  const { data } = await apiClient.post<Holding>('/investments/holdings', payload);
  return data;
}

export async function updateHolding(
  ticker: string,
  payload: Partial<HoldingPayload>
): Promise<Holding> {
  const { data } = await apiClient.patch<Holding>(
    `/investments/holdings/${ticker}`,
    payload
  );
  return data;
}

export async function deleteHolding(ticker: string): Promise<void> {
  await apiClient.delete(`/investments/holdings/${ticker}`);
}

/**
 * PUT — the modal edits every input at once, so a partial merge would make
 * "I cleared this" and "I did not touch this" the same request.
 */
export async function setValuationOverride(
  ticker: string,
  payload: ValuationOverridePayload
): Promise<ValuationInputRow> {
  const { data } = await apiClient.put<ValuationInputRow>(
    `/investments/holdings/${ticker}/valuation-override`,
    payload
  );
  return data;
}

/** Discard every override, returning the ticker to fetched inputs. */
export async function clearValuationOverride(ticker: string): Promise<void> {
  await apiClient.delete(`/investments/holdings/${ticker}/valuation-override`);
}

/** One FMP call per holding. Cheap enough to run daily. */
export async function refreshPrices(): Promise<PriceRefreshResult> {
  const { data } = await apiClient.post<PriceRefreshResult>(
    '/investments/refresh-prices',
    null,
    { timeout: 120_000 }
  );
  return data;
}

/**
 * The monthly path, and slow on purpose: ~4 provider calls per holding plus a
 * throttled Finviz request every five seconds, so a thirteen-name book takes
 * about ninety seconds. The timeout is generous for that reason.
 */
export async function refreshValuations(force = false): Promise<RefreshResult> {
  const { data } = await apiClient.post<RefreshResult>(
    '/investments/refresh',
    null,
    { params: { force }, timeout: 300_000 }
  );
  return data;
}
