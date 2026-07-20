import axios from 'axios';

import { Trade, KPIStats, HeatmapCell } from '@/types/trade';
import type {
  DashboardStats,
  Position,
  PositionReviewPayload,
  Strategy,
  StrategyCreatePayload,
} from '@/types/api';

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export interface APITrade {
  id: number;
  symbol: string;
  side: string;
  entry_price: number | null;
  exit_price: number | null;
  quantity: number | null;
  status: string;
  notes: number | string | null;
}

export interface CompleteTradeParams {
  exit_price: number;
  quantity?: number;
  notes?: string;
}

// Convert raw API response into frontend Trade blueprint format
export function mapAPITradeToFrontend(apiTrade: APITrade): Trade {
  const isLong = (apiTrade.side || 'LONG').toUpperCase() === 'LONG';
  return {
    id: String(apiTrade.id),
    ibkr_exec_id: `EXEC-${apiTrade.id}`,
    ticker: apiTrade.symbol || 'UNKNOWN',
    direction: isLong ? 'LONG' : 'SHORT',
    style: 'Intraday Execution',
    status: (apiTrade.status === 'completed' ? 'completed' : 'pending_review'),
    entry_date: new Date().toISOString().replace('T', ' ').substring(0, 16),
    actual_entry: apiTrade.entry_price ?? 0,
    exit_price: apiTrade.exit_price ?? null,
    quantity: apiTrade.quantity ?? 100,
    risk_percent: 1.0,
    source_tag: 'FastAPI / Supabase Live',
    lessons_comments: typeof apiTrade.notes === 'string' ? apiTrade.notes : '',
    hard_sl_set: true,
    waited_retest: true,
    followed_plan: true,
    created_at: new Date().toISOString(),
  };
}

// GET or POST /api/trades
export async function fetchTradesFromAPI(): Promise<Trade[]> {
  try {
    const res = await fetch(`${API_BASE_URL}/api/trades`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
    });

    if (!res.ok) {
      throw new Error(`API error: ${res.status} ${res.statusText}`);
    }

    const data = await res.json();
    
    // Flatten grouped dict response if { "pending_review": [...], "completed": [...] }
    let rawTrades: APITrade[] = [];
    if (Array.isArray(data)) {
      rawTrades = data;
    } else if (typeof data === 'object' && data !== null) {
      Object.values(data).forEach((group) => {
        if (Array.isArray(group)) {
          rawTrades.push(...group);
        }
      });
    }

    return rawTrades.map(mapAPITradeToFrontend);
  } catch (error) {
    console.warn('Failed to fetch from live API, falling back:', error);
    throw error;
  }
}

