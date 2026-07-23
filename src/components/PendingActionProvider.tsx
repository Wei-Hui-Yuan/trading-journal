'use client';

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { Loader2, RotateCcw, Trash2 } from 'lucide-react';

/** How long you get to change your mind. */
export const UNDO_WINDOW_MS = 10_000;

export interface PendingAction {
  /**
   * Stable identity for the thing being removed, e.g. `trade:<uuid>`.
   *
   * Doubles as the key components filter their lists on, so the row can vanish
   * the instant it is scheduled. Scheduling the same id twice replaces the
   * first, which is what makes a double-click harmless.
   */
  id: string;
  /** Headline, e.g. "AAPL fill deleted". Written as though it already happened. */
  label: string;
  /** The specifics, so the toast can be checked without reopening anything. */
  detail?: string;
  /** Fires only when the window expires. Undo means this is never called. */
  commit: () => Promise<unknown>;
  onCommitted?: (result: unknown) => void;
  onError?: (error: Error) => void;
  onUndo?: () => void;
}

interface PendingActionContextValue {
  schedule: (action: PendingAction) => void;
  /** Whether this id is inside its undo window, and so should be hidden. */
  isPending: (id: string) => boolean;
  /** Commit now instead of waiting out the countdown. */
  commitNow: (id: string) => void;
  undo: (id: string) => void;
}

const PendingActionContext = createContext<PendingActionContextValue | null>(null);

/**
 * Destructive actions you can take back.
 *
 * The request is DEFERRED, not compensated: nothing reaches the API until the
 * countdown expires, and Undo simply cancels it. The alternative -- delete
 * immediately and restore on undo -- cannot work here, because these deletes
 * are not reversible server-side. Removing a fill re-runs FIFO matching, which
 * dissolves round trips and discards the reviews written against them, and
 * deleting a broker fill writes a tombstone so a later sync cannot re-add it.
 * A "restore" would have to rebuild all of that from a snapshot that does not
 * exist.
 *
 * The cost of deferring is that the delete has not happened yet while the toast
 * is up. So the row is hidden optimistically, and closing the tab mid-countdown
 * may lose the delete rather than the data -- which is the safe direction to
 * fail in.
 */
