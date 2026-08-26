/**
 * AllocationPanel: scoped to `planTileLabel` alone.
 *
 * That function decides whether a treemap tile can show its ticker, in which
 * of three tiers (normal, compact, or rotated), from the pixel rect squarify
 * laid it into. It exists as a standalone export specifically so this
 * question could be tested without mounting the panel itself, which needs a
 * ResizeObserver and a real holdings/sector-colors round trip through React
 * Query.
 *
 * The numbers below are not made up. SOXX and FDS are the real rects
 * squarify produced for a real book with SOXX at 7.9% of the ETF sector and
 * FDS at 21.7% of Financials, captured by mounting the actual component with
 * a mocked ResizeObserver reporting the real measured treemap box. Before
 * the tier that recovers each, SOXX rendered with no label at all -- a
 * 23x160px sliver with 3,700px^2 of real area, just not laid out in a
 * direction horizontal text could use -- and FDS rotated unnecessarily,
 * despite having enough width for a smaller horizontal ticker.
 */

import { describe, expect, it } from 'vitest';
import { isDayChangeStale, isTrackedForProgress, planTileLabel } from '@/components/AllocationPanel';

describe('planTileLabel', () => {
  it('shows the ticker and the detail line on a tile with room in both directions', () => {
    const plan = planTileLabel({ w: 100, h: 100 });
    expect(plan.showTicker).toBe(true);
    expect(plan.showDetail).toBe(true);
    // Neither fallback tier: a tile this size clears the width bar every
    // tier accepts, so without the compact/vertical guards checking
    // `!horizontal` first, both would incorrectly also fire here.
    expect(plan.compact).toBe(false);
    expect(plan.vertical).toBe(false);
  });

  it('shows the ticker but drops the detail line once height gets tight', () => {
    // FDS's OLD rect, from before the compact tier existed: wide enough for
    // a full-size ticker, not tall enough for two lines.
    const plan = planTileLabel({ w: 112.8, h: 28.9 });
    expect(plan.showTicker).toBe(true);
    expect(plan.showDetail).toBe(false);
    expect(plan.compact).toBe(false);
    expect(plan.vertical).toBe(false);
  });

  it('rotates the ticker for a tall sliver too narrow for horizontal text', () => {
    // SOXX's real rect -- the one that motivated this function.
    const plan = planTileLabel({ w: 23.4, h: 159.9 });
    expect(plan.showTicker).toBe(true);
    expect(plan.vertical).toBe(true);
  });

  it('never shows the detail line on a rotated tile', () => {
    // Two rotated glyph runs in an already-narrow column reads as clipped
    // rather than small; the tile's tooltip carries the exact figures instead.
    const plan = planTileLabel({ w: 23.4, h: 159.9 });
    expect(plan.showDetail).toBe(false);
  });

  it('does not rotate a tile that is too short for vertical text either', () => {
    // Fails the horizontal test on width, and fails vertical's own height
    // bar too -- there is no orientation with room for a label here.
    const plan = planTileLabel({ w: 30, h: 20 });
    expect(plan.vertical).toBe(false);
    expect(plan.showTicker).toBe(false);
  });

  it('leaves a genuinely too-small tile unlabelled in either orientation', () => {
    // SOXX at 1% of its sector instead of 7.9%: no orientation has room for
    // even one character. This is the real limit, not a bug.
    const plan = planTileLabel({ w: 5.5, h: 58.6 });
    expect(plan.showTicker).toBe(false);
    expect(plan.vertical).toBe(false);
  });

  it('prefers a compact horizontal ticker over rotating, once width allows it', () => {
    // CPRT's rect at a narrower viewport: fails horizontal on width alone by
    // less than 2px, but easily clears the compact tier's ~26px bar. Reading
    // sideways is strictly harder than reading a smaller font upright, so
    // compact wins here even though the tile is also tall enough to rotate.
    const plan = planTileLabel({ w: 42.6, h: 69.8 });
    expect(plan.showTicker).toBe(true);
    expect(plan.compact).toBe(true);
    expect(plan.vertical).toBe(false);
  });

  it('recovers FDS with a compact ticker instead of rotating it', () => {
    // FDS's real rect. Before this tier existed it rotated -- correct, but
    // needlessly: 38.8px is well past the compact bar, so upright text at a
    // smaller size reads easier than the same four characters sideways.
    const plan = planTileLabel({ w: 38.8, h: 113.8 });
    expect(plan.showTicker).toBe(true);
    expect(plan.compact).toBe(true);
    expect(plan.vertical).toBe(false);
  });

  it('keeps SOXX rotated -- still under the compact bar even with this tier added', () => {
    // SOXX's real rect, width unchanged at ~25px: this tier recovers tiles
    // between roughly 26 and 44px wide. SOXX sits just below that band, so
    // rotation remains the only orientation with room for it.
    const plan = planTileLabel({ w: 25.3, h: 207.4 });
    expect(plan.compact).toBe(false);
    expect(plan.vertical).toBe(true);
  });

  it('requires the width to clear the compact bar, not merely meet it', () => {
    const atBar = planTileLabel({ w: 26, h: 100 });
    expect(atBar.compact).toBe(false);
    expect(atBar.vertical).toBe(true); // still tall enough to rotate instead

    const justOver = planTileLabel({ w: 26.01, h: 100 });
    expect(justOver.compact).toBe(true);
    expect(justOver.vertical).toBe(false);
  });

  it('never shows the detail line on a compact tile either', () => {
    // The value+percent text is longer than a ticker, so the ~26px the
    // compact tier accepts is not enough for it even at the same 9px size.
    const plan = planTileLabel({ w: 30, h: 100 });
    expect(plan.compact).toBe(true);
    expect(plan.showDetail).toBe(false);
  });

  it('does not let a compact-eligible tile fall through to rotation', () => {
    // Guards the exclusion itself: this rect clears BOTH the compact bar
    // (w>26) and vertical's own bar (h>44, w>18) at once. Dropping either
    // tier's negative guard would make this tile rotate on top of, or
    // instead of, its compact label.
    const plan = planTileLabel({ w: 30, h: 100 });
    expect(plan.compact).toBe(true);
    expect(plan.vertical).toBe(false);
  });

  it('reads the compact bar off width, not height', () => {
    // Guards against a w/h mix-up in the compact branch, the same way the
    // existing vertical test guards that branch. w=30 clears the ~26px bar
    // and h=25 does not (tall enough at >24 to reach this branch at all, but
    // not >26) -- a swapped comparison would check the wrong dimension and
    // wrongly deny this tile any label, since it is also too short to rotate.
    const plan = planTileLabel({ w: 30, h: 25 });
    expect(plan.showTicker).toBe(true);
    expect(plan.compact).toBe(true);
  });

  it('rotates only using height, never width, as the long axis', () => {
    // Guards against a w/h mix-up in the vertical branch. This rect fails
    // BOTH the horizontal height bar (h=20, needs >24) and vertical's height
    // bar (needs >44) -- but its width alone (60) would clear either
    // dimension's threshold, so a swapped comparison would wrongly rotate it.
    const plan = planTileLabel({ w: 60, h: 20 });
    expect(plan.vertical).toBe(false);
    expect(plan.showTicker).toBe(false);
  });
});

