'use client';

import React, { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, PiggyBank } from 'lucide-react';

import type { Holding } from '@/types/investments';

const money = (value: number) =>
  value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** `$243`, `$1.1k` -- a treemap tile has room for one short number, not a
 * fully punctuated one. */
const compactMoney = (value: number) => {
  const abs = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  return abs >= 1000 ? `${sign}$${(abs / 1000).toFixed(1)}k` : `${sign}$${abs.toFixed(0)}`;
};

/**
 * One color per GROUP (sector, or category when sector is absent), not per
 * ticker -- twenty distinct hues never reads cleanly, however it is laid
 * out. A handful of sector colors carries actual meaning; every ticker in
 * Technology being visibly the same color is the point.
 *
 * Deliberately desaturated -- a market-data terminal's sector map reads as
 * information at a glance because the palette stays low-key; saturated
 * primaries compete with the numbers instead of sitting behind them.
 */
const GROUP_COLORS: Record<string, string> = {
  Technology: '#3B5978',
  'Communication Services': '#5B4E80',
  'Consumer Discretionary': '#8A5A2B',
  'Consumer Staples': '#5C6B3A',
  'Health Care': '#2F6B60',
  Financials: '#2B5F6B',
  Industrials: '#6B3B52',
  Energy: '#7A3B33',
  Utilities: '#7A6B2B',
  Materials: '#6B5A2B',
  'Real Estate': '#2B6B5E',
  ETF: '#4A5568',
  // Deliberately near the card background rather than another muted hue --
  // this bucket means "we don't know", and giving it a real color would make
  // it look like a deliberate category instead of a gap to close. The
  // diagonal hatch, not the fill, is what marks these tiles.
  'No Target Set': '#1A2029',
};
const FALLBACK_COLORS = ['#3B5978', '#5B4E80', '#8A5A2B', '#5C6B3A', '#2F6B60', '#2B5F6B', '#6B3B52'];

function groupColor(name: string, indexIfUnknown: number): string {
  return GROUP_COLORS[name] ?? FALLBACK_COLORS[indexIfUnknown % FALLBACK_COLORS.length];
}

const HATCH_BG =
  'repeating-linear-gradient(45deg, transparent, transparent 4px, rgba(255,255,255,0.04) 4px, rgba(255,255,255,0.04) 8px)';

type RowStatus = 'short' | 'funded' | 'no-target';

interface GroupedRow {
  ticker: string;
  deployed: number;
  target: number | null;
  gap: number | null;
  fundedPct: number | null;
  status: RowStatus;
}

interface Group {
  name: string;
  color: string;
  rows: GroupedRow[];
  totalDeployed: number;
}

const STATUS_ORDER: Record<RowStatus, number> = { short: 0, funded: 1, 'no-target': 2 };

// ---------------------------------------------------------------------------
// Squarify (Bruls, Huizing & van Wijk, 1999) -- laid out in a fixed virtual
// canvas and rendered with percentage positioning, so it never needs to
// measure the real DOM width. Produces genuinely 2D, roughly-square tiles
// instead of one dimension's worth of strips.
// ---------------------------------------------------------------------------
interface VRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const CANVAS: VRect = { x: 0, y: 0, w: 1000, h: 500 };

function worstRatio(row: number[], side: number): number {
  if (row.length === 0 || side <= 0) return Infinity;
  const sum = row.reduce((a, b) => a + b, 0);
  const thickness = sum / side;
  if (thickness <= 0) return Infinity;
  let worst = 0;
  for (const v of row) {
    const length = v / thickness;
    const ratio = Math.max(length / thickness, thickness / length);
    if (ratio > worst) worst = ratio;
  }
  return worst;
}

