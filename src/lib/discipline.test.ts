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

  it('agrees with the backend for every rule count that can occur', () => {
    // The two implementations round differently -- Math.round is half-up, and
    // the backend's Python round() is half-to-even -- so "they agree" is a
    // claim with a boundary, and this is it.
    //
    // Checked exhaustively up to 64 answers: the first divergence is at 32
    // answered rules (1 of 32 gives 3.13 here and 3.12 there). The playbook has
    // 5 rules, and the most ever answered on one trade is 5, so nothing
    // reachable today disagrees. If disciplines ever pass 32, the score in a
    // journal row and the score behind a compliance bucket start to differ in
    // the second decimal, and this is the note that says why.
    const pythonRound = (followed: number, total: number) => {
      const scaled = (followed / total) * 100;
      const floored = Math.floor(scaled * 100);
      const remainder = scaled * 100 - floored;
      // Half-to-even, matching Python's round(x, 2).
      if (Math.abs(remainder - 0.5) > Number.EPSILON * 100) {
        return Math.round(scaled * 100) / 100;
      }
      return (floored % 2 === 0 ? floored : floored + 1) / 100;
    };

    for (let total = 1; total <= 12; total += 1) {
      for (let followed = 0; followed <= total; followed += 1) {
        const answers = [
          ...Array.from({ length: followed }, () => answer(true)),
          ...Array.from({ length: total - followed }, () => answer(false)),
        ];
        expect(computeDisciplineScore(answers)).toBeCloseTo(
          pythonRound(followed, total),
          10
        );
      }
    }
  });

  it('rounds half away from zero, where the two implementations part', () => {
    // 1 of 32 is the smallest divergent case. Pinned as the frontend's answer so
    // the difference is documented rather than discovered.
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
