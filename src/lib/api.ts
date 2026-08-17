import axios from 'axios';

import type {
  AdvancedMetrics,
  AuditResult,
  AppSettings,
  AppSettingsPayload,
  DashboardStats,
  ExecutionUpdatePayload,
  ExecutionUpdateResult,
  PositionDeleteImpact,
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
  StrategyDeleteResult,
  StrategyUpdatePayload,
  SyncStatus,
  Trade,
  TradeDeleteResult,
  TradeAnnotationPayload,
  TimeframePreset,
  TimeframePresetPayload,
  TimeframeSelection,
} from '@/types/api';
import type {
  SizingEntryPayload,
  SizingEntryUpdatePayload,
  SizingScratchpadEntry,
} from '@/types/sizing';

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

/** One entry of FastAPI's 422 body: `loc` is the path to the offending field. */
interface ValidationDetail {
  loc?: (string | number)[];
  msg?: string;
}

/**
 * Flatten FastAPI's `detail` into a string, whatever shape it arrived in.
 *
 * `HTTPException` sends a string, but a Pydantic failure sends an ARRAY of
 * objects, and the difference is invisible until one reaches a component:
 * `{error.message}` on an array of objects is React error #31, which unmounts
 * the tree and replaces the whole page with "Application error". A rejected
 * field is the most ordinary thing a form can do — it must never be able to
 * take the app down, so the array is collapsed here, at the one place every
 * request already passes through, rather than defended against at each
 * `setError` call site.
 */
function messageFromDetail(detail: unknown): string | null {
  if (typeof detail === 'string') {
    return detail;
  }
  if (Array.isArray(detail)) {
    const parts = (detail as ValidationDetail[])
      .map((entry) => {
        // Pydantic prefixes every custom `raise ValueError(...)` with
        // "Value error, ". The sentence after it was written to be read by a
        // person; the prefix was not.
        const msg =
          typeof entry?.msg === 'string' ? entry.msg.replace(/^Value error, /, '') : null;
        if (!msg) return null;
        // Drop the leading "body"/"query" segment: it names the part of the
        // request, not the field the user typed into.
        const field = (entry.loc ?? []).slice(1).join('.');
        return field ? `${field}: ${msg}` : msg;
      })
      .filter((part): part is string => part !== null);
    return parts.length ? parts.join('; ') : null;
  }
  return null;
}

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
      const detail = messageFromDetail(
        (error.response?.data as { detail?: unknown } | undefined)?.detail
      );
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

/** What the journal can ask the server to narrow by, and how much to send. */
export interface RoundTripQuery {
  /** Exact symbol match. */
  ticker?: string;
  /** Substring match on the symbol — what the search box sends. */
  search?: string;
  /** One half of the journal, or both when omitted. */
  kind?: 'open' | 'closed';
  /** A strategy id, or 'unassigned' for round trips carrying none. */
  strategy?: string;
  /** Page size, applied to CLOSED round trips only. */
  limit?: number;
  offset?: number;
}

/**
 * GET /api/round-trips — the journal, one row per trade idea.
 *
 * Prefer this over getTrades() for anything user-facing: /trades returns raw
 * executions, so a scale-in reads as several unrelated rows.
 *
 * Filtering happens SERVER-SIDE. Doing it here would only ever narrow the rows
 * already fetched, which is correct while the whole ledger is in memory and
 * quietly wrong the moment it is paged — a search would find matches on the
 * current page and miss identical ones on the next.
 *
 * `limit`/`offset` page the closed half only; open exposure always arrives
 * whole, because it is the half that needs decisions.
 */
