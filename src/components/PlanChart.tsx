'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ImagePlus, Loader2, Maximize2, Trash2, X } from 'lucide-react';

import { usePlanChart } from '@/hooks/useTradeInbox';
import {
  ACCEPTED_INPUT,
  compressChartImage,
  formatBytes,
  imageFromPaste,
  type CompressedChart,
} from '@/lib/chartImage';

/**
 * Pick, paste or drop a chart screenshot.
 *
 * Controlled: the compressed result is handed up and the parent decides when
 * to upload it. The Create Plan modal holds it until the plan exists, because
 * the upload is keyed by plan id and there is no id until the plan is saved.
 *
 * Compression runs the moment an image arrives rather than at submit, so the
 * size shown is the size that will actually be stored — and a file that fails
 * to decode says so while there is still something to do about it.
 */
export function ChartDropzone({
  value,
  onChange,
  disabled = false,
}: {
  value: CompressedChart | null;
  onChange: (chart: CompressedChart | null) => void;
  disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!value) {
      setPreview(null);
      return;
    }
    const url = URL.createObjectURL(value.blob);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [value]);

  const accept = useCallback(
    async (file: Blob | null) => {
      if (!file || disabled) return;
      setBusy(true);
      setError(null);
      try {
        onChange(await compressChartImage(file));
      } catch (err) {
        setError(err instanceof Error ? err.message : 'That image could not be read.');
        onChange(null);
      } finally {
        setBusy(false);
      }
    },
    [disabled, onChange]
  );

  // Paste anywhere in the document while this is mounted. The screenshot is
  // already on the clipboard from the charting platform, and making the user
  // save it to disk first is most of the friction in attaching one at all.
  useEffect(() => {
    const onPaste = (event: ClipboardEvent) => {
      const image = imageFromPaste(event);
      if (image) {
        event.preventDefault();
        void accept(image);
      }
    };
    document.addEventListener('paste', onPaste);
    return () => document.removeEventListener('paste', onPaste);
  }, [accept]);

  if (value && preview) {
    return (
      <div className="rounded-lg border border-obsidian-border bg-obsidian-bg p-2">
        <img
          src={preview}
          alt="Chart screenshot to be attached to this plan"
          className="w-full rounded border border-obsidian-border"
        />
        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="font-mono text-[10px] text-obsidian-muted">
            {value.width}&times;{value.height} &middot; {formatBytes(value.encodedBytes)}
            {value.originalBytes > value.encodedBytes && (
              <> &middot; from {formatBytes(value.originalBytes)}</>
            )}
            {value.lossless && <> &middot; lossless</>}
          </span>
          <button
            type="button"
            onClick={() => onChange(null)}
            disabled={disabled}
            className="inline-flex items-center gap-1 rounded border border-obsidian-border px-2 py-1 text-[10px] text-obsidian-muted transition-colors hover:border-loss/40 hover:text-loss disabled:opacity-50"
          >
            <Trash2 className="h-3 w-3" />
            Remove
          </button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          void accept(e.dataTransfer.files?.[0] ?? null);
        }}
        disabled={disabled || busy}
        className={`flex w-full flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed px-4 py-6 transition-colors disabled:opacity-50 ${
          dragging
            ? 'border-amber-500/50 bg-amber-500/5'
            : 'border-obsidian-border bg-obsidian-bg hover:border-slate-600'
        }`}
      >
        {busy ? (
          <Loader2 className="h-5 w-5 animate-spin text-obsidian-muted" />
        ) : (
          <ImagePlus className="h-5 w-5 text-obsidian-muted" />
        )}
        <span className="text-xs text-slate-300">
          {busy ? 'Compressing…' : 'Paste, drop or click to add a chart'}
        </span>
        <span className="text-[10px] text-obsidian-muted">
          Stored at full resolution, losslessly — nothing is resized or blurred
        </span>
      </button>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED_INPUT}
        className="hidden"
        onChange={(e) => {
          void accept(e.target.files?.[0] ?? null);
          e.target.value = '';
        }}
      />
      {error && <p className="mt-1.5 text-[11px] text-loss">{error}</p>}
    </div>
  );
}

/**
 * The stored chart for a plan that already has one.
 *
 * `thumbnail` is the dock's compact form; full size is the ledger's, beside
 * the post-mortem. Clicking either opens the lightbox, which is the whole
 * point on a 1866px-wide capture — the small numbers are unreadable until it
 * fills the screen.
 */
export function PlanChartView({
  planId,
  thumbnail = false,
  enabled = true,
}: {
  planId: string;
  thumbnail?: boolean;
  enabled?: boolean;
}) {
  const { url, isLoading, error } = usePlanChart(planId, enabled);
  const [zoomed, setZoomed] = useState(false);

  useEffect(() => {
    if (!zoomed) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setZoomed(false);
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [zoomed]);

  if (isLoading) {
    return (
      <div
        className={`flex items-center justify-center rounded border border-obsidian-border bg-obsidian-bg ${
          thumbnail ? 'h-14 w-20' : 'h-40 w-full'
        }`}
      >
        <Loader2 className="h-4 w-4 animate-spin text-obsidian-muted" />
      </div>
    );
  }

  if (error || !url) {
    return (
      <div
        className={`flex items-center justify-center rounded border border-obsidian-border bg-obsidian-bg px-2 text-center text-[10px] text-obsidian-muted ${
          thumbnail ? 'h-14 w-20' : 'h-40 w-full'
        }`}
      >
        {error ? 'Chart unavailable' : 'No chart'}
      </div>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setZoomed(true)}
        className="group relative block overflow-hidden rounded border border-obsidian-border transition-colors hover:border-slate-600"
        aria-label="Open the chart full size"
      >
        <img
          src={url}
          alt="The chart this plan was written from"
          className={thumbnail ? 'h-14 w-20 object-cover' : 'w-full'}
        />
        <span className="absolute inset-0 flex items-center justify-center bg-black/0 opacity-0 transition-opacity group-hover:bg-black/40 group-hover:opacity-100">
          <Maximize2 className="h-4 w-4 text-white" />
        </span>
      </button>

      {zoomed && (
        <div
          className="fixed inset-0 z-[200] flex items-center justify-center bg-black/90 p-4"
          onClick={() => setZoomed(false)}
          role="dialog"
          aria-modal="true"
        >
          <button
            type="button"
            onClick={() => setZoomed(false)}
            className="absolute right-4 top-4 rounded border border-obsidian-border bg-obsidian-card p-2 text-slate-300 transition-colors hover:text-white"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
          <img
            src={url}
            alt="The chart this plan was written from, full size"
            className="max-h-full max-w-full object-contain"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </>
  );
}

