/**
 * EquityCurveChart: scoped to the "Net Realised" caption only.
 *
 * The audit that motivated this file found the caption factually wrong: it
 * claimed open-position money was excluded ("Sum of every closed round
 * trip. Open positions are not included") when `build_equity_curve`
 * deliberately includes it -- confirmed against the backend's own test,
 * `test_the_curve_plots_legs_when_it_has_them` in
 * `api/tests/test_realized_legs.py`, whose fixture is reproduced here: a
 * $100 closed round trip plus a $30 loss banked out of a position still
 * open sums to net_pnl 70, not 100 -- proving the open-position leg is
 * counted, not excluded.
 *
 * The rest of the component (the chart itself, Peak/Max Drawdown/Below
 * Peak Now, loading/error/empty states) was already verified correct by
 * the same audit and is out of scope here.
 */

import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { EquityCurveChart } from '@/components/EquityCurveChart';
import type { EquityCurve } from '@/types/api';

afterEach(cleanup);

/**
 * The exact fixture `test_the_curve_plots_legs_when_it_has_them` proves the
 * claim with: a $100 round trip closing 2026-06-01, and a separate $30 loss
 * banked scaling out of a position that stays open, landing on 2026-06-05 --
 * a date no round trip closed on. net_pnl is 70 (100 - 30), not 100.
 */
const CURVE: EquityCurve = {
  points: [
    { date: '2026-05-31', realized_pnl: 0, cumulative_pnl: 0, peak_pnl: 0, drawdown: 0, trades: 0 },
    { date: '2026-06-01', realized_pnl: 100, cumulative_pnl: 100, peak_pnl: 100, drawdown: 0, trades: 1 },
    { date: '2026-06-05', realized_pnl: -30, cumulative_pnl: 70, peak_pnl: 100, drawdown: -30, trades: 0 },
  ],
  summary: {
    start_date: '2026-06-01',
    end_date: '2026-06-05',
    net_pnl: 70,
    peak_pnl: 100,
    max_drawdown: -30,
    current_drawdown: -30,
    trading_days: 2,
    calendar_days: 5,
    closed_trades: 1,
    truncated: false,
  },
};

describe('EquityCurveChart > Net Realised caption', () => {
  it('states that money from still-open positions is included, not excluded', () => {
    render(<EquityCurveChart data={CURVE} />);
    // The stat's hover hint (native title attribute).
    const stat = screen.getByText('Net Realised').closest('div')!;
    expect(stat.getAttribute('title')).toMatch(/still open/i);
    expect(stat.getAttribute('title')).not.toMatch(/not included/i);
  });

  it('never claims open positions are excluded, in the hint or the footer', () => {
    render(<EquityCurveChart data={CURVE} />);
    // Regression guard for the exact wrong phrase the audit found.
    expect(screen.queryByText(/open positions are not included/i)).not.toBeInTheDocument();
  });

  it('the footer explains the dating rule accurately: by when the dollar was booked, not by when a round trip closed', () => {
    render(<EquityCurveChart data={CURVE} />);
    expect(screen.getByText(/dated by when each dollar was booked/i)).toBeInTheDocument();
    expect(screen.queryByText(/dated by when each round trip closed/i)).not.toBeInTheDocument();
  });

  it('the footer states open-position money is included, and still reports the closed round-trip count separately', () => {
    render(<EquityCurveChart data={CURVE} />);
    expect(
      screen.getByText(/money banked scaling out of positions still open is included/i)
    ).toBeInTheDocument();
    // closed_trades (1) is still a real, separate, correctly-labeled fact --
    // this fix does not touch that figure, only the false exclusion claim.
    expect(screen.getByText(/1 round trips closed/i)).toBeInTheDocument();
  });

  it('renders the actual net_pnl value the fixture proves includes open-position money', () => {
    render(<EquityCurveChart data={CURVE} />);
    // $70, not $100 -- the $30 open-position loss is genuinely subtracted in.
    expect(screen.getByText('+$70.00')).toBeInTheDocument();
  });
});
