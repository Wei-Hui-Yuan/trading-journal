/**
 * The day x session grid, and specifically how hard it is allowed to imply
 * a pattern.
 *
 * This component had no tests. It is added now because the change it is
 * being tested for is about restraint rather than function: the grid renders
 * a one-trade cell with the same colour, glow and weight as a twenty-six
 * trade cell, and colour is what the eye reads as a finding. On the ledger
 * this was written against, seven of twelve populated cells hold one to four
 * trades while five hold twenty-three to twenty-six -- so "Wednesday
 * After-Hours loses money" was a sentence the chart was happy to imply from a
 * single trade.
 *
 * Nothing is hidden. A thin cell keeps its number and its sign; what it loses
 * is the emphasis.
 */

import React from 'react';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { DayOfWeekHeatmap } from '@/components/DayOfWeekHeatmap';
import type { DayName, Heatmap, HeatmapCell, SessionName } from '@/types/api';

afterEach(() => cleanup());

const DAYS: DayName[] = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'];
const SESSIONS: SessionName[] = ['Morning', 'Midday', 'Afternoon', 'After-Hours'];

function cell(overrides: Partial<HeatmapCell> = {}): HeatmapCell {
  return {
    trade_count: 0,
    net_pnl: 0,
    win_rate_pct: 0,
    profit_factor: 0,
    ...overrides,
  };
}

/** An empty grid with `overrides` painted onto specific cells. */
function heatmap(
  overrides: Partial<Record<DayName, Partial<Record<SessionName, HeatmapCell>>>> = {}
): Heatmap {
  const cells = Object.fromEntries(
    DAYS.map((day) => [
      day,
      Object.fromEntries(
        SESSIONS.map((s) => [s, overrides[day]?.[s] ?? cell()])
      ),
    ])
  ) as Heatmap['cells'];

  return {
    days: DAYS,
    sessions: SESSIONS,
    cells,
    day_totals: Object.fromEntries(
      DAYS.map((day) => [
        day,
        {
          trade_count: SESSIONS.reduce((n, s) => n + cells[day][s].trade_count, 0),
          net_pnl: SESSIONS.reduce((n, s) => n + cells[day][s].net_pnl, 0),
        },
      ])
    ) as Heatmap['day_totals'],
    session_totals: Object.fromEntries(
      SESSIONS.map((s) => [
        s,
        {
          trade_count: DAYS.reduce((n, day) => n + cells[day][s].trade_count, 0),
          net_pnl: DAYS.reduce((n, day) => n + cells[day][s].net_pnl, 0),
        },
      ])
    ) as Heatmap['session_totals'],
    timezone: 'America/New_York',
    excluded_weekend_trades: 0,
  };
}

/**
 * The rendered cell box for one day/session, by its own displayed figure.
 *
 * `getAllByText`, not `getByText`: the row header prints the day TOTAL, which
 * on a grid with a single populated cell is the same string as the cell. Only
 * the cell sits inside a `div.relative`, so that is what disambiguates them.
 */
function cellBox(pnlText: string): HTMLElement {
  const box = screen
    .getAllByText(pnlText)
    .map((node) => node.closest('div.relative'))
    .find((found): found is HTMLElement => found !== null);
  if (!box) throw new Error(`No cell box found around "${pnlText}".`);
  return box;
}

describe('states', () => {
  it('reports loading without claiming a grid', () => {
    render(<DayOfWeekHeatmap isLoading />);
    expect(screen.getByText(/Loading performance grid/)).toBeInTheDocument();
  });

  it('surfaces an error message rather than an empty grid', () => {
    render(<DayOfWeekHeatmap error={new Error('backend is down')} />);
    expect(screen.getByText('backend is down')).toBeInTheDocument();
  });

  it('says there is no data when there is none', () => {
    render(<DayOfWeekHeatmap />);
    expect(screen.getByText(/No performance data yet/)).toBeInTheDocument();
  });
});