export const PendingActionProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [actions, setActions] = useState<
    Array<PendingAction & { expiresAt: number; committing: boolean }>
  >([]);
  const [now, setNow] = useState(() => Date.now());
  const [mounted, setMounted] = useState(false);

  // Read by the unload handler, which must not close over stale state.
  const actionsRef = useRef(actions);
  actionsRef.current = actions;

  useEffect(() => setMounted(true), []);

  // Ids whose request has already gone out. The `committing` flag lives in
  // state and so is only visible on the next render; this is readable
  // immediately, which is what stops a tick and an unload firing the same
  // DELETE twice. Worth the redundancy: the second request would 404 and
  // surface to the user as a failure of something that actually succeeded.
  const firedRef = useRef<Set<string>>(new Set());

  const runCommit = useCallback((action: PendingAction) => {
    if (firedRef.current.has(action.id)) return;
    firedRef.current.add(action.id);

    setActions((prev) =>
      prev.map((a) => (a.id === action.id ? { ...a, committing: true } : a))
    );
    action
      .commit()
      .then((result) => action.onCommitted?.(result))
      .catch((error) =>
        action.onError?.(error instanceof Error ? error : new Error(String(error)))
      )
      .finally(() => {
        firedRef.current.delete(action.id);
        setActions((prev) => prev.filter((a) => a.id !== action.id));
      });
  }, []);

  // One ticker drives both the countdown and the commit, so what the toast
  // shows and what actually fires cannot disagree. Per-action setTimeouts plus
  // a separate display interval would be two clocks telling one story.
  useEffect(() => {
    if (actions.length === 0) return;
    const handle = window.setInterval(() => setNow(Date.now()), 200);
    return () => window.clearInterval(handle);
  }, [actions.length]);

  useEffect(() => {
    const due = actions.filter((a) => !a.committing && now >= a.expiresAt);
    due.forEach(runCommit);
  }, [now, actions, runCommit]);

  const schedule = useCallback((action: PendingAction) => {
    setActions((prev) => [
      ...prev.filter((a) => a.id !== action.id),
      { ...action, expiresAt: Date.now() + UNDO_WINDOW_MS, committing: false },
    ]);
    setNow(Date.now());
  }, []);

  const undo = useCallback((id: string) => {
    const target = actionsRef.current.find((a) => a.id === id);
    // Already firing: too late to call it back, and pretending otherwise would
    // leave the row hidden while the request deletes it anyway.
    if (!target || target.committing) return;
    // Called out here rather than inside the updater: React may run an updater
    // twice, and a callback that fires twice per undo is a real bug in waiting.
    target.onUndo?.();
    setActions((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const commitNow = useCallback(
    (id: string) => {
      const target = actionsRef.current.find((a) => a.id === id);
      if (target && !target.committing) runCommit(target);
    },
    [runCommit]
  );

  const isPending = useCallback(
    (id: string) => actions.some((a) => a.id === id),
    [actions]
  );

  // Leaving the page is not a decision to keep the row. The last thing the
  // user actually asked for was the delete, so it is sent rather than dropped.
  // Best-effort by nature -- the browser may cut the request off -- but a lost
  // delete leaves the data intact, where a lost undo would not.
  useEffect(() => {
    const flush = () => {
      actionsRef.current.forEach((a) => {
        if (firedRef.current.has(a.id)) return;
        firedRef.current.add(a.id);
        // Fire and forget: there is no render left to report into.
        void a.commit().catch(() => undefined);
      });
    };
    window.addEventListener('beforeunload', flush);
    return () => {
      window.removeEventListener('beforeunload', flush);
      flush();
    };
  }, []);

  const value = useMemo(
    () => ({ schedule, isPending, commitNow, undo }),
    [schedule, isPending, commitNow, undo]
  );

  return (
    <PendingActionContext.Provider value={value}>
      {children}
      {mounted &&
        actions.length > 0 &&
        createPortal(
          <div className="fixed bottom-4 left-4 z-[120] flex w-[21rem] max-w-[calc(100vw-2rem)] flex-col gap-2">
            {actions.map((action) => {
              const remaining = Math.max(0, action.expiresAt - now);
              const seconds = Math.ceil(remaining / 1000);
              const pct = (remaining / UNDO_WINDOW_MS) * 100;

              return (
                <div
                  key={action.id}
                  role="status"
                  aria-live="polite"
                  className="overflow-hidden rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl"
                >
                  <div className="flex items-start gap-2 px-4 py-3">
                    <Trash2 className="mt-px h-4 w-4 shrink-0 text-loss" />
                    <div className="min-w-0 flex-1">
                      <p className="text-xs font-semibold text-slate-100">
                        {action.label}
                      </p>
                      {action.detail && (
                        <p className="mt-0.5 truncate text-[11px] text-obsidian-muted">
                          {action.detail}
                        </p>
                      )}
                      <p className="mt-0.5 text-[11px] text-obsidian-muted">
                        {action.committing ? (
                          <span className="inline-flex items-center gap-1">
                            <Loader2 className="h-3 w-3 animate-spin" />
                            Deleting…
                          </span>
                        ) : (
                          // Present tense: nothing has been sent yet, and
                          // saying "deleted" would be a lie for ten seconds.
                          `Deleting in ${seconds}s`
                        )}
                      </p>
                    </div>

                    <button
                      type="button"
                      onClick={() => undo(action.id)}
                      disabled={action.committing}
                      className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-obsidian-border bg-obsidian-bg px-2.5 py-1.5 text-[11px] font-medium text-slate-200 transition-colors hover:border-slate-500 disabled:opacity-40"
                    >
                      <RotateCcw className="h-3 w-3" />
                      Undo
                    </button>
                  </div>

                  {/* Draining bar. A countdown you can see is worth more than
                      one you have to read. */}
                  <div className="h-0.5 w-full bg-obsidian-bg">
                    <div
                      className="h-full bg-loss/70 transition-[width] duration-200 ease-linear"
                      style={{ width: `${action.committing ? 0 : pct}%` }}
                    />
                  </div>
                </div>
              );
            })}
          </div>,
          document.body
        )}
    </PendingActionContext.Provider>
  );
};

/**
 * Schedule a destructive action behind an undo window.
 *
 * Throws outside the provider rather than degrading to an immediate delete: a
 * silently missing undo window on a delete that discards reviews is worse than
 * a component that fails loudly in development.
 */
export function usePendingActions(): PendingActionContextValue {
  const ctx = useContext(PendingActionContext);
  if (!ctx) {
    throw new Error('usePendingActions must be used inside a PendingActionProvider');
  }
  return ctx;
}

export default PendingActionProvider;
