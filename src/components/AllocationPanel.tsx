'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, PiggyBank } from 'lucide-react';

import { useDeleteSectorColor, useSectorColors, useSetSectorColor } from '@/hooks/useInvestments';
import type { Holding } from '@/types/investments';

const money = (value: number) =>
  value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Whole dollars. The deployed/target pair in the progress table is a
 * progress read, not an accounting one, and two sets of cents is exactly what
 * overruns its column once a target reaches six figures. The basis to the
 * cent is one row away in the holdings table. */
const wholeMoney = (value: number) =>
  value.toLocaleString('en-US', { maximumFractionDigits: 0 });

/**
 * One row of the progress table: ticker, bar, deployed/target, funded %, and
 * either the shortfall or the button that sets a target.
 *
 * The deployed/target column is dropped below `sm`, and the track list drops
 * with it. 294px of fixed columns plus their gaps does not fit the ~267px a
 * 375px viewport leaves once the page, the card and this row have each taken
 * their padding, and the cell that would absorb the overrun is the `1fr`
 * progress bar -- the only one here that is not a number readable somewhere
 * else. `hidden` takes the span out of the grid entirely rather than merely
 * blanking it, so the four children that remain land on the four tracks
 * named first.
 */
const PROGRESS_ROW =
  'grid grid-cols-[48px_1fr_36px_85px] items-center gap-2 rounded px-1 py-1 pl-6 ' +
  'text-[11px] hover:bg-slate-700/15 sm:grid-cols-[48px_1fr_125px_36px_85px]';

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

/**
 * What the FILL of a tile means. Area is always cost basis and never changes
 * -- see the panel doc below -- so this only ever reassigns the color
 * channel.
 *
 * Sector identity does not disappear in the performance modes: it moves from
 * the fill to the labelled container each block sits in, which is how a
 * market heatmap has always done it. That is what frees the color channel up
 * in the first place.
 */
type ColorMode = 'sector' | 'pnl' | 'day';

const COLOR_MODES: { key: ColorMode; label: string; title: string }[] = [
  { key: 'sector', label: 'Sector', title: 'Color by sector' },
  { key: 'pnl', label: 'P&L', title: 'Color by unrealized P&L since purchase' },
  { key: 'day', label: '1D', title: "Color by the day's move, as of the last price refresh" },
];

/**
 * Where the scale reaches full saturation, in whole percent. Everything
 * beyond clamps.
 *
 * Two domains rather than one because the measures live on entirely
 * different scales: 3% is a large day, while 3% on a position held for years
 * is noise. A single shared domain would render one of the two maps almost
 * flat and the other almost uniformly saturated.
 *
 * 50 rather than a tighter number for P&L because a book held for years
 * genuinely spreads that far -- at 25 this one clipped nearly every winner to
 * the same flat green, which is the failure mode a heatmap exists to avoid:
 * the gradient has to spend its range where the holdings actually sit, not
 * where a day trader's would. The big multi-baggers still clip, and that is
 * the intended trade -- discriminating among the middle of the book matters
 * more than ranking the two names that ran away with it.
 */
const PERF_DOMAIN: Record<'pnl' | 'day', number> = { pnl: 50, day: 3 };

type RGB = [number, number, number];

// A step down from screen-primary red/green, in keeping with the sector
// palette above: the map is read by comparing tiles to their neighbours, not
// by how loud any single one is.
const PERF_NEUTRAL: RGB = [57, 66, 78];
const PERF_UP: RGB = [18, 128, 92];
const PERF_DOWN: RGB = [169, 52, 70];
/** Deliberately NOT the neutral color. "There is no price for this" and "this
 * has not moved" are different facts and must not look alike. */
const PERF_UNKNOWN = '#232A33';

const mix = (from: RGB, to: RGB, t: number): string =>
  `rgb(${from.map((f, i) => Math.round(f + (to[i] - f) * t)).join(', ')})`;

function perfFill(pct: number | null, domain: number): string {
  if (pct === null || !Number.isFinite(pct)) return PERF_UNKNOWN;
  return mix(PERF_NEUTRAL, pct >= 0 ? PERF_UP : PERF_DOWN, Math.min(Math.abs(pct) / domain, 1));
}

