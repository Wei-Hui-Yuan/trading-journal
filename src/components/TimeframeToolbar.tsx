'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { CalendarRange, Loader2, Pencil, Plus, X } from 'lucide-react';

import { ConfirmDialog } from './ConfirmDialog';
import {
  useCreateTimeframe,
  useDeleteTimeframe,
  useTimeframes,
  useUpdateTimeframe,
} from '@/hooks/useTradeInbox';
import type {
  ToolbarWindow,
  TimeframePreset,
  TimeframePresetName,
  TimeframeSelection,
} from '@/types/api';

/**
 * The built-ins, in the order they appear.
 *
 * Deliberately three. A toolbar of ranges is a menu, and the point of the
 * permanent pills is that the two you actually use are always in the same
 * place — anything else belongs in a saved preset, where it carries a name
 * saying why it exists.
 *
 * Their meaning lives on the server. These are labels for a name the API
 * expands; putting the date arithmetic here would be a second definition of
 * "YTD", free to disagree with the one the figures were computed under.
 */
const BUILT_INS: { preset: TimeframePresetName; label: string; hint: string }[] = [
  { preset: 'YTD', label: 'YTD', hint: 'January 1st of this year to today' },
  { preset: '1Y', label: '1Y', hint: 'The last 365 days' },
  { preset: 'ALL', label: 'ALL', hint: 'Every closed round trip' },
];

export const DEFAULT_SELECTION: TimeframeSelection = { kind: 'preset', preset: '1Y' };

const isSame = (a: TimeframeSelection, b: TimeframeSelection) =>
  a.kind === 'custom' && b.kind === 'custom'
    ? a.id === b.id
    : a.kind === 'preset' && b.kind === 'preset' && a.preset === b.preset;

const pillClass = (active: boolean) =>
  `rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors ${
    active
      ? 'bg-slate-700 text-slate-100'
      : 'text-obsidian-muted hover:bg-obsidian-border/60 hover:text-slate-300'
  }`;

/** `YYYY-MM-DD` for an `<input type="date">`. */
const todayISO = () => new Date().toISOString().slice(0, 10);

/**
 * Rendered without a timezone conversion, on purpose.
 *
 * These are calendar dates, not instants — the API stores and compares them as
 * market-time days. Passing one through `new Date()` would make it midnight
 * UTC and render as the previous day for anyone west of London, so the window
 * label would disagree with the window.
 */
const prettyDate = (iso: string) => {
  const [y, m, d] = iso.split('-').map(Number);
  if (!y || !m || !d) return iso;
  const MONTHS = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ');
  return `${d} ${MONTHS[m - 1]} ${y}`;
};

// ---------------------------------------------------------------------------
// Add / edit modal
// ---------------------------------------------------------------------------

interface EditorProps {
  /** The preset being edited, or null when adding a new one. */
  preset: TimeframePreset | null;
  open: boolean;
  busy: boolean;
  error: string | null;
  onSave: (values: { name: string; start_date: string; end_date: string }) => void;
  onCancel: () => void;
}

