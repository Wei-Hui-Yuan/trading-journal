import axios from 'axios';

import type {
  AdvancedMetrics,
  AppSettings,
  AppSettingsPayload,
  DashboardStats,
  ExecutionUpdatePayload,
  ExecutionUpdateResult,
  PositionDeleteResult,
  SuppressedExecution,
  UnsuppressResult,
  Discipline,
  DisciplineCreatePayload,
  IngestResult,
  ManualTradePayload,
  ManualTradeResult,
  PlanAttachResult,
  PlanDetachResult,
  PlanStatus,
  Position,
  PositionFill,
  PositionReviewPayload,
  RoundTrip,
  TradePlan,
  TradePlanPayload,
  TradePlanUpdatePayload,
  Strategy,
  StrategyCreatePayload,
  StrategyUpdatePayload,
  Trade,
  TradeDeleteResult,
  TradeAnnotationPayload,
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
      } else if (!error.response) {
        // No response object at all means the browser never let us see one:
        // the API is genuinely unreachable, OR it answered without CORS
        // headers. Axios calls both "Network Error", which reads as "the
        // server is down" and hides the second case entirely. Naming the
        // target is what makes this debuggable -- a stale or missing
        // NEXT_PUBLIC_API_URL is invisible otherwise.
        const target = error.config?.baseURL ?? 'the API';
        error.message = `Could not reach ${target}. The server may be down, or it responded with an error that carried no CORS headers.`;
      }
    }
    return Promise.reject(error);
  }
);

/**
 * The HTTP status behind a failed request, when the server answered at all.
 *
 * Null means no response reached us — unreachable host, or a reply stripped of
 * CORS headers. That distinction is worth keeping: "500" and "never answered"
 * point at completely different problems.
 */
export function httpStatusOf(error: unknown): number | null {
  return axios.isAxiosError(error) ? (error.response?.status ?? null) : null;
}

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

/**
 * GET /api/trades — every execution, newest first.
 *
 * The master list. `positions` holds only closed round trips, so an unsold
 * buy has no row there; this is the only view that shows it.
 */
export async function getTrades(ticker?: string): Promise<Trade[]> {
  const { data } = await apiClient.get<Trade[]>('/trades', {
    params: ticker ? { ticker } : undefined,
  });
  return data;
}

/**
 * GET /api/round-trips — the journal, one row per trade idea.
 *
 * Prefer this over getTrades() for anything user-facing: /trades returns raw
 * executions, so a scale-in reads as several unrelated rows.
 */
export async function getRoundTrips(ticker?: string): Promise<RoundTrip[]> {
  const { data } = await apiClient.get<RoundTrip[]>('/round-trips', {
    params: ticker ? { ticker } : undefined,
  });
  return data;
}

