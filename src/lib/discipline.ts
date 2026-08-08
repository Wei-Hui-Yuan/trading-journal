import type { PositionDiscipline } from '@/types/api';

/**
 * One trade's compliance, as a percentage of the rules it was ANSWERED
 * against — never of however many rules exist today.
 *
 * Mirrors the backend's `compute_discipline_score` exactly (see
 * api/services/analytics.py) so a round trip's score reads the same whether
 * it comes from a bucket in the aggregate compliance chart or from this
 * single row. Computed here rather than sent by the API because
 * `RoundTrip.disciplines` already carries everything needed — a second trip
 * to the server for arithmetic this cheap would just be latency.
 *
 * Null, not 0, when nothing was answered. A trade never reviewed against any
 * rule has no compliance to report, and 0% would read as "broke every rule"
 * for a trade that was simply never checked — the same distinction
 * `PositionDiscipline`'s own doc comment draws for a single answer.
 */
export function computeDisciplineScore(
  disciplines: PositionDiscipline[]
): number | null {
  if (disciplines.length === 0) return null;
  const followed = disciplines.filter((d) => d.followed).length;
  return Math.round((followed / disciplines.length) * 10000) / 100;
}