const TimeframeEditor: React.FC<EditorProps> = ({
  preset,
  open,
  busy,
  error,
  onSave,
  onCancel,
}) => {
  const [mounted, setMounted] = useState(false);
  const [name, setName] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => setMounted(true), []);

  // Reset from the preset every time the modal opens, so a cancelled edit
  // does not leave its half-typed values behind for the next one.
  useEffect(() => {
    if (!open) return;
    setName(preset?.name ?? '');
    setStart(preset?.start_date ?? `${new Date().getFullYear()}-01-01`);
    setEnd(preset?.end_date ?? todayISO());
    const id = window.setTimeout(() => nameRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [open, preset]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onCancel]);

  // Checked here so the reason is on screen beside the field rather than
  // arriving as a 422 after a round trip. The API and a CHECK constraint both
  // enforce it again — this is the message, not the guarantee.
  const localError = useMemo(() => {
    if (!name.trim()) return 'Give the filter a name.';
    if (!start || !end) return 'Pick both a start and an end date.';
    if (start > end) return 'The start date must be on or before the end date.';
    return null;
  }, [name, start, end]);

  if (!mounted || !open) return null;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (localError || busy) return;
    onSave({ name: name.trim(), start_date: start, end_date: end });
  };

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border border-obsidian-border bg-obsidian-card p-5 shadow-2xl"
      >
        <div className="mb-4 flex items-start justify-between">
          <h3 className="text-sm font-semibold text-slate-100">
            {preset ? 'Edit filter' : 'New filter'}
          </h3>
          <button
            type="button"
            onClick={onCancel}
            className="text-obsidian-muted transition-colors hover:text-slate-300"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <label className="block text-[10px] uppercase tracking-wider text-obsidian-muted">
          Name
        </label>
        <input
          ref={nameRef}
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={60}
          placeholder="2025 Full Year"
          className="mt-1 w-full rounded-md border border-obsidian-border bg-obsidian-bg px-2.5 py-1.5 text-sm text-slate-100 outline-none focus:border-slate-500"
        />

        <div className="mt-3 grid grid-cols-2 gap-3">
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-obsidian-muted">
              From
            </label>
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              className="mt-1 w-full rounded-md border border-obsidian-border bg-obsidian-bg px-2.5 py-1.5 text-sm text-slate-100 outline-none focus:border-slate-500"
            />
          </div>
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-obsidian-muted">
              To
            </label>
            <input
              type="date"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              className="mt-1 w-full rounded-md border border-obsidian-border bg-obsidian-bg px-2.5 py-1.5 text-sm text-slate-100 outline-none focus:border-slate-500"
            />
          </div>
        </div>

        <p className="mt-2 text-[10px] leading-relaxed text-obsidian-muted">
          Both dates are included. Round trips are selected by when they{' '}
          <span className="text-slate-400">closed</span>, so a trade opened in
          December and sold in January belongs to January.
        </p>

        {(localError || error) && (
          <p className="mt-3 text-[11px] text-loss">{localError ?? error}</p>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md px-3 py-1.5 text-xs text-obsidian-muted transition-colors hover:text-slate-300"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={!!localError || busy}
            className="inline-flex items-center rounded-md bg-slate-700 px-3 py-1.5 text-xs font-medium text-slate-100 transition-colors hover:bg-slate-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy && <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />}
            {preset ? 'Save' : 'Add filter'}
          </button>
        </div>
      </form>
    </div>,
    document.body
  );
};

// ---------------------------------------------------------------------------
// Toolbar
// ---------------------------------------------------------------------------

interface TimeframeToolbarProps {
  selection: TimeframeSelection;
  onSelect: (selection: TimeframeSelection) => void;
  /**
   * Echoed back by the API — what the figures on screen actually cover.
   *
   * Typed as the narrow `ToolbarWindow` rather than `DashboardWindow` so the
   * analytics page can pass its own window too. The dashboard's satisfies this
   * structurally, so nothing there changes.
   */
  window?: ToolbarWindow;
  /** True while a window switch is in flight, so the pills can say so. */
  isFetching?: boolean;
}

/**
 * Chooses the span every figure on the dashboard is computed over.
 *
 * Sits above the KPI strip rather than inside the equity curve card, because
 * it governs all three panels. Tucked into the chart header it would read as a
 * chart control, and a 1Y curve beside an all-time win rate is exactly the
 * confusion this is meant to remove.
 *
 * YTD / 1Y / ALL are permanent and expand server-side. Everything else is a
 * saved preset, stored in the database rather than localStorage so a window
 * you look at daily is still there on another device.
 */