// PUT /api/trades/{id}
export async function completeTradeAPI(
  tradeId: string | number,
  params: CompleteTradeParams
): Promise<Trade> {
  const numericId = typeof tradeId === 'number' ? tradeId : parseInt(tradeId, 10);
  
  const res = await fetch(`${API_BASE_URL}/api/trades/${numericId}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      exit_price: params.exit_price,
      quantity: params.quantity,
      notes: params.notes,
    }),
  });

  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    throw new Error(errData.detail || `Failed to complete trade ${tradeId}: ${res.status}`);
  }

  const updatedAPITrade: APITrade = await res.json();
  return mapAPITradeToFrontend(updatedAPITrade);
}

// Shape returned by POST /api/sync/ibkr
export interface SyncResult {
  reference_code: string;
  fills_found: number;
  inserted: number;
  skipped_duplicates: number;
  skipped_unparseable: number;
  inserted_trade_ids: string[];
}

// POST /api/sync/ibkr - pulls fresh executions from the IBKR Flex service
export async function syncBrokerAPI(): Promise<SyncResult> {
  const res = await fetch(`${API_BASE_URL}/api/sync/ibkr`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
  });

  if (!res.ok) {
    const errData = await res.json().catch(() => ({}));
    throw new Error(errData.detail || `Broker sync failed: ${res.status} ${res.statusText}`);
  }

  return res.json();
}

// Helper to compute live KPI stats from array of trades
export function computeKPIStats(trades: Trade[]): KPIStats {
  const completedTrades = trades.filter((t) => t.status === 'completed' && t.exit_price !== null);
  const pendingCount = trades.filter((t) => t.status === 'pending_review').length;

  let totalWinPnl = 0;
  let totalLossPnl = 0;
  let winsCount = 0;
  let netPnl = 0;

  completedTrades.forEach((t) => {
    const exit = t.exit_price ?? t.actual_entry;
    const diff = t.direction === 'LONG' ? exit - t.actual_entry : t.actual_entry - exit;
    const tradePnl = diff * t.quantity;

    netPnl += tradePnl;
    if (tradePnl > 0) {
      totalWinPnl += tradePnl;
      winsCount++;
    } else if (tradePnl < 0) {
      totalLossPnl += Math.abs(tradePnl);
    }
  });

  const totalTrades = completedTrades.length;
  const winRate = totalTrades > 0 ? parseFloat(((winsCount / totalTrades) * 100).toFixed(1)) : 0;
  const profitFactor = totalLossPnl > 0 ? parseFloat((totalWinPnl / totalLossPnl).toFixed(2)) : totalWinPnl > 0 ? 9.99 : 1.0;
  const avgRoi = totalTrades > 0 ? parseFloat((netPnl / totalTrades / 100).toFixed(2)) : 0;

  return {
    netPnl,
    winRate,
    totalTrades: trades.length,
    profitFactor,
    avgRoi,
    pendingCount,
  };
}

// ===========================================================================
// Typed API client (Phase 1)
// ===========================================================================
//
// The fetch-based helpers above target the legacy `trades` endpoints and are
// still used by the current dashboard. Everything below is the axios client
// for the positions/strategies/analytics layer.

/** Axios instance pointed at the FastAPI router root. */
export const apiClient = axios.create({
  baseURL: `${API_BASE_URL}/api`,
  headers: { 'Content-Type': 'application/json' },
  timeout: 30_000,
});

/**
 * Normalize errors into something renderable.
 *
 * FastAPI puts its message in `detail`; without this, a failed request
 * surfaces as a generic "Request failed with status code 4xx".
 */
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (axios.isAxiosError(error)) {
      const detail = (error.response?.data as { detail?: string } | undefined)?.detail;
      if (detail) {
        error.message = detail;
      }
    }
    return Promise.reject(error);
  }
);

/** GET /api/positions?review_status=pending — the Trade Inbox queue. */
export async function getPendingPositions(): Promise<Position[]> {
  const { data } = await apiClient.get<Position[]>('/positions', {
    params: { review_status: 'pending' },
  });
  return data;
}

/** GET /api/positions — every closed position, newest first. */
export async function getPositions(reviewStatus?: string): Promise<Position[]> {
  const { data } = await apiClient.get<Position[]>('/positions', {
    params: reviewStatus ? { review_status: reviewStatus } : undefined,
  });
  return data;
}

/** GET /api/strategies */
export async function getStrategies(): Promise<Strategy[]> {
  const { data } = await apiClient.get<Strategy[]>('/strategies');
  return data;
}

/** POST /api/strategies — rejects with the API's detail on a duplicate name. */
export async function createStrategy(
  payload: StrategyCreatePayload
): Promise<Strategy> {
  const { data } = await apiClient.post<Strategy>('/strategies', payload);
  return data;
}

/** GET /api/analytics/dashboard — core stats plus the heatmap grid. */
export async function getDashboardAnalytics(): Promise<DashboardStats> {
  const { data } = await apiClient.get<DashboardStats>('/analytics/dashboard');
  return data;
}

/**
 * PATCH /api/positions/{id}/review
 *
 * Only the keys present in `payload` are applied; the backend always sets
 * review_status to 'completed' on success.
 */
export async function updatePositionReview(
  id: string,
  payload: PositionReviewPayload
): Promise<Position> {
  const { data } = await apiClient.patch<Position>(
    `/positions/${id}/review`,
    payload
  );
  return data;
}