/** `values` must already be sorted descending and sum to `rect.w * rect.h`. */
function squarify(values: number[], rect: VRect): VRect[] {
  const result: VRect[] = [];
  let items = values;
  let container = { ...rect };

  while (items.length > 0 && container.w > 0 && container.h > 0) {
    const side = Math.min(container.w, container.h);
    let row = [items[0]];
    let i = 1;
    while (i < items.length) {
      const testRow = [...row, items[i]];
      // Grow the row only while doing so does not worsen its aspect ratio --
      // the greedy criterion that keeps tiles close to square.
      if (worstRatio(testRow, side) <= worstRatio(row, side)) {
        row = testRow;
        i++;
      } else {
        break;
      }
    }

    const rowSum = row.reduce((a, b) => a + b, 0);
    const thickness = rowSum / side;

    if (container.w <= container.h) {
      let cx = container.x;
      for (const v of row) {
        const w = v / thickness;
        result.push({ x: cx, y: container.y, w, h: thickness });
        cx += w;
      }
      container = { x: container.x, y: container.y + thickness, w: container.w, h: container.h - thickness };
    } else {
      let cy = container.y;
      for (const v of row) {
        const h = v / thickness;
        result.push({ x: container.x, y: cy, w: thickness, h });
        cy += h;
      }
      container = { x: container.x + thickness, y: container.y, w: container.w - thickness, h: container.h };
    }

    items = items.slice(row.length);
  }

  return result;
}

function squarifyItems<T>(items: T[], valueOf: (t: T) => number, rect: VRect): { item: T; rect: VRect }[] {
  const sorted = [...items].sort((a, b) => valueOf(b) - valueOf(a));
  const total = sorted.reduce((s, it) => s + valueOf(it), 0);
  if (total <= 0 || rect.w <= 0 || rect.h <= 0) return [];
  const scale = (rect.w * rect.h) / total;
  const rects = squarify(
    sorted.map((it) => valueOf(it) * scale),
    rect
  );
  return sorted.map((item, i) => ({ item, rect: rects[i] }));
}

interface Tile {
  ticker: string;
  sector: string;
  color: string;
  deployed: number;
  pctOfTotal: number;
  isUntargeted: boolean;
  rect: VRect;
}

/**
 * Deployment discipline, not performance -- every figure here is cost basis
 * or a planned dollar target, never market value, because a name that is
 * simply up a lot should not read as "over-allocated" for reasons unrelated
 * to any decision the user made.
 *
 * Grouped by sector (falling back to category, then to a "No Target Set"
 * bucket) so both the treemap and the table beneath it use the same
 * grouping key -- a ticker in the "Technology" block of the treemap is the
 * same ticker under "Technology" in the table, not two different views that
 * happen to share a name.
 */
