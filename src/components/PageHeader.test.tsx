/**
 * PageHeader: the title bar every page except the dashboard now shares.
 *
 * Scoped to the two things the refactor was actually for -- the heading level,
 * and the fact that all six back-links point at the same place -- plus the one
 * escape hatch (`maxWidthClass`) that exists so the investment book's wider
 * table does not sit out of register with its own header.
 *
 * Pure presentation: no hooks, no queries, no router. Nothing to mock.
 */

import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { BookText } from 'lucide-react';

import { PageHeader } from '@/components/PageHeader';

afterEach(cleanup);

const mount = (props: Partial<React.ComponentProps<typeof PageHeader>> = {}) =>
  render(
    <PageHeader
      icon={BookText}
      title="TRADE JOURNAL"
      subtitle="Every trade, open and closed"
      {...props}
    />
  );

describe('PageHeader', () => {
  it('renders the title as the page h1, not a styled span', () => {
    // The defect this component was written to fix: analytics, strategies and
    // settings each rendered their title as a <span>, so those pages had no
    // top-level heading at all -- nothing to jump to, and no outline.
    mount();
    const heading = screen.getByRole('heading', { level: 1 });
    expect(heading).toHaveTextContent('TRADE JOURNAL');
  });

  it('renders exactly one h1, so the outline has a single root', () => {
    mount();
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });

  it('shows the subtitle alongside it', () => {
    mount();
    expect(screen.getByText('Every trade, open and closed')).toBeInTheDocument();
  });

  it('always links back to the dashboard, which is why the href is not a prop', () => {
    mount();
    expect(screen.getByRole('link', { name: /Dashboard/ })).toHaveAttribute('href', '/');
  });

  it('caps at max-w-7xl by default', () => {
    const { container } = mount();
    expect(container.querySelector('.max-w-7xl')).not.toBeNull();
  });

  it('honours a wider cap, so a wide page and its header stay in register', () => {
    // The investment table is wider than every other page. A header pinned to
    // max-w-7xl above it would be visibly narrower than the content it names.
    const { container } = mount({ maxWidthClass: 'max-w-[1400px]' });
    expect(container.querySelector('.max-w-\\[1400px\\]')).not.toBeNull();
    expect(container.querySelector('.max-w-7xl')).toBeNull();
  });

  it('caps its height with min-h, not h, so a wrapped subtitle cannot spill out', () => {
    // The bug this replaced: `h-16` is a fixed 64px, and at 375px a subtitle
    // that wraps to two lines needs ~76px -- it rendered straight through the
    // header's bottom border on four of the six pages. Asserted on the class
    // because jsdom has no layout to measure; the growth itself was checked
    // in a real browser at 375px.
    const { container } = mount();
    const row = container.querySelector('header > div')!;
    expect(row.className).toContain('min-h-16');
    expect(row.className).not.toMatch(/(^|\s)h-16(\s|$)/);
  });

  it('defaults the accent to win and applies a named one when asked', () => {
    const { container: dflt } = mount();
    expect(dflt.querySelector('.border-win\\/30')).not.toBeNull();

    cleanup();
    const { container: sky } = mount({ accent: 'sky' });
    expect(sky.querySelector('.border-sky-500\\/30')).not.toBeNull();
    expect(sky.querySelector('.border-win\\/30')).toBeNull();
  });
});