describe('a cell with a real sample', () => {
  it('gets the confident colour and no thin marking', () => {
    render(
      <DayOfWeekHeatmap
        data={heatmap({
          Friday: { Morning: cell({ trade_count: 26, net_pnl: 299.82, win_rate_pct: 46, profit_factor: 1.6 }) },
        })}
      />
    );

    const box = cellBox('+$299.82');
    expect(box.className).toContain('shadow-win-glow');
    expect(box.className).not.toContain('border-dashed');
    expect(within(box).queryByText('thin')).not.toBeInTheDocument();
  });

  it('keeps the loss glow on a heavily-traded losing cell', () => {
    render(
      <DayOfWeekHeatmap
        data={heatmap({
          Tuesday: { Morning: cell({ trade_count: 23, net_pnl: -196.02, profit_factor: 0.4 }) },
        })}
      />
    );

    const box = cellBox('-$196.02');
    expect(box.className).toContain('shadow-loss-glow');
    expect(box.className).not.toContain('border-dashed');
  });
});

describe('a thin cell', () => {
  it('keeps its number and sign but loses the emphasis', () => {
    render(
      <DayOfWeekHeatmap
        data={heatmap({
          Wednesday: { 'After-Hours': cell({ trade_count: 1, net_pnl: -23.6, profit_factor: 0 }) },
        })}
      />
    );

    // "-$23.6", not "-$23.60": this component has its OWN `money` helper
    // that sets only maximumFractionDigits, where the shared formatMoney in
    // src/lib/format.ts sets both bounds and pads the cents. Pinned as the
    // current behaviour rather than corrected here -- consolidating the two
    // formatters changes every cell's display and is not this change.
    const box = cellBox('-$23.6');
    // The figure is still there -- one trade is real history.
    expect(within(box).getByText('1 trade')).toBeInTheDocument();
    // What it loses is the glow that reads as a pattern.
    expect(box.className).toContain('border-dashed');
    expect(box.className).not.toContain('shadow-loss-glow');
    expect(within(box).getByText('thin')).toBeInTheDocument();
  });

  it('mutes a thin winning cell too, not only losses', () => {
    render(
      <DayOfWeekHeatmap
        data={heatmap({
          Monday: { Midday: cell({ trade_count: 2, net_pnl: 40, profit_factor: 2 }) },
        })}
      />
    );

    const box = cellBox('+$40');
    expect(box.className).toContain('border-dashed');
    expect(box.className).not.toContain('shadow-win-glow');
  });

  it('withholds the perfect-session gold from a two-trade cell', () => {
    // profit_factor null is the backend's "no losses at all". At n=2 that is
    // nearly guaranteed, and it is the strongest claim the grid can make.
    render(
      <DayOfWeekHeatmap
        data={heatmap({
          Friday: { Afternoon: cell({ trade_count: 2, net_pnl: 80, win_rate_pct: 100, profit_factor: null }) },
        })}
      />
    );

    const box = cellBox('+$80');
    expect(box.className).not.toContain('border-amber-400/60');
    expect(box.className).toContain('border-dashed');
  });

  it('still awards the gold once the sample is there', () => {
    render(
      <DayOfWeekHeatmap
        data={heatmap({
          Friday: { Morning: cell({ trade_count: 25, net_pnl: 900, win_rate_pct: 100, profit_factor: null }) },
        })}
      />
    );

    const box = cellBox('+$900');
    expect(box.className).toContain('border-amber-400/60');
    expect(box.className).not.toContain('border-dashed');
  });
});

describe('the legend', () => {
  it('explains the dashed treatment rather than leaving it to be guessed', () => {
    render(<DayOfWeekHeatmap data={heatmap()} />);
    expect(screen.getByText(/Under 20 trades/)).toBeInTheDocument();
  });
});

describe('an empty cell', () => {
  it('is not called thin, because there is nothing to be thin about', () => {
    render(<DayOfWeekHeatmap data={heatmap()} />);

    expect(screen.queryByText('thin')).not.toBeInTheDocument();
    // Every cell, plus the legend's own swatch label.
    expect(screen.getAllByText('No trades').length).toBe(
      DAYS.length * SESSIONS.length + 1
    );
  });
});