export const AllocationPanel: React.FC<{
  holdings: Holding[];
  onSetTarget: (ticker: string) => void;
}> = ({ holdings, onSetTarget }) => {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const toggle = (name: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const { groups, dca, tiles } = useMemo(() => {
    const funded = holdings.filter((h) => h.cost_basis > 0);

    const byGroup = new Map<string, Holding[]>();
    for (const h of funded) {
      const key = h.sector || h.category || 'No Target Set';
      if (!byGroup.has(key)) byGroup.set(key, []);
      byGroup.get(key)!.push(h);
    }

    let unknownIdx = 0;
    const built: Group[] = Array.from(byGroup.entries()).map(([name, hs]) => {
      const color = groupColor(name, GROUP_COLORS[name] ? 0 : unknownIdx++);
      const rows: GroupedRow[] = hs
        .map((h) => {
          const target = h.planned_allocation && h.planned_allocation > 0 ? h.planned_allocation : null;
          const gap = target !== null ? target - h.cost_basis : null;
          const fundedPct = target !== null ? (h.cost_basis / target) * 100 : null;
          const status: RowStatus = target === null ? 'no-target' : gap! > 0 ? 'short' : 'funded';
          return { ticker: h.ticker, deployed: h.cost_basis, target, gap, fundedPct, status };
        })
        .sort((a, b) => {
          if (a.status !== b.status) return STATUS_ORDER[a.status] - STATUS_ORDER[b.status];
          if (a.status === 'short') return (b.gap ?? 0) - (a.gap ?? 0);
          return b.deployed - a.deployed;
        });
      const totalDeployed = hs.reduce((sum, h) => sum + h.cost_basis, 0);
      return { name, color, rows, totalDeployed };
    });

    // Real sectors sorted largest first; "No Target Set" always last --
    // it is a gap to close, not a position of the book worth leading with.
    // (The treemap below uses its own, size-driven placement -- this
    // ordering is for the progress table only.)
    const groups = [
      ...built.filter((g) => g.name !== 'No Target Set').sort((a, b) => b.totalDeployed - a.totalDeployed),
      ...built.filter((g) => g.name === 'No Target Set'),
    ];

    const allShort = groups.flatMap((g) => g.rows.filter((r) => r.status === 'short'));
    const dca = allShort.length
      ? allShort.reduce((worst, r) => ((r.gap ?? 0) > (worst.gap ?? 0) ? r : worst))
      : null;

    const grandTotal = groups.reduce((sum, g) => sum + g.totalDeployed, 0);

    const sectorPlacement = squarifyItems(
      groups.filter((g) => g.totalDeployed > 0),
      (g) => g.totalDeployed,
      CANVAS
    );

    const tiles: Tile[] = sectorPlacement.flatMap(({ item: g, rect: sectorRect }) =>
      squarifyItems(
        g.rows.filter((r) => r.deployed > 0),
        (r) => r.deployed,
        sectorRect
      ).map(({ item: r, rect }) => ({
        ticker: r.ticker,
        sector: g.name,
        color: g.color,
        deployed: r.deployed,
        pctOfTotal: grandTotal > 0 ? (r.deployed / grandTotal) * 100 : 0,
        isUntargeted: g.name === 'No Target Set',
        rect,
      }))
    );

    return { groups, dca, tiles };
  }, [holdings]);

  if (groups.length === 0) return null;

  return (
    <div className="grid grid-cols-1 gap-4 rounded-xl border border-obsidian-border bg-obsidian-card p-4 lg:grid-cols-2">
      {/* ---------------- sector treemap ---------------- */}
      {/* No fixed height, and no `items-start` on the grid above: the two
          columns stretch to match each other (CSS Grid's default), and this
          one is a flex column with the canvas as the only `flex-1` -- so it
          fills whatever the progress table's real height turns out to be
          instead of leaving dead space, in either direction, if the table
          grows or shrinks. */}
      <div className="flex h-full flex-col">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-300">
          Sector treemap
        </div>
        <div className="text-[10px] text-obsidian-muted">Sized by cost basis, not market value</div>

        {/* Squarified: tiles are positioned by percentage against the fixed
            CANVAS above, so the layout is correct at any real render height
            without measuring the DOM -- it only needs SOME height from the
            flex parent, not a specific one. Each tile is two nested divs --
            an outer one at the exact percentage bounds (pure layout, no gap
            between tiles) and an inner one inset by a couple of pixels,
            which is what actually creates the seam: the card's own
            background shows through the inset rather than a drawn border. */}
        <div className="relative mt-2 min-h-[240px] flex-1 overflow-hidden rounded-lg">
          {tiles.map((t) => {
            const showLine1 = t.rect.w > 55 && t.rect.h > 30;
            const showLine2 = showLine1 && t.rect.h > 55;
            return (
              <div
                key={t.ticker}
                className="absolute"
                style={{
                  left: `${(t.rect.x / CANVAS.w) * 100}%`,
                  top: `${(t.rect.y / CANVAS.h) * 100}%`,
                  width: `${(t.rect.w / CANVAS.w) * 100}%`,
                  height: `${(t.rect.h / CANVAS.h) * 100}%`,
                }}
              >
                <div
                  title={`${t.ticker} — ${t.sector} — ${money(t.deployed)} (${t.pctOfTotal.toFixed(1)}% of book)`}
                  className="absolute flex flex-col items-center justify-center overflow-hidden rounded-[3px] text-center"
                  style={{
                    inset: '1.5px',
                    backgroundColor: t.color,
                    backgroundImage: t.isUntargeted ? HATCH_BG : undefined,
                  }}
                >
                  {showLine1 && (
                    <span className="px-1 text-[11px] font-bold leading-tight text-slate-100">
                      {t.ticker}
                    </span>
                  )}
                  {showLine2 && (
                    <span className="px-1 font-mono text-[9px] leading-tight text-slate-300/80">
                      {compactMoney(t.deployed)} · {t.pctOfTotal.toFixed(1)}%
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
          {groups
            .filter((g) => g.totalDeployed > 0)
            .map((g) => (
              <div key={g.name} className="flex items-center gap-1 text-[10px] text-slate-300">
                <span
                  className="h-2 w-2 shrink-0 rounded-sm"
                  style={{
                    backgroundColor: g.color,
                    backgroundImage: g.name === 'No Target Set' ? HATCH_BG : undefined,
                  }}
                />
                {g.name}
              </div>
            ))}
        </div>
      </div>

      {/* ---------------- grouped progress table ---------------- */}
      <div>
        <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-300">
          Progress against target
        </div>
        <div className="text-[10px] text-obsidian-muted">Grouped by sector, most under-funded first</div>

        {dca && (
          <div className="mt-2 flex items-start gap-2 rounded-lg border border-sky-500/25 bg-sky-500/5 px-3 py-2.5">
            <PiggyBank className="mt-0.5 h-4 w-4 shrink-0 text-sky-400" />
            <div className="text-[11px] leading-relaxed text-sky-100">
              <span className="font-semibold">{dca.ticker} is furthest from its target</span> —{' '}
              {money(dca.deployed)} of {money(dca.target as number)} deployed (
              {(dca.fundedPct ?? 0).toFixed(0)}%). Next DCA suggestion:{' '}
              <span className="font-semibold">
                {money(dca.gap as number)} into {dca.ticker}
              </span>
              .
            </div>
          </div>
        )}

        <div className="mt-2 space-y-0.5">
          {groups.map((g) => (
            <div key={g.name}>
              <button
                type="button"
                onClick={() => toggle(g.name)}
                className="flex w-full items-center gap-1.5 rounded px-1 py-1 text-left transition-colors hover:bg-slate-700/25"
              >
                {collapsed.has(g.name) ? (
                  <ChevronRight className="h-3 w-3 shrink-0 text-obsidian-muted" />
                ) : (
                  <ChevronDown className="h-3 w-3 shrink-0 text-obsidian-muted" />
                )}
                <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: g.color }} />
                <span className="text-[11px] font-semibold text-slate-200">{g.name}</span>
                <span className="font-mono text-[10px] text-obsidian-muted">{money(g.totalDeployed)}</span>
              </button>

              {!collapsed.has(g.name) &&
                g.rows.map((r) => (
                  <div
                    key={r.ticker}
                    className="grid grid-cols-[52px_1fr_40px_92px] items-center gap-2 rounded px-1 py-1 pl-6 text-[11px] hover:bg-slate-700/15"
                  >
                    <span className="font-medium text-slate-300">{r.ticker}</span>

                    {r.status === 'no-target' ? (
                      <div className="h-1.5 rounded-full bg-obsidian-border" />
                    ) : (
                      <div className="h-1.5 overflow-hidden rounded-full bg-obsidian-border">
                        <div
                          className={`h-full rounded-full ${
                            r.status === 'short' ? 'bg-amber-400' : 'bg-emerald-400'
                          }`}
                          style={{ width: `${Math.min(100, r.fundedPct ?? 0)}%` }}
                        />
                      </div>
                    )}

                    <span className="text-right font-mono text-obsidian-muted">
                      {r.status === 'no-target' ? '—' : `${(r.fundedPct ?? 0).toFixed(0)}%`}
                    </span>

                    {r.status === 'no-target' ? (
                      <button
                        type="button"
                        onClick={() => onSetTarget(r.ticker)}
                        className="truncate text-right text-[10px] text-slate-500 underline decoration-dotted transition-colors hover:text-slate-300"
                      >
                        Set a target →
                      </button>
                    ) : (
                      <span
                        className={`rounded px-1.5 py-0.5 text-center text-[10px] font-medium ${
                          r.status === 'short'
                            ? 'bg-amber-500/10 text-amber-300'
                            : 'bg-win-glow text-win'
                        }`}
                      >
                        {r.status === 'short' ? `${money(r.gap as number)} short` : 'funded'}
                      </span>
                    )}
                  </div>
                ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

export default AllocationPanel;