export async function getRoundTrips(
  query: RoundTripQuery = {}
): Promise<RoundTrip[]> {
  // Empty strings are omitted rather than sent: `?search=` would otherwise
  // reach the API as a filter for the empty string and occupy its own cache
  // entry, distinct from the unfiltered one that returns the same rows.
  const params: Record<string, string | number> = {};
  if (query.ticker) params.ticker = query.ticker;
  if (query.search) params.search = query.search;
  if (query.kind) params.kind = query.kind;
  if (query.strategy) params.strategy = query.strategy;
  if (query.limit !== undefined) params.limit = query.limit;
  if (query.offset) params.offset = query.offset;

  const { data } = await apiClient.get<RoundTrip[]>('/round-trips', { params });
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
  reason?: string,
  includeShared = false
): Promise<PositionDeleteResult> {
  const { data } = await apiClient.delete<PositionDeleteResult>(
    `/positions/${id}`,
    {
      params: {
        ...(reason ? { reason } : {}),
        // Without this the API returns 409 whenever an execution under this
        // round trip also belongs to another one. Only set once the user has
        // been shown which, via getPositionDeleteImpact.
        ...(includeShared ? { include_shared: true } : {}),
      },
    }
  );
  return data;
}

/**
 * GET /api/positions/{id}/delete-impact — what a delete would remove.
 *
 * Read-only, and fetched before the confirmation prompt rather than after it.
 * Deleting a round trip deletes its executions, and an execution can be shared
 * with the round trip beside it, so "are you sure?" is only an honest question
 * once it can name what else disappears.
 */