export const TimeframeToolbar: React.FC<TimeframeToolbarProps> = ({
  selection,
  onSelect,
  window: activeWindow,
  isFetching = false,
}) => {
  const { data: presets, isPending, isError, error } = useTimeframes();
  const create = useCreateTimeframe();
  const update = useUpdateTimeframe();
  const remove = useDeleteTimeframe();

  const [editing, setEditing] = useState<TimeframePreset | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [confirming, setConfirming] = useState<TimeframePreset | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const openEditor = (preset: TimeframePreset | null) => {
    setEditing(preset);
    setSaveError(null);
    setEditorOpen(true);
  };

  const save = (values: { name: string; start_date: string; end_date: string }) => {
    setSaveError(null);
    const onSuccess = (saved: TimeframePreset) => {
      setEditorOpen(false);
      // Follow the preset you just wrote. Adding a filter and staying on the
      // old window makes it look as though nothing happened; and if the one
      // being edited is on screen, its dates just changed under it.
      if (!editing || isSame(selection, asSelection(saved))) {
        onSelect(asSelection(saved));
      }
    };
    const onError = (err: Error) => setSaveError(err.message);

    if (editing) {
      update.mutate({ id: editing.id, payload: values }, { onSuccess, onError });
    } else {
      create.mutate(values, { onSuccess, onError });
    }
  };

  const confirmDelete = () => {
    if (!confirming) return;
    const doomed = confirming;
    remove.mutate(doomed.id, {
      onSettled: () => setConfirming(null),
      onSuccess: () => {
        // The selected window cannot outlive the pill it came from.
        if (isSame(selection, asSelection(doomed))) onSelect(DEFAULT_SELECTION);
      },
    });
  };

  const busy = create.isPending || update.isPending;

  return (
    <div className="flex flex-wrap items-center gap-x-1.5 gap-y-2">
      <CalendarRange className="mr-0.5 h-3.5 w-3.5 shrink-0 text-obsidian-muted" />

      {BUILT_INS.map(({ preset, label, hint }) => (
        <button
          key={preset}
          type="button"
          title={hint}
          onClick={() => onSelect({ kind: 'preset', preset })}
          className={pillClass(
            selection.kind === 'preset' && selection.preset === preset
          )}
        >
          {label}
        </button>
      ))}

      {(isPending || (presets?.length ?? 0) > 0) && (
        <span className="mx-1 h-4 w-px bg-obsidian-border" aria-hidden />
      )}

      {presets?.map((preset) => {
        const active = isSame(selection, asSelection(preset));
        return (
          <span
            key={preset.id}
            className={`group inline-flex items-center rounded-md ${
              active ? 'bg-slate-700' : 'hover:bg-obsidian-border/60'
            }`}
          >
            <button
              type="button"
              title={`${prettyDate(preset.start_date)} — ${prettyDate(preset.end_date)}`}
              onClick={() => onSelect(asSelection(preset))}
              className={`rounded-l-md py-1 pl-2.5 pr-1 text-[11px] font-medium transition-colors ${
                active ? 'text-slate-100' : 'text-obsidian-muted group-hover:text-slate-300'
              }`}
            >
              {preset.name}
            </button>
            {/* Revealed on hover and on keyboard focus. focus-within is what
                keeps edit and delete reachable without a mouse. */}
            <span className="flex items-center opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
              <button
                type="button"
                onClick={() => openEditor(preset)}
                aria-label={`Edit ${preset.name}`}
                title="Edit"
                className="px-1 py-1 text-obsidian-muted transition-colors hover:text-slate-200"
              >
                <Pencil className="h-3 w-3" />
              </button>
              <button
                type="button"
                onClick={() => setConfirming(preset)}
                aria-label={`Delete ${preset.name}`}
                title="Delete"
                className="rounded-r-md px-1 py-1 pr-1.5 text-obsidian-muted transition-colors hover:text-loss"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          </span>
        );
      })}

      <button
        type="button"
        onClick={() => openEditor(null)}
        className="inline-flex items-center rounded-md border border-dashed border-obsidian-border px-2 py-1 text-[11px] text-obsidian-muted transition-colors hover:border-slate-600 hover:text-slate-300"
      >
        <Plus className="mr-1 h-3 w-3" />
        Add filter
      </button>

      {isFetching && (
        <Loader2 className="ml-1 h-3 w-3 animate-spin text-obsidian-muted" />
      )}

      {/* The window actually applied, in words. Without it a custom pill is a
          label with no way to check what it selected, and ALL is the only
          built-in whose span is self-evident. */}
      {activeWindow && (
        <span className="ml-auto text-[10px] text-obsidian-muted">
          {activeWindow.start_date
            ? `${prettyDate(activeWindow.start_date)} — ${
                activeWindow.end_date ? prettyDate(activeWindow.end_date) : 'now'
              }`
            : 'All history'}
          {' · '}
          {activeWindow.closed_trades_in_window} of{' '}
          {activeWindow.closed_trades_total} closed
        </span>
      )}

      {/* A failed preset load costs the custom pills, not the toolbar. The
          built-ins still work, so the dashboard stays usable. */}
      {isError && (
        <span className="ml-auto text-[10px] text-loss">
          Saved filters unavailable — {(error as Error)?.message ?? 'request failed'}
        </span>
      )}

      <TimeframeEditor
        preset={editing}
        open={editorOpen}
        busy={busy}
        error={saveError}
        onSave={save}
        onCancel={() => setEditorOpen(false)}
      />

      <ConfirmDialog
        open={confirming !== null}
        title="Delete this filter?"
        confirmLabel={remove.isPending ? 'Deleting…' : 'Delete filter'}
        onConfirm={confirmDelete}
        onCancel={() => setConfirming(null)}
      >
        <p>
          <span className="font-medium text-slate-200">{confirming?.name}</span>{' '}
          covers {confirming && prettyDate(confirming.start_date)} to{' '}
          {confirming && prettyDate(confirming.end_date)}.
        </p>
        <p className="mt-2">
          Only the saved filter is removed. No trade, review or figure is
          affected — you can add the same range again at any time.
        </p>
      </ConfirmDialog>
    </div>
  );
};

/** A saved preset as the selection the dashboard query is keyed on. */
function asSelection(preset: TimeframePreset): TimeframeSelection {
  return {
    kind: 'custom',
    id: preset.id,
    start_date: preset.start_date,
    end_date: preset.end_date,
  };
}

export default TimeframeToolbar;
