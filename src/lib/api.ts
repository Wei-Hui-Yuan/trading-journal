import axios from 'axios';

import type {
  AdvancedMetrics,
  DashboardStats,
  IngestResult,
  ManualTradePayload,
  ManualTradeResult,
  Position,
  PositionFill,
  PositionReviewPayload,
  Strategy,
  StrategyCreatePayload,
  StrategyUpdatePayload,
} from '@/types/api';

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

declare global {
  interface Window {
    Clerk?: {
      loaded?: boolean;
      session?: {
        getToken: () => Promise<string | null>;
      };
    };
  }
}

/**
 * Ceiling on how long we'll wait for a session token.
 *
 * Axios's own `timeout` covers the HTTP request only, NOT time spent inside a
 * request interceptor -- so without an explicit bound here, a Clerk call that
 * never settles leaves the request undispatched and the caller pending
 * forever. An error is recoverable; a spinner that never resolves is not.
 */
const TOKEN_TIMEOUT_MS = 5_000;

// ===========================================================================
// Typed API client (Phase 1)
// ===========================================================================
//
// Axios client for the positions / strategies / analytics / ingest layer.

/** Axios instance pointed at the FastAPI router root. */
export const apiClient = axios.create({
  baseURL: `${API_BASE_URL}/api`,
  headers: { 'Content-Type': 'application/json' },
  timeout: 30_000,
});

/**
 * Read the current Clerk session token, or null when signed out.
 *
 * clerk-js loads asynchronously, so a query firing on mount can easily beat
 * it. Returning early in that window would send an unauthenticated request
 * and surface a spurious 401, so wait for `loaded` first -- but only within
 * the deadline, never indefinitely.
 */
async function getSessionToken(deadline: number): Promise<string | null> {
  while (!window.Clerk?.loaded) {
    if (Date.now() > deadline) {
      throw new Error('Timed out waiting for the authentication session to load');
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }

  const session = window.Clerk?.session;
  if (!session) {
    return null; // Signed out: let the request go and the API answer 401.
  }
  return session.getToken();
}

/**
 * Attach the active Clerk session JWT as a Bearer token.
 *
 * This module is called from React Query hooks, not components, so the
 * `useAuth()` hook isn't available here -- `window.Clerk` is Clerk's
 * documented escape hatch for reaching the session outside of React.
 */
apiClient.interceptors.request.use(async (config) => {
  if (typeof window === 'undefined') {
    return config;
  }

  const deadline = Date.now() + TOKEN_TIMEOUT_MS;
  const token = await Promise.race([
    getSessionToken(deadline),
    new Promise<never>((_, reject) =>
      setTimeout(
        () => reject(new Error('Timed out retrieving the authentication token')),
        TOKEN_TIMEOUT_MS
      )
    ),
  ]);

  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
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

/**
 * GET /api/positions/{id}/fills - the executions behind one round trip.
 *
 * Fetched on demand rather than embedded in the positions list: the inbox
 * renders fine without it, and most positions are never expanded.
 */
export async function getPositionFills(id: string): Promise<PositionFill[]> {
  const { data } = await apiClient.get<PositionFill[]>(`/positions/${id}/fills`);
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

/** PATCH /api/strategies/{id} - save playbook edits. */
export async function updateStrategy(
  id: string,
  payload: StrategyUpdatePayload
): Promise<Strategy> {
  const { data } = await apiClient.patch<Strategy>(`/strategies/${id}`, payload);
  return data;
}

/**
 * POST /api/trades/manual - hand-log an execution.
 *
 * The backend inserts it into `trades` like a synced fill, then re-runs FIFO
 * matching for that ticker, so positions appear without a broker sync.
 */
export async function createManualTrade(
  payload: ManualTradePayload
): Promise<ManualTradeResult> {
  const { data } = await apiClient.post<ManualTradeResult>(
    '/trades/manual',
    payload
  );
  return data;
}

/**
 * POST /api/ingest/ibkr - pull executions from IBKR.
 *
 * Replaces the retired /api/sync/ibkr. Fills are staged in `ibkr_executions`
 * keyed by the broker's transaction id before promotion, so re-running over an
 * overlapping date range is a no-op rather than a duplicate.
 */
export async function ingestIBKR(): Promise<IngestResult> {
  const { data } = await apiClient.post<IngestResult>('/ingest/ibkr');
  return data;
}

/** GET /api/analytics/advanced - R-multiples, slippage, expectancy. */
export async function getAdvancedMetrics(): Promise<AdvancedMetrics> {
  const { data } = await apiClient.get<AdvancedMetrics>('/analytics/advanced');
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
  const { data } = await apiClient.put<Position>(
    `/positions/${id}/review`,
    payload
  );
  return data;
}
