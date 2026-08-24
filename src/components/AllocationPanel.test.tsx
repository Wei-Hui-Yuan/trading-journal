/**
 * AllocationPanel: scoped to `planTileLabel` alone.
 *
 * That function decides whether a treemap tile can show its ticker, and in
 * which orientation, from the pixel rect squarify laid it into. It exists as
 * a standalone export specifically so this question could be tested without
 * mounting the panel itself, which needs a ResizeObserver and a real
 * holdings/sector-colors round trip through React Query.
 *
 * The numbers below are not made up: they are the actual rects squarify
 * produced for a real book with SOXX at 7.9% of the ETF sector, reproduced
 * by hand-running the algorithm before this fix existed. SOXX rendered with
 * no label at all -- a 23x160px sliver with 3,700px^2 of real area, just not
 * laid out in a direction horizontal text could use.
 */

import { describe, expect, it } from 'vitest';
import { planTileLabel } from '@/components/AllocationPanel';

describe('planTileLabel', () => {
  it('shows the ticker and the detail line on a tile with room in both directions', () => {
    const plan = planTileLabel({ w: 100, h: 100 });
    expect(plan.showTicker).toBe(true);
    expect(plan.showDetail).toBe(true);
    expect(plan.vertical).toBe(false);
  });

  it('shows the ticker but drops the detail line once height gets tight', () => {
    // FDS's real rect: wide enough for a ticker, not tall enough for two lines.
    const plan = planTileLabel({ w: 112.8, h: 28.9 });
    expect(plan.showTicker).toBe(true);
    expect(plan.showDetail).toBe(false);
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

  it('rotates a near-square tile once it clears the height-over-width bar', () => {
    // CPRT's rect at a narrower viewport: almost square, fails horizontal on
    // width alone by less than 2px. Confirms the fallback engages exactly at
    // the boundary this function defines, not only for extreme slivers.
    const plan = planTileLabel({ w: 42.6, h: 69.8 });
    expect(plan.showTicker).toBe(true);
    expect(plan.vertical).toBe(true);
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
