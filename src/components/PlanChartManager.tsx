'use client';

import { useState } from 'react';
import { Loader2 } from 'lucide-react';

import { ChartDropzone, PlanChartView } from '@/components/PlanChart';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import {
  useDeletePlanChart,
  useUploadPlanChart,
} from '@/hooks/useTradeInbox';
import type { CompressedChart } from '@/lib/chartImage';

/**
 * Attach, replace or remove a plan's chart -- for an EXISTING plan, by id.
 *
 * Extracted from the Plan modal's own edit mode, which had this exact state
 * machine before there was a second place that needed it. Both callers use
 * the same upload/delete mutations and the same replace/remove buttons, so a
 * fix to one (an error message, a race, a disabled state) reaches both
 * automatically instead of drifting between two copies.
 *
 * `hasChart` / `onHasChartChange` are controlled rather than read from a
 * query, because the two current callers have different staleness problems:
 * the Plan modal holds a snapshot prop that a cache invalidation cannot reach
 * back in and replace, and the Journal renders one row per round trip and
 * wants its own button state to update the instant an upload succeeds,
 * without waiting on a refetch. Both are solved the same way -- local state,
 * seeded from the caller's own source of truth, updated optimistically here.
 */
export function PlanChartManager({
  planId,
  ticker,
  hasChart,
  onHasChartChange,
}: {
  planId: string;
  ticker: string;
  hasChart: boolean;
  onHasChartChange: (hasChart: boolean) => void;
}) {
  const uploadChart = useUploadPlanChart();
  const deleteChart = useDeletePlanChart();
  const [chart, setChart] = useState<CompressedChart | null>(null);
  const [showPicker, setShowPicker] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  // `useUploadPlanChart` is keyed by plan id and upserts in place
  // server-side, so this never leaves an orphaned object in storage behind a
  // replacement -- see services/storage.py's upload().
  const save = async () => {
    if (!chart) return;
    setError(null);
    try {
      await uploadChart.mutateAsync({
        planId,
        image: chart.blob,
        filename: `${ticker}-chart.${chart.mime === 'image/webp' ? 'webp' : 'png'}`,
      });
      setChart(null);
      setShowPicker(false);
      onHasChartChange(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed.');
    }
  };

  // The DELETE endpoint removes the object from storage before clearing the
  // plan's chart columns -- confirmed here because that is real and
  // irreversible, unlike replacing it.
  const confirmDelete = async () => {
    setError(null);
    try {
      await deleteChart.mutateAsync(planId);
      onHasChartChange(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove the chart.');
    } finally {
      setConfirmingDelete(false);
    }
  };

  return (
    <div>
      <span className="text-[10px] uppercase tracking-wide text-obsidian-muted">
        Chart at entry
      </span>

      <div className="mt-1">
        {hasChart && !showPicker ? (
          <div className="flex items-start gap-3">
            <PlanChartView planId={planId} />
            <div className="flex shrink-0 flex-col gap-1.5">
              <button
                type="button"
                onClick={() => setShowPicker(true)}
                disabled={uploadChart.isPending || deleteChart.isPending}
                className="rounded-lg border border-obsidian-border bg-obsidian-bg px-2.5 py-1.5 text-[10px] text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-200 disabled:opacity-50"
              >
                Replace
              </button>
              <button
                type="button"
                onClick={() => setConfirmingDelete(true)}
                disabled={uploadChart.isPending || deleteChart.isPending}
                className="rounded-lg border border-obsidian-border bg-obsidian-bg px-2.5 py-1.5 text-[10px] text-obsidian-muted transition-colors hover:border-loss/40 hover:text-loss disabled:opacity-50"
              >
                {deleteChart.isPending ? 'Removing…' : 'Remove'}
              </button>
            </div>
          </div>
        ) : (
          <>
            <ChartDropzone value={chart} onChange={setChart} disabled={uploadChart.isPending} />
            <div className="mt-2 flex gap-2">
              {chart && (
                <button
                  type="button"
                  onClick={save}
                  disabled={uploadChart.isPending}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-[10px] font-medium text-amber-300 transition-colors hover:bg-amber-500/20 disabled:opacity-60"
                >
                  {uploadChart.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
                  {uploadChart.isPending
                    ? 'Uploading…'
                    : hasChart
                      ? 'Save replacement'
                      : 'Attach chart'}
                </button>
              )}
              {hasChart && (
                <button
                  type="button"
                  onClick={() => {
                    setShowPicker(false);
                    setChart(null);
                    setError(null);
                  }}
                  disabled={uploadChart.isPending}
                  className="rounded-lg border border-obsidian-border px-3 py-1.5 text-[10px] text-obsidian-muted transition-colors hover:text-slate-200 disabled:opacity-50"
                >
                  Cancel
                </button>
              )}
            </div>
          </>
        )}
        {error && <p className="mt-1.5 text-[11px] text-loss">{error}</p>}
      </div>

      <ConfirmDialog
        open={confirmingDelete}
        title="Remove this chart?"
        confirmLabel={deleteChart.isPending ? 'Removing…' : 'Remove'}
        confirmDisabled={deleteChart.isPending}
        onConfirm={confirmDelete}
        onCancel={() => setConfirmingDelete(false)}
      >
        Deletes it from storage. This can&apos;t be undone.
      </ConfirmDialog>
    </div>
  );
}