const signedPct = (pct: number | null) =>
  pct === null || !Number.isFinite(pct) ? '—' : `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;

/** Height of a sector block's label strip, and the smallest block that gets
 * one -- below this a header would eat the tiles it is meant to caption. */
const HEADER_PX = 14;
const HEADER_MIN_H = 46;
const HEADER_MIN_W = 64;

/**
 * The legend swatch, made clickable. A real `<input type="color">` sits
 * invisible on top of the visible square so clicking it opens the browser's
 * (on Windows, the OS's) native color picker -- the exact matrix in the
 * screenshot this was built from -- while the swatch itself stays styled the
 * way the rest of the legend already looks.
 *
 * The reset control only appears once a sector actually has an override:
 * there is nothing to revert for a sector still on the built-in palette.
 */
const SectorSwatch: React.FC<{
  name: string;
  color: string;
  hatched: boolean;
  isOverridden: boolean;
  onPick: (color: string) => void;
  onReset: () => void;
}> = ({ name, color, hatched, isOverridden, onPick, onReset }) => (
  <span className="group/swatch relative inline-flex h-3 w-3 shrink-0">
    <input
      type="color"
      value={color}
      onChange={(e) => onPick(e.target.value)}
      title={`Recolor ${name}`}
      aria-label={`Recolor ${name}`}
      className="absolute inset-0 h-full w-full cursor-pointer appearance-none border-0 bg-transparent p-0 opacity-0"
    />
    <span
      className="pointer-events-none h-full w-full rounded-sm ring-1 ring-inset ring-black/25"
      style={{ backgroundColor: color, backgroundImage: hatched ? HATCH_BG : undefined }}
    />
    {isOverridden && (
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onReset();
        }}
        title={`Reset ${name} to its default color`}
        aria-label={`Reset ${name} to its default color`}
        className="absolute -right-1.5 -top-1.5 hidden h-2.5 w-2.5 items-center justify-center rounded-full bg-obsidian-bg text-[7px] leading-none text-obsidian-muted ring-1 ring-obsidian-border hover:text-slate-200 group-hover/swatch:flex"
      >
        ×
      </button>
    )}
  </span>
);

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

/** What a tile can show, given the pixel rect squarify laid it into. */
interface TileLabel {
  showTicker: boolean;
  /** The value/percent line. Full-size horizontal only -- see the note below. */
  showDetail: boolean;
  /** Ticker text should render at the smaller 9px size, still horizontal. */
  compact: boolean;
  /** Ticker text should be set in `writing-mode: vertical-rl`. */
  vertical: boolean;
}

/**
 * A small holding sitting beside a much larger one in the same sector fails
 * the horizontal test on width alone -- SOXX at 7.9% of its sector rendered
 * as a 23x160px sliver, well under the ~44px an 11px ticker needs, while
 * sitting on real area the rest of this function exists to recover.
 *
 * Three tiers, tried in order, each one a fallback for when the last did not
 * fit:
 *
 *  1. Normal (11px): the common case, and the only tier that also earns a
 *     detail line -- that text is longer than a ticker, so even the width
 *     tier 2 accepts would not fit it.
 *  2. Compact (9px), still horizontal: a 4-character ticker fits a 9px font
 *     in ~24px, so the width bar drops to ~26 rather than needing tier 3's
 *     rotation. Horizontal is objectively easier to read than sideways text,
 *     so this is preferred over rotating whenever the width allows it, no
 *     matter how tall the tile is.
 *  3. Vertical, rotated: `writing-mode: vertical-rl` runs the ticker down
 *     the tile's height instead of across its width, recovering tiles too
 *     narrow for even the compact tier -- what has to clear a width bar
 *     becomes `h`, and `w` only has to be thick enough for the glyphs.
 *
 * Each tier's guard excludes every tier before it -- `compact` checks
 * `!horizontal`, `vertical` checks `!horizontal && !compact` -- and both
 * exclusions are load-bearing, not defensive filler: without `!horizontal`,
 * a wide, tall tile would ALSO satisfy vertical's own `h>44 && w>18` and end
 * up rotated on top of its normal label; without `!compact`, the same
 * happens to any tile compact already claims.
 *
 * A tile can still end up with no label at all -- there is no orientation
 * that fits a holding thin enough in both directions, and that limit is
 * real: at 1% of a sector a ticker is a few pixels wide regardless of angle.
 */
export function planTileLabel(rect: { w: number; h: number }): TileLabel {
  const tall = rect.h > 24;
  const horizontal = rect.w > 44 && tall;
  const compact = !horizontal && rect.w > 26 && tall;
  const vertical = !horizontal && !compact && rect.h > 44 && rect.w > 18;
  return {
    showTicker: horizontal || compact || vertical,
    showDetail: horizontal && rect.h > 42,
    compact,
    vertical,
  };
}

interface Tile {
  ticker: string;
  deployed: number;
  pctOfTotal: number;
  /** Both in whole percent. `pnl` is since purchase, `day` is as of the last
   * price refresh; null in either means the figure is not available, which is
   * not zero. */
  pnlPct: number | null;
  dayPct: number | null;
  isUntargeted: boolean;
  /** LOCAL to the sector block, not to the whole canvas. */
  rect: VRect;
}

/** One sector's container: a labelled box with its own tiles laid out inside
 * it. The nesting already existed geometrically -- this is what makes it
 * visible. */
interface SectorBlock {
  name: string;
  color: string;
  totalDeployed: number;
  pctOfTotal: number;
  rect: VRect;
  /** The area the tiles were squarified into: the block minus its header. */
  inner: VRect;
  showHeader: boolean;
  tiles: Tile[];
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

  const [colorMode, setColorMode] = useState<ColorMode>('sector');

  /**
   * The canvas is MEASURED rather than assumed.
   *
   * Squarify optimises for square tiles against the aspect ratio it is given,
   * so a fixed virtual canvas only produces square tiles when the real box
   * happens to share its shape. This one does not -- it is a grid column that
   * stretches to the progress table's height, landing nearer 1:1 than the 2:1
   * that was assumed, which is precisely why the tiles came out as tall
   * ribbons. Feeding real pixels in costs one ResizeObserver and makes the
   * algorithm do what it was chosen for. The fixed CANVAS stays as the
   * first-paint fallback, before any measurement exists.
   */
  const canvasRef = useRef<HTMLDivElement>(null);
  const [canvas, setCanvas] = useState<VRect>(CANVAS);
  useEffect(() => {
    const el = canvasRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setCanvas({ x: 0, y: 0, w: width, h: height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const { data: sectorColorRows } = useSectorColors();
  const setSectorColor = useSetSectorColor();
  const deleteSectorColor = useDeleteSectorColor();

  // Sparse -- only sectors the trader has manually recolored have a row.
  // Everything else keeps resolving through GROUP_COLORS / FALLBACK_COLORS.
  const colorOverrides = useMemo(() => {
    const map: Record<string, string> = {};
    for (const row of sectorColorRows ?? []) map[row.sector] = row.color;
    return map;
  }, [sectorColorRows]);

  const { groups, dca, blocks, priceAsOf, hasDayData } = useMemo(() => {
    const funded = holdings.filter((h) => h.cost_basis > 0);
    const byTicker = new Map(funded.map((h) => [h.ticker, h]));

    const byGroup = new Map<string, Holding[]>();
    for (const h of funded) {
      const key = h.sector || h.category || 'No Target Set';
      if (!byGroup.has(key)) byGroup.set(key, []);
      byGroup.get(key)!.push(h);
    }

    let unknownIdx = 0;
    const built: Group[] = Array.from(byGroup.entries()).map(([name, hs]) => {
      const color = colorOverrides[name] ?? groupColor(name, GROUP_COLORS[name] ? 0 : unknownIdx++);
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
      canvas
    );

    // Tiles are laid out in coordinates LOCAL to their sector block, against
    // the block's real dimensions minus its header. Positioning them as a
    // percentage of that local box, inside a container that is itself a
    // percentage of the canvas, is what keeps the header honest: it takes its
    // pixels from a flex row rather than from an allowance the layout math
    // has to guess at and the rendered box then contradicts.
    const blocks: SectorBlock[] = sectorPlacement.map(({ item: g, rect }) => {
      const showHeader = rect.h >= HEADER_MIN_H && rect.w >= HEADER_MIN_W;
      const inner: VRect = {
        x: 0,
        y: 0,
        w: rect.w,
        h: Math.max(1, rect.h - (showHeader ? HEADER_PX : 0)),
      };
      const tiles: Tile[] = squarifyItems(
        g.rows.filter((r) => r.deployed > 0),
        (r) => r.deployed,
        inner
      ).map(({ item: r, rect: local }) => {
        const h = byTicker.get(r.ticker);
        return {
          ticker: r.ticker,
          deployed: r.deployed,
          pctOfTotal: grandTotal > 0 ? (r.deployed / grandTotal) * 100 : 0,
          pnlPct: h?.unrealized_pnl_pct ?? null,
          dayPct: h?.day_change_pct ?? null,
          isUntargeted: g.name === 'No Target Set',
          rect: local,
        };
      });
      return {
        name: g.name,
        color: g.color,
        totalDeployed: g.totalDeployed,
        pctOfTotal: grandTotal > 0 ? (g.totalDeployed / grandTotal) * 100 : 0,
        rect,
        inner,
        showHeader,
        tiles,
      };
    });

    // Both figures come from the same refresh, so one timestamp covers the
    // price and the day's move alike -- see migration 029.
    const stamps = funded.map((h) => h.price_updated_at).filter((s): s is string => !!s).sort();
    const priceAsOf = stamps.length ? stamps[stamps.length - 1] : null;
    const hasDayData = funded.some((h) => h.day_change_pct !== null);

    return { groups, dca, blocks, priceAsOf, hasDayData };
  }, [holdings, colorOverrides, canvas]);

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
        <div className="flex items-start justify-between gap-2">
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-300">
              Sector treemap
            </div>
            {/* The first clause never changes, in any mode: area is the one
                thing this panel promises, and switching the fill must not
                read as switching what the map is of. */}
            <div className="text-[10px] text-obsidian-muted">
              {colorMode === 'sector'
                ? 'Sized by cost basis, not market value'
                : colorMode === 'pnl'
                  ? 'Sized by cost basis · colored by unrealized P&L'
                  : "Sized by cost basis · colored by the day's move"}
            </div>
          </div>
          <div className="flex shrink-0 gap-0.5 rounded-md border border-obsidian-border p-0.5">
            {COLOR_MODES.map((m) => (
              <button
                key={m.key}
                type="button"
                title={m.title}
                onClick={() => setColorMode(m.key)}
                className={`rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide transition-colors ${
                  colorMode === m.key
                    ? 'bg-obsidian-border text-slate-100'
                    : 'text-obsidian-muted hover:text-slate-300'
                }`}
              >
                {m.label}
              </button>
            ))}
          </div>
        </div>

        {/* Squarified against MEASURED pixels (see `canvas` above), so tiles
            come out square rather than stretched by the gap between the
            assumed and the real aspect ratio.

            Three levels of nesting, each with a job: the sector block at
            exact percentage bounds, a flex column inside it that gives the
            header real pixels and the tile area whatever remains, and each
            tile inset a hair -- the seams between tiles are the card
            background showing through those insets, not drawn borders. */}
        <div
          ref={canvasRef}
          className="relative mt-2 min-h-[240px] flex-1 overflow-hidden rounded-lg"
        >
          {blocks.map((b) => (
            <div
              key={b.name}
              className="absolute"
              style={{
                left: `${(b.rect.x / canvas.w) * 100}%`,
                top: `${(b.rect.y / canvas.h) * 100}%`,
                width: `${(b.rect.w / canvas.w) * 100}%`,
                height: `${(b.rect.h / canvas.h) * 100}%`,
              }}
            >
              <div className="absolute flex flex-col overflow-hidden rounded-[3px]" style={{ inset: '1px' }}>
                {b.showHeader && (
                  <div
                    className="flex shrink-0 items-center gap-1 px-1 text-[9px] uppercase tracking-wide text-slate-400"
                    style={{ height: `${HEADER_PX}px` }}
                  >
                    {/* In the performance modes the fills no longer carry
                        sector, so the dot is what keeps each block tied to
                        the legend below. */}
                    {colorMode !== 'sector' && (
                      <span
                        className="h-1.5 w-1.5 shrink-0 rounded-sm"
                        style={{ backgroundColor: b.color }}
                      />
                    )}
                    <span className="truncate">{b.name}</span>
                    <span className="ml-auto shrink-0 font-mono text-slate-500">
                      {b.pctOfTotal.toFixed(0)}%
                    </span>
                  </div>
                )}
                <div className="relative flex-1">
                  {b.tiles.map((t) => {
                    const { showTicker, showDetail, compact, vertical } = planTileLabel(t.rect);
                    const fill =
                      colorMode === 'sector'
                        ? b.color
                        : perfFill(
                            colorMode === 'pnl' ? t.pnlPct : t.dayPct,
                            PERF_DOMAIN[colorMode]
                          );
                    return (
                      <div
                        key={t.ticker}
                        className="absolute"
                        style={{
                          left: `${(t.rect.x / b.inner.w) * 100}%`,
                          top: `${(t.rect.y / b.inner.h) * 100}%`,
                          width: `${(t.rect.w / b.inner.w) * 100}%`,
                          height: `${(t.rect.h / b.inner.h) * 100}%`,
                        }}
                      >
                        <div
                          title={`${t.ticker} — ${b.name} — ${money(t.deployed)} (${t.pctOfTotal.toFixed(1)}% of book) · P&L ${signedPct(t.pnlPct)} · 1D ${signedPct(t.dayPct)}`}
                          className="absolute flex flex-col items-center justify-center overflow-hidden rounded-[3px] text-center"
                          style={{
                            inset: '1.5px',
                            backgroundColor: fill,
                            backgroundImage: t.isUntargeted ? HATCH_BG : undefined,
                          }}
                        >
                          {showTicker && (
                            <span
                              className={
                                vertical
                                  ? 'px-0.5 text-[10px] font-bold leading-none text-slate-100'
                                  : compact
                                    ? 'px-0.5 text-[9px] font-bold leading-none text-slate-100'
                                    : 'px-1 text-[11px] font-bold leading-tight text-slate-100'
                              }
                              style={
                                vertical
                                  ? { writingMode: 'vertical-rl', textOrientation: 'mixed' }
                                  : undefined
                              }
                            >
                              {t.ticker}
                            </span>
                          )}
                          {showDetail && (
                            <span className="px-1 font-mono text-[9px] leading-tight text-slate-300/80">
                              {colorMode === 'sector'
                                ? `${compactMoney(t.deployed)} · ${t.pctOfTotal.toFixed(1)}%`
                                : signedPct(colorMode === 'pnl' ? t.pnlPct : t.dayPct)}
                            </span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          ))}
        </div>

        {/* The swatch legend doubles as the recolor control, so it stays in
            every mode -- the sector palette still drives the block headers,
            and hiding it would make the picker reachable only by switching
            modes first. */}
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1.5">
          {groups
            .filter((g) => g.totalDeployed > 0)
            .map((g) => (
              <div key={g.name} className="flex items-center gap-1.5 text-[10px] text-slate-300">
                <SectorSwatch
                  name={g.name}
                  color={g.color}
                  hatched={g.name === 'No Target Set'}
                  isOverridden={g.name in colorOverrides}
                  onPick={(color) => setSectorColor.mutate({ sector: g.name, color })}
                  onReset={() => deleteSectorColor.mutate(g.name)}
                />
                {g.name}
              </div>
            ))}
        </div>

        {colorMode === 'sector' ? (
          <div className="mt-1 text-[9px] text-slate-600">Click a swatch to pick its color</div>
        ) : (
          <div className="mt-1.5 flex items-center gap-2 text-[9px] text-obsidian-muted">
            <span className="font-mono">−{PERF_DOMAIN[colorMode]}%</span>
            <div
              className="h-2 flex-1 rounded-sm"
              style={{
                backgroundImage: `linear-gradient(to right, ${mix(PERF_NEUTRAL, PERF_DOWN, 1)}, ${mix(PERF_NEUTRAL, PERF_DOWN, 0)}, ${mix(PERF_NEUTRAL, PERF_UP, 1)})`,
              }}
            />
            <span className="font-mono">+{PERF_DOMAIN[colorMode]}%</span>
            <span className="ml-1 shrink-0">
              {colorMode === 'day'
                ? hasDayData
                  ? priceAsOf
                    ? `as of ${new Date(priceAsOf).toLocaleString()}`
                    : 'as of last refresh'
                  : 'run Refresh prices to populate'
                : 'since purchase'}
            </span>
          </div>
        )}
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
                    className={PROGRESS_ROW}
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

                    <span className="hidden text-right font-mono text-[10px] tabular-nums sm:inline">
                      {r.status === 'no-target' ? (
                        <span className="text-obsidian-muted">{wholeMoney(r.deployed)} / —</span>
                      ) : (
                        <>
                          <span className="text-slate-200">{wholeMoney(r.deployed)}</span>
                          <span className="text-obsidian-muted">
                            {' '}
                            / {wholeMoney(r.target as number)}
                          </span>
                        </>
                      )}
                    </span>

                    <span className="text-right font-mono text-[10px] text-obsidian-muted tabular-nums">
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