/** PATCH /api/trades/{id} — attach a strategy or thesis to an execution. */
export async function annotateTrade(
  id: string,
  payload: TradeAnnotationPayload
): Promise<Trade> {
  const { data } = await apiClient.patch<Trade>(`/trades/${id}`, payload);
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

/**
 * PATCH /api/trades/{id}/execution - correct the facts of a fill.
 *
 * Separate from annotateTrade, which locks these fields. The backend re-runs
 * FIFO afterwards, so the response reports what that rebuild removed.
 */
export async function updateExecution(
  id: string,
  payload: ExecutionUpdatePayload
): Promise<ExecutionUpdateResult> {
  const { data } = await apiClient.patch<ExecutionUpdateResult>(
    `/trades/${id}/execution`,
    payload
  );
  return data;
}

/**
 * DELETE /api/positions/{id} - remove a round trip AND its executions.
 *
 * For a trade that never happened. Broker fills are tombstoned so the next
 * sync cannot re-add them.
 */
export async function deletePosition(
  id: string,
  reason?: string
): Promise<PositionDeleteResult> {
  const { data } = await apiClient.delete<PositionDeleteResult>(
    `/positions/${id}`,
    { params: reason ? { reason } : undefined }
  );
  return data;
}

/** POST /api/positions/{id}/dismiss - leave the queue, keep the P&L. */
export async function dismissPosition(id: string): Promise<Position> {
  const { data } = await apiClient.post<Position>(`/positions/${id}/dismiss`);
  return data;
}

/** GET /api/trades/suppressed - broker fills ingest is deliberately skipping. */
export async function getSuppressedExecutions(): Promise<SuppressedExecution[]> {
  const { data } = await apiClient.get<SuppressedExecution[]>('/trades/suppressed');
  return data;
}

/**
 * DELETE /api/trades/suppressed/{id} - lift a tombstone.
 *
 * Keyed by the BROKER id, not a trade id: suppression exists because the trade
 * row was deleted, so there is no trade to address. This restores nothing on
 * its own — the fill returns only when a sync next covers its date.
 */
export async function unsuppressExecution(
  execId: string
): Promise<UnsuppressResult> {
  const { data } = await apiClient.delete<UnsuppressResult>(
    `/trades/suppressed/${encodeURIComponent(execId)}`
  );
  return data;
}

/**
 * GET /api/plans - trade plans, newest first.
 *
 * Defaults to OPEN because that is the only actionable status: the dock exists
 * to show what you are still waiting to be filled on. Pass 'ALL' for history.
 */
export async function getPlans(
  status: PlanStatus | 'ALL' = 'OPEN'
): Promise<TradePlan[]> {
  const { data } = await apiClient.get<TradePlan[]>('/plans', {
    params: { status },
  });
  return data;
}

/**
 * POST /api/plans - record a trade you intend to take.
 *
 * Writes to `planned_trades` and nowhere else. No row reaches `trades`, so
 * nothing here moves P&L, win rate or exposure until a real fill arrives.
 */
export async function createPlan(payload: TradePlanPayload): Promise<TradePlan> {
  const { data } = await apiClient.post<TradePlan>('/plans', payload);
  return data;
}

/** PATCH /api/plans/{id} - edit a plan that has not been attached yet. */
export async function updatePlan(
  planId: string,
  payload: TradePlanUpdatePayload
): Promise<TradePlan> {
  const { data } = await apiClient.patch<TradePlan>(`/plans/${planId}`, payload);
  return data;
}

/**
 * DELETE /api/plans/{id} - cancel a plan you did not take.
 *
 * Marked CANCELLED rather than removed: the setups you talked yourself out of
 * are evidence about your process, and a row that vanishes takes that with it.
 */
export async function cancelPlan(planId: string): Promise<TradePlan> {
  const { data } = await apiClient.delete<TradePlan>(`/plans/${planId}`);
  return data;
}

/**
 * POST /api/trades/{id}/attach-plan - link a plan the sync did not match.
 *
 * Attaches to every fill in the same opening leg, not only the one named.
 */
export async function attachPlan(
  tradeId: string,
  planId: string
): Promise<PlanAttachResult> {
  const { data } = await apiClient.post<PlanAttachResult>(
    `/trades/${tradeId}/attach-plan`,
    null,
    { params: { plan_id: planId } }
  );
  return data;
}

/** POST /api/trades/{id}/detach-plan - unlink, returning the plan to OPEN. */
export async function detachPlan(tradeId: string): Promise<PlanDetachResult> {
  const { data } = await apiClient.post<PlanDetachResult>(
    `/trades/${tradeId}/detach-plan`
  );
  return data;
}

/** GET /api/settings - account size and default risk for the calculator. */
export async function getSettings(): Promise<AppSettings> {
  const { data } = await apiClient.get<AppSettings>('/settings');
  return data;
}

/** PUT /api/settings - partial; only the keys sent are applied. */
export async function updateSettings(
  payload: AppSettingsPayload
): Promise<AppSettings> {
  const { data } = await apiClient.put<AppSettings>('/settings', payload);
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

/** DELETE /api/trades/{id} — delete an execution fill from the ledger. */
export async function deleteTrade(id: string): Promise<TradeDeleteResult> {
  const { data } = await apiClient.delete<TradeDeleteResult>(`/trades/${id}`);
  return data;
}

/** GET /api/disciplines — list all discipline rules. */
export async function getDisciplines(): Promise<Discipline[]> {
  const { data } = await apiClient.get<Discipline[]>('/disciplines');
  return data;
}

/** POST /api/disciplines — add a new discipline rule. */
export async function createDiscipline(
  payload: DisciplineCreatePayload
): Promise<Discipline> {
  const { data } = await apiClient.post<Discipline>('/disciplines', payload);
  return data;
}

/** DELETE /api/disciplines/{id} — delete a discipline rule. */
export async function deleteDiscipline(id: string): Promise<void> {
  await apiClient.delete(`/disciplines/${id}`);
}
