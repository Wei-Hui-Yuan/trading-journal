'use client';

import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, X } from 'lucide-react';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** The consequences, in prose. Rendered as-is so it can carry emphasis. */
  children: React.ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  /** Danger reds the confirm button. Anything reversible should not use it. */
  tone?: 'danger' | 'neutral';
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * An in-app replacement for window.confirm().
 *
 * The native dialog had three problems, and only the first is cosmetic. It is
 * unstyled, so a destructive prompt looked less serious than the app around
 * it. It blocks the main thread, freezing React and any in-flight request
 * until it is answered. And it renders `\n\n` as the only structure available,
 * so the consequences of a delete arrived as one undifferentiated paragraph
 * that is easy to dismiss unread -- which is exactly the moment it matters.
 *
 * Cancel is the default focus. For an irreversible action the safe choice
 * should be the one that happens if you hit Enter out of habit.
 */
export const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  open,
  title,
  children,
  confirmLabel,
  cancelLabel = 'Cancel',
  tone = 'danger',
  onConfirm,
  onCancel,
}) => {
  const [mounted, setMounted] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (!open) return;
    // Focus lands on Cancel, not Confirm.
    const id = window.setTimeout(() => cancelRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onCancel]);

  if (!open || !mounted) return null;

  const confirmClass =
    tone === 'danger'
      ? 'border-loss/40 bg-loss/15 text-loss hover:bg-loss/25'
      : 'border-win-border bg-win-glow text-win hover:bg-win/20';

  return createPortal(
    <div
      // Above the modals it can be opened from.
      className="fixed inset-0 z-[130] flex items-center justify-center p-4"
      role="alertdialog"
      aria-modal="true"
      aria-label={title}
    >
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onCancel} />

      <div className="relative w-full max-w-md rounded-xl border border-obsidian-border bg-obsidian-card shadow-2xl">
        <div className="flex items-start justify-between border-b border-obsidian-border px-5 py-4">
          <div className="flex items-center gap-2">
            <AlertTriangle
              className={`h-4 w-4 ${tone === 'danger' ? 'text-loss' : 'text-amber-400'}`}
            />
            <h2 className="text-sm font-semibold tracking-wide text-slate-100">{title}</h2>
          </div>
          <button
            type="button"
            onClick={onCancel}
            aria-label="Cancel"
            className="rounded-lg p-1 text-obsidian-muted transition-colors hover:bg-obsidian-bg hover:text-slate-200"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-2 px-5 py-4 text-[12px] leading-relaxed text-slate-300">
          {children}
        </div>

        <div className="flex justify-end gap-2 border-t border-obsidian-border px-5 py-3">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-obsidian-border bg-obsidian-bg px-3.5 py-2 text-xs font-medium text-slate-300 transition-colors hover:border-slate-600 hover:text-slate-100"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className={`rounded-lg border px-4 py-2 text-xs font-medium transition-colors ${confirmClass}`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
};

export default ConfirmDialog;
