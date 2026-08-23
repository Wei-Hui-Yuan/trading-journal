import React from 'react';
import Link from 'next/link';
import { ArrowLeft } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

/**
 * Accent for the icon tile.
 *
 * Three values because three were already in use, not because a page should
 * be able to pick freely. `sky` exists for the investment book alone, which is
 * deliberately a different kind of thing from the trading pages -- see the
 * module comment on investments/page.tsx. Anything new should take `win`
 * unless it has as good a reason.
 */
export type PageAccent = 'win' | 'emerald' | 'sky';

const ACCENT: Record<PageAccent, string> = {
  win: 'from-win/20 to-emerald-900/40 border-win/30 text-win',
  emerald: 'from-emerald-500/20 to-emerald-900/40 border-emerald-500/30 text-emerald-400',
  sky: 'from-sky-500/20 to-sky-900/40 border-sky-500/30 text-sky-400',
};

interface PageHeaderProps {
  icon: LucideIcon;
  /** Rendered as the page's h1. Shouted in caps to match the existing pages. */
  title: string;
  subtitle: string;
  accent?: PageAccent;
  /**
   * Width cap for the header's inner row, so it lines up with the `main`
   * underneath it. Defaults to the `max-w-7xl` every page but one uses; the
   * investment table is wider and its header has to agree with it, or the
   * two are visibly out of register at the same scroll position.
   */
  maxWidthClass?: string;
}

/**
 * Title bar for a page that is not the dashboard.
 *
 * Six pages had hand-rolled one of these and they had drifted into two
 * dialects: `card/80` + sticky + `h-16` + a gradient icon tile on analytics,
 * strategies and settings, versus `card/60` + non-sticky + `py-4` + a flat
 * tile on journal, sizing and investments. Same job, two answers, and neither
 * matched the dashboard.
 *
 * Settled on the dashboard's shape, since that is the page every one of these
 * is reached from and the one they should feel continuous with. Sticky is part
 * of that: the journal ledger and the investment table are the longest
 * scrolls in the app and were the two that lost their header on the way down.
 *
 * The title is an `h1`. Three of the six rendered it as a `<span>`, so those
 * pages had no top-level heading at all -- nothing for a screen reader to jump
 * to and no document outline, which is a real defect rather than a styling
 * preference and is the reason this component exists at all rather than a
 * shared className string.
 *
 * `backHref` is not a prop: every one of these goes to the dashboard, and the
 * one thing worse than six copies of a back link is six copies that can point
 * somewhere different.
 */
export const PageHeader: React.FC<PageHeaderProps> = ({
  icon: Icon,
  title,
  subtitle,
  accent = 'win',
  maxWidthClass = 'max-w-7xl',
}) => (
  <header className="border-b border-obsidian-border bg-obsidian-card/80 backdrop-blur-md sticky top-0 z-50">
    {/* `min-h-16`, not `h-16`. The dashboard and the three sticky pages used a
        fixed height, which silently fails on a narrow screen: a subtitle that
        wraps to two lines needs ~76px and spilled straight through the bottom
        border. The three non-sticky pages used `py-4` and grew correctly, so
        adopting the fixed height wholesale would have spread the bug rather
        than settled the argument. This keeps the intended 64px wherever the
        content fits and grows only where it must. */}
    <div
      className={`${maxWidthClass} mx-auto px-4 sm:px-6 lg:px-8 min-h-16 py-2 flex items-center justify-between gap-3`}
    >
      <div className="flex items-center space-x-3">
        <div
          className={`h-9 w-9 rounded-xl bg-gradient-to-br border flex items-center justify-center ${ACCENT[accent]}`}
        >
          <Icon className="h-5 w-5" />
        </div>
        <div>
          <h1 className="font-bold text-lg tracking-wider text-white">{title}</h1>
          <p className="text-xs text-obsidian-muted font-medium">{subtitle}</p>
        </div>
      </div>

      <Link
        href="/"
        className="inline-flex items-center gap-2 rounded-lg bg-obsidian-bg border border-obsidian-border px-3 py-2 text-xs font-medium text-obsidian-muted hover:text-slate-100 hover:border-slate-600 transition-colors"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Dashboard
      </Link>
    </div>
  </header>
);

export default PageHeader;