/**
 * isDayChangeStale: backs the treemap's "day" color mode.
 *
 * refresh_prices (api/main.py) advances price_updated_at on every successful
 * quote but only advances day_change_updated_at when the provider's response
 * actually included a day change that time -- confirmed real via
 * fetch_quote's own docstring in api/services/market_data.py and pinned by
 * test_a_missing_day_change_leaves_the_last_one_standing in
 * api/tests/test_price_refresh.py. Left unguarded, the treemap would color a
 * tile by a number that is not from today's price, with the "as of" caption
 * next to it implying it is. These tests are the frontend half of that
 * failsafe -- the exact match/mismatch check the audit asked for.
 */
describe('isDayChangeStale', () => {
  const FRESH = '2026-06-05T13:00:00+00:00';
  const OLDER = '2026-06-04T13:00:00+00:00';

  it('is not stale when the day figure and price came from the same refresh', () => {
    expect(
      isDayChangeStale({
        day_change_pct: -1.09,
        day_change_updated_at: FRESH,
        price_updated_at: FRESH,
      })
    ).toBe(false);
  });

  it('is stale when the day figure predates the current price', () => {
    // Exactly the scenario the migration exists for: a later refresh got a
    // new price but the provider omitted changePercentage that time, so
    // day_change_updated_at was left at the prior refresh's stamp.
    expect(
      isDayChangeStale({
        day_change_pct: -1.09,
        day_change_updated_at: OLDER,
        price_updated_at: FRESH,
      })
    ).toBe(true);
  });

  it('is never stale when there is no day figure at all', () => {
    // "No data" and "old data" are different facts -- see PERF_UNKNOWN. A
    // holding refresh_prices has never gotten a day change for must not be
    // flagged as if it once had a fresher one.
    expect(
      isDayChangeStale({
        day_change_pct: null,
        day_change_updated_at: null,
        price_updated_at: FRESH,
      })
    ).toBe(false);
  });

  it('treats a day figure with no recorded write time as stale', () => {
    // Should not arise post-migration (the backfill sets it whenever
    // day_change_pct is set), but a day figure this component cannot prove
    // is current must not default to looking fresh.
    expect(
      isDayChangeStale({
        day_change_pct: -1.09,
        day_change_updated_at: null,
        price_updated_at: FRESH,
      })
    ).toBe(true);
  });
});

/**
 * isTrackedForProgress: decides which holdings reach "Progress against
 * target" (and so are eligible to be the DCA suggestion).
 *
 * Before this existed, the panel used ONE filter (cost_basis > 0) to gate
 * both the treemap AND the progress table. That hid a holding with a real
 * target but zero cost basis -- a position planned but not yet bought,
 * explicitly supported by AddHoldingModal's own "build up over several
 * purchases" use case -- from the table entirely, even though a 0%-funded
 * holding is the most under-funded thing in the book by definition. This
 * predicate is deliberately WIDER than a treemap tile's requirement: the
 * treemap's own exclusion of a $0 holding happens elsewhere, by filtering
 * on deployed/totalDeployed, not by narrowing who reaches the table.
 */
describe('isTrackedForProgress', () => {
  it('tracks a holding with money deployed, target or not', () => {
    expect(isTrackedForProgress({ cost_basis: 500, planned_allocation: null })).toBe(true);
  });

  it('tracks a holding with a real target but nothing bought yet', () => {
    // The exact case the audit found missing: planned, not funded.
    expect(isTrackedForProgress({ cost_basis: 0, planned_allocation: 2000 })).toBe(true);
  });

  it('does not track a holding with neither money nor a target', () => {
    // Nothing to show progress on -- correctly excluded, not a gap.
    expect(isTrackedForProgress({ cost_basis: 0, planned_allocation: null })).toBe(false);
  });

  it('does not track a holding with a zero or negative target and no money', () => {
    // A cleared target (0) must not be treated as "planned" -- matches the
    // `planned_allocation && planned_allocation > 0` check used elsewhere in
    // this file to decide whether a target is real.
    expect(isTrackedForProgress({ cost_basis: 0, planned_allocation: 0 })).toBe(false);
  });
});