export async function getPositionDeleteImpact(
  id: string
): Promise<PositionDeleteImpact> {
  const { data } = await apiClient.get<PositionDeleteImpact>(
    `/positions/${id}/delete-impact`
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
 * POST /api/plans/{id}/chart - attach or replace the chart screenshot.
 *
 * The image is compressed in the browser first (see lib/chartImage.ts); this
 * only carries the result. Re-uploading replaces in place rather than
 * accumulating a new object per correction.
 */
export async function uploadPlanChart(
  planId: string,
  image: Blob,
  filename: string
): Promise<TradePlan> {
  const form = new FormData();
  form.append('file', image, filename);
  const { data } = await apiClient.post<TradePlan>(`/plans/${planId}/chart`, form, {
    // Explicitly unset, so the browser writes its own value WITH the multipart
    // boundary token. Axios's instance default of application/json would
    // otherwise ride along and the server would fail to parse the body.
    headers: { 'Content-Type': undefined },
    // A screenshot is orders of magnitude larger than any JSON this client
    // sends, and it makes two network hops (here, then on to Storage).
    timeout: 60_000,
  });
  return data;
}

/**
 * GET /api/plans/{id}/chart - the screenshot itself.
 *
 * Fetched as a blob through the authenticated client rather than pointed at
 * with a bare <img src>, because the endpoint requires the Clerk bearer token
 * and an <img> tag cannot carry one. The caller turns this into an object URL.
 */
export async function getPlanChart(planId: string): Promise<Blob> {
  const { data } = await apiClient.get<Blob>(`/plans/${planId}/chart`, {
    responseType: 'blob',
    timeout: 60_000,
  });
  return data;
}

/** DELETE /api/plans/{id}/chart - detach the screenshot, keeping the plan. */
export async function deletePlanChart(planId: string): Promise<TradePlan> {
  const { data } = await apiClient.delete<TradePlan>(`/plans/${planId}/chart`);
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
 * DELETE /api/strategies/{id} — remove a playbook entry.
 *
 * `reassignTo` is required by the server whenever anything references the
 * strategy; omitting it returns a 409 naming the counts. An unused strategy
 * deletes without one.
 */
export async function deleteStrategy(
  id: string,
  reassignTo?: string | null
): Promise<StrategyDeleteResult> {
  const { data } = await apiClient.delete<StrategyDeleteResult>(
    `/strategies/${id}`,
    { params: reassignTo ? { reassign_to: reassignTo } : undefined }
  );
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
  const { data } = await apiClient.post<IngestResult>('/ingest/ibkr', null, {
    // The 30s default cannot work here, and not marginally: IBKR compiles a
    // Flex report on demand, so the backend SENDS a request and then POLLS for
    // the payload. Its own budget for a single query is up to 3 send attempts
    // (2 x 5s backoff) plus 5 polls (4 x 4s backoff), each with a 30s HTTP
    // timeout -- roughly 266s before it gives up, all of it legitimate.
    //
    // Widening the Flex window to 365 days made this reachable rather than
    // theoretical: the first such sync timed out client-side at 30s and
    // reported "no response from the server" for a request that had not
    // failed. Nothing was written, so the ledger stayed correct, but the run
    // was wasted and IBKR had already been asked to build the report.
    //
    // This number is now the OUTER half of a pair, and must stay above the
    // server's own ceiling rather than guessing at one. IBKR_QUERY_ID takes a
    // list, and two queries is the documented configuration, so the honest
    // worst case here was ~535s against this 300s -- a sync the browser
    // abandoned while the server was still legitimately working, reported to
    // the user as "no response". ibkr_client.TOTAL_BUDGET_SECONDS (240s) now
    // bounds the fetch server-side and turns an over-run into an ordinary
    // partial result in `queries_failed`, which the sync toast already
    // renders. Keep the 60s of slack: it covers the staging, promotion and
    // FIFO matching that run after the fetch.
    //
    // The real fix is still for ingest to return 202 with a job id and be
    // polled -- a request whose duration is bounded by a third party's compile
    // time does not belong in a synchronous round trip.
    timeout: 300_000,
  });
  return data;
}

/**
 * GET /api/sync/runs/latest — the last sync, and the last one that worked.
 *
 * The durable counterpart to `LastSyncState`, which only ever knew about syncs
 * this tab performed and so could not see a scheduled run at all.
 */
export async function getSyncStatus(): Promise<SyncStatus> {
  const { data } = await apiClient.get<SyncStatus>('/sync/runs/latest');
  return data;
}

/** GET /api/analytics/advanced - R-multiples, slippage, expectancy. */
export async function getAdvancedMetrics(
  selection?: TimeframeSelection
): Promise<AdvancedMetrics> {
  // Same shape the dashboard sends, because the endpoint takes the same three
  // parameters with the same precedence. Omitted entirely when there is no
  // selection, which lets the server apply its own default rather than the
  // browser holding a second opinion about what "1Y" means.
  const params =
    selection?.kind === 'custom'
      ? { start_date: selection.start_date, end_date: selection.end_date }
      : selection
        ? { preset: selection.preset }
        : undefined;

  const { data } = await apiClient.get<AdvancedMetrics>('/analytics/advanced', {
    params,
  });
  return data;
}

/**
 * GET /api/analytics/dashboard — core stats, heatmap and equity curve.
 *
 * The window governs the whole payload, not just the curve. Pass a built-in
 * preset name and let the server expand it: "YTD" and "1Y" are definitions,
 * and a second copy of them here would be free to drift from the one the
 * figures are actually computed with. Pass explicit dates for a saved preset.
 *
 * Omitting everything defaults to 1Y server-side, so a bare call is bounded
 * rather than plotting the entire ledger.
 */
export async function getDashboardAnalytics(
  selection?: TimeframeSelection
): Promise<DashboardStats> {
  const params =
    selection?.kind === 'custom'
      ? { start_date: selection.start_date, end_date: selection.end_date }
      : selection
        ? { preset: selection.preset }
        : undefined;

  const { data } = await apiClient.get<DashboardStats>('/analytics/dashboard', {
    params,
  });
  return data;
}

/** GET /api/settings/timeframes — saved custom windows, oldest first. */
export async function getTimeframes(): Promise<TimeframePreset[]> {
  const { data } = await apiClient.get<TimeframePreset[]>('/settings/timeframes');
  return data;
}

/** POST /api/settings/timeframes — rejects a duplicate name with a 409. */
export async function createTimeframe(
  payload: TimeframePresetPayload
): Promise<TimeframePreset> {
  const { data } = await apiClient.post<TimeframePreset>(
    '/settings/timeframes',
    payload
  );
  return data;
}

/**
 * PUT /api/settings/timeframes/{id} — a full replacement, not a patch.
 *
 * All three fields are one statement about a window; editing an end date
 * without its start in view is how a range ends up backwards.
 */
export async function updateTimeframe(
  id: string,
  payload: TimeframePresetPayload
): Promise<TimeframePreset> {
  const { data } = await apiClient.put<TimeframePreset>(
    `/settings/timeframes/${id}`,
    payload
  );
  return data;
}

/** DELETE /api/settings/timeframes/{id} — returns the row it removed. */
export async function deleteTimeframe(id: string): Promise<TimeframePreset> {
  const { data } = await apiClient.delete<TimeframePreset>(
    `/settings/timeframes/${id}`
  );
  return data;
}

/**
 * PUT /api/positions/{id}/review
 *
 * The API registers PUT and PATCH on one handler, and the body is a partial
 * update either way (`exclude_unset`), so both are accurate. This sends PUT;
 * the docstring used to say PATCH, which is the verb the Trade Inbox uses.
 *
 * Only the keys present in `payload` are applied. The backend sets
 * review_status to 'reviewed' on success unless `mark_reviewed: false` is sent.
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

/**
 * POST /api/audit — re-derive the ledger and report where stored state
 * disagrees with it.
 *
 * A few seconds by design: it rebuilds every ticker's round trips from the
 * fills to compare against. Deliberately a mutation-shaped call with no
 * caching, because a remembered result answers "was this healthy earlier",
 * which is the claim that goes stale silently. Nothing is written.
 */
export async function runDataAudit(): Promise<AuditResult> {
  const { data } = await apiClient.post<AuditResult>('/audit', null, {
    timeout: 120_000,
  });
  return data;
}

/**
 * GET /api/sizing-scratchpad — everything noted in the last three days,
 * newest first.
 *
 * The server deletes anything older as part of answering this request, so
 * "the log clears itself" is literally true on every call rather than a
 * filter over rows that are still sitting there.
 */
export async function getSizingScratchpad(): Promise<SizingScratchpadEntry[]> {
  const { data } = await apiClient.get<SizingScratchpadEntry[]>(
    '/sizing-scratchpad'
  );
  return data;
}

/** POST /api/sizing-scratchpad — note a possible trade, fast. */
export async function createSizingEntry(
  payload: SizingEntryPayload
): Promise<SizingScratchpadEntry> {
  const { data } = await apiClient.post<SizingScratchpadEntry>(
    '/sizing-scratchpad',
    payload
  );
  return data;
}

/**
 * PATCH /api/sizing-scratchpad/{id} — adjust a note in place.
 *
 * Does not reset the note's three-day clock: `created_at` is untouched by
 * the server regardless of what changes here.
 */
export async function updateSizingEntry(
  id: string,
  payload: SizingEntryUpdatePayload
): Promise<SizingScratchpadEntry> {
  const { data } = await apiClient.patch<SizingScratchpadEntry>(
    `/sizing-scratchpad/${id}`,
    payload
  );
  return data;
}

/** DELETE /api/sizing-scratchpad/{id} — discard a note. */
export async function deleteSizingEntry(id: string): Promise<void> {
  await apiClient.delete(`/sizing-scratchpad/${id}`);
}

/**
 * POST /api/sizing-scratchpad/{id}/promote — turn a note into a real plan.
 *
 * One-way: the server deletes the note in the same transaction as creating
 * the plan, so the scratchpad and the journal can never end up disagreeing
 * about whether this trade was promoted.
 */
export async function promoteSizingEntry(id: string): Promise<TradePlan> {
  const { data } = await apiClient.post<TradePlan>(
    `/sizing-scratchpad/${id}/promote`
  );
  return data;
}
