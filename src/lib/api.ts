import axios from 'axios';

import type {
  DashboardStats,
  Position,
  PositionReviewPayload,
  Strategy,
  StrategyCreatePayload,
} from '@/types/api';

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

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

// ===========================================================================
// Typed API client (Phase 1)
// ===========================================================================
//
// `syncBrokerAPI` above still uses fetch directly; everything below is the
// axios client for the positions/strategies/analytics layer.

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

/** GET /api/positions?review_status=pending â€” the Trade Inbox queue. */
export async function getPendingPositions(): Promise<Position[]> {
  const { data } = await apiClient.get<Position[]>('/positions', {
    params: { review_status: 'pending' },
  });
  return data;
}

/** GET /api/positions â€” every closed position, newest first. */
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

/** POST /api/strategies â€” rejects with the API's detail on a duplicate name. */
export async function createStrategy(
  payload: StrategyCreatePayload
): Promise<Strategy> {
  const { data } = await apiClient.post<Strategy>('/strategies', payload);
  return data;
}

/** GET /api/analytics/dashboard â€” core stats plus the heatmap grid. */
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
