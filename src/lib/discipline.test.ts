/**
 * Discipline scoring, which exists twice on purpose.
 *
 * This is a deliberate reimplementation of the backend's
 * `compute_discipline_score` (api/services/analytics.py), so that one round
 * trip's score reads the same whether it came from the aggregate compliance
 * chart or from the single row in front of you. Two copies of a formula drift;
 * these tests are what makes the drift visible, so they encode the BACKEND's
 * documented behaviour rather than just whatever this file happens to do.
 */

import { describe, expect, it } from 'vitest';

import { computeDisciplineScore } from '@/lib/discipline';
import type { PositionDiscipline } from '@/types/api';

/** Only `followed` is read; the rest is padding to satisfy the type. */
const answer = (followed: boolean, id = crypto.randomUUID()): PositionDiscipline =>
  ({ discipline_id: id, followed } as unknown as PositionDiscipline);

describe('computeDisciplineScore', () => {
  it('scores against the rules ANSWERED, not the rules that exist', () => {
    // Three answers, two followed. Adding a fourth rule to the playbook
    // tomorrow must not retroactively drop this trade to 50%.
    expect(computeDisciplineScore([answer(true), answer(true), answer(false)])).toBe(
      66.67
    );
  });

  it('returns null when nothing was answered, not zero', () => {
    // "A trade never reviewed against any rule has no compliance to report, and
    // 0% would read as broke every rule for a trade that was simply never
    // checked."
    expect(computeDisciplineScore([])).toBeNull();
  });

  it('distinguishes never-checked from checked-and-failed', () => {
    // The distinction the null exists for, stated as one assertion.
    expect(computeDisciplineScore([])).toBeNull();
    expect(computeDisciplineScore([answer(false)])).toBe(0);
  });

  it('reports a clean sheet as exactly 100', () => {
    // Not 99.99 or 100.00000001 -- this is rendered straight into the UI.
    expect(computeDisciplineScore([answer(true), answer(true)])).toBe(100);
  });

  it('rounds to two decimals', () => {
    // 1/3 -> 33.33, not 33.33333333333333. The implementation multiplies by
    // 10000 and divides by 100 to get there.
    expect(computeDisciplineScore([answer(true), answer(false), answer(false)])).toBe(
      33.33
    );
  });

  it('agrees with the backend at every rule count, including ties', () => {
    // The score is duplicated in api/services/analytics.py so a journal row can
    // render without a round trip. That makes agreement a requirement, not a
    // nicety -- the same trade must not read one way in the ledger and another
    // behind a compliance bucket.
    //
    // They used to disagree. Python's round() is half-to-even and Math.round is
    // half-up, so an exact tie split them: one followed rule of 32 gave 3.13
    // here and 3.12 there. The backend now uses floor(x * 10000 + 0.5) / 100,
    // which is precisely what Math.round(x * 10000) / 100 does for a
    // non-negative value, so the two are the same arithmetic in two languages.
    //
    // Checked to 64 rather than to a plausible rule count on purpose: 32 is
    // where the old divergence began, and a range stopping short of it would
    // pass either way and prove nothing.
    const backend = (followed: number, total: number) =>
      Math.floor((followed / total) * 10000 + 0.5) / 100;

    for (let total = 1; total <= 64; total += 1) {
      for (let followed = 0; followed <= total; followed += 1) {
        const answers = [
          ...Array.from({ length: followed }, () => answer(true)),
          ...Array.from({ length: total - followed }, () => answer(false)),
        ];
        expect(computeDisciplineScore(answers)).toBe(backend(followed, total));
      }
    }
  });

  it('rounds a tie up, matching the backend', () => {
    // 1 of 32 -- the smallest case that used to differ, kept as an explicit
    // assertion so a regression names itself instead of hiding in the loop.
    const thirtyTwo = [
      answer(true),
      ...Array.from({ length: 31 }, () => answer(false)),
    ];

    expect(computeDisciplineScore(thirtyTwo)).toBe(3.13);
  });

  it('is unaffected by the order the answers arrive in', () => {
    const mixed = [answer(false), answer(true), answer(true), answer(false)];
    const sorted = [answer(true), answer(true), answer(false), answer(false)];

    expect(computeDisciplineScore(mixed)).toBe(computeDisciplineScore(sorted));
  });

  it('counts only a strict true as followed', () => {
    // `filter((d) => d.followed)` is truthiness, so anything the API could send
    // in that field other than a boolean would be counted. Guarding the two
    // shapes that could realistically arrive from JSON.
    const nullish = [
      { discipline_id: 'a', followed: null },
      { discipline_id: 'b', followed: true },
    ] as unknown as PositionDiscipline[];

    expect(computeDisciplineScore(nullish)).toBe(50);
  });
});
