/**
 * Commas in an arbitrary track list -- a layout bug that reports nothing.
 *
 * `grid-cols-[1fr,120px,140px]` looks right and is not. Tailwind substitutes
 * underscores for spaces in arbitrary values but leaves a comma alone, so it
 * happily emits `grid-template-columns: 1fr,120px,140px` -- which is invalid
 * CSS. The browser drops the declaration, the element silently falls back to
 * a single column, and every "row" stacks into three lines with a
 * full-width input.
 *
 * Nothing anywhere reports this. Not tsc, not eslint, not the build, not a
 * render test -- jsdom has no layout engine, so even a mounted component
 * looks fine. It shipped in the valuation modal and was found only by a
 * person looking at the screen and calling the form ugly.
 *
 * Commas INSIDE parentheses are legitimate and left alone: `repeat(2,1fr)`
 * and `rgba(0,0,0,.5)` are both correct.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { join, relative } from 'node:path';

import { describe, expect, it } from 'vitest';


/** Every source file under src/, tests excluded. No dependency needed. */
function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sourceFiles(full));
    else if (/.tsx?$/.test(entry.name) && !/.test.tsx?$/.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

/** `grid-cols-[...]` / `grid-rows-[...]`, capturing what is in the brackets. */
const TRACK_LIST = /\b(?:grid-(?:cols|rows))-\[([^\]]+)\]/g;

/** True when a comma sits outside every parenthesised group. */
function hasTopLevelComma(value: string): boolean {
  let depth = 0;
  for (const char of value) {
    if (char === '(') depth += 1;
    else if (char === ')') depth -= 1;
    else if (char === ',' && depth === 0) return true;
  }
  return false;
}

describe('arbitrary Tailwind track lists', () => {
  it('separate tracks with underscores, never commas', () => {
    const files = sourceFiles(join(process.cwd(), 'src'));

    const offenders: string[] = [];
    for (const file of files) {
      const source = readFileSync(file, 'utf8');
      for (const [full, value] of source.matchAll(TRACK_LIST)) {
        if (hasTopLevelComma(value)) {
          offenders.push(`${relative(process.cwd(), file)}: ${full}`);
        }
      }
    }

    expect(offenders).toEqual([]);
  });

  it('detects the exact class that shipped, and clears its fix', () => {
    // Guards the guard: a matcher that quietly matched nothing would make
    // the assertion above pass forever.
    const broken = [...'grid-cols-[1fr,120px,140px]'.matchAll(TRACK_LIST)];
    expect(broken).toHaveLength(1);
    expect(hasTopLevelComma(broken[0][1])).toBe(true);

    const fixed = [...'grid-cols-[1fr_110px_130px]'.matchAll(TRACK_LIST)];
    expect(hasTopLevelComma(fixed[0][1])).toBe(false);
  });

  it('leaves commas inside a function alone', () => {
    // `repeat(2,1fr)` is valid CSS and must not be flagged.
    const [match] = [...'grid-cols-[repeat(2,minmax(0,1fr))]'.matchAll(TRACK_LIST)];
    expect(hasTopLevelComma(match[1])).toBe(false);
  });
});
