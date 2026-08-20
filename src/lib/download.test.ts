/**
 * Handing a blob to the browser as a file.
 *
 * Eight lines of very ordinary DOM code, and three of them are load-bearing in
 * ways that are invisible if they regress:
 *
 *   * the anchor must be IN the document when clicked, or Firefox ignores it
 *   * the object URL must be revoked, or the whole file stays pinned in memory
 *     until the tab closes -- and these exports are the largest thing this app
 *     produces
 *   * the revoke must NOT be synchronous, or some browsers cancel the download
 *     before they have finished reading the URL
 *
 * The first two fail closed and loudly. The third fails on some browsers only,
 * silently, which is why it is deferred and why that deferral is pinned here.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { saveBlob } from '@/lib/download';

/** What the anchor looked like at the moment click() was called. */
interface ClickSnapshot {
  download: string;
  href: string;
  rel: string;
  inDocument: boolean;
}

let clicks: ClickSnapshot[];
let created: Blob[];
let revoked: string[];

beforeEach(() => {
  clicks = [];
  created = [];
  revoked = [];

  vi.useFakeTimers();

  // jsdom implements neither of these.
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: (blob: Blob) => {
      created.push(blob);
      return `blob:mock/${created.length}`;
    },
    revokeObjectURL: (url: string) => {
      revoked.push(url);
    },
  });

  // Recording the state at click time, and deliberately not calling through:
  // a real anchor click in jsdom logs a "navigation not implemented" error.
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement
  ) {
    clicks.push({
      download: this.download,
      href: this.href,
      rel: this.rel,
      inDocument: document.body.contains(this),
    });
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('saveBlob', () => {
  it('clicks an anchor carrying the filename and a blob URL', () => {
    saveBlob(new Blob(['a,b\n1,2']), 'trading-executions-2026-08-19.csv');

    expect(clicks).toHaveLength(1);
    expect(clicks[0].download).toBe('trading-executions-2026-08-19.csv');
    expect(clicks[0].href).toMatch(/^blob:/);
  });

  it('has the anchor attached to the document when it clicks it', () => {
    // Firefox ignores a click on a detached element. This is the assertion that
    // catches someone "simplifying away" the appendChild.
    saveBlob(new Blob(['x']), 'x.csv');

    expect(clicks[0].inDocument).toBe(true);
  });

  it('leaves nothing behind in the DOM', () => {
    saveBlob(new Blob(['x']), 'x.csv');

    expect(document.querySelectorAll('a[download]')).toHaveLength(0);
  });

  it('does not revoke the URL before the browser has read it', () => {
    // Synchronous revocation is the obvious implementation and it cancels the
    // download on some browsers, with no error anywhere.
    saveBlob(new Blob(['x']), 'x.csv');

    expect(revoked).toEqual([]);
  });

  it('does revoke the URL once the delay has passed', () => {
    // The other half: not revoking at all pins the file in memory for the life
    // of the tab.
    saveBlob(new Blob(['x']), 'x.csv');

    vi.advanceTimersByTime(1_000);

    expect(revoked).toEqual(['blob:mock/1']);
  });

  it('revokes exactly the URL it created, not a stale one', () => {
    saveBlob(new Blob(['one']), 'one.csv');
    saveBlob(new Blob(['two']), 'two.csv');

    vi.advanceTimersByTime(1_000);

    expect(revoked).toEqual(['blob:mock/1', 'blob:mock/2']);
    expect(clicks.map((c) => c.download)).toEqual(['one.csv', 'two.csv']);
  });

  it('passes the blob through untouched', async () => {
    const blob = new Blob(['\ufeffticker,quantity\r\nCRWD,0.00000001\r\n'], {
      type: 'text/csv',
    });

    saveBlob(blob, 'x.csv');

    expect(created).toHaveLength(1);
    // The same object, not a re-encoded copy.
    expect(created[0]).toBe(blob);

    // Checked as BYTES rather than via text(). Blob.text() decodes as UTF-8 and
    // strips a leading BOM per spec, so the string form cannot see the one byte
    // sequence that matters most to Excel.
    const bytes = new Uint8Array(await created[0].arrayBuffer());
    expect([...bytes.slice(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);

    // And the fractional quantity is still fixed-point rather than 1E-8.
    await expect(created[0].text()).resolves.toContain('0.00000001');
  });

  it('sets rel=noopener on the anchor it appends', () => {
    // The anchor goes into the live document, so the attribute is worth having
    // even though a blob: URL cannot reach an opener.
    saveBlob(new Blob(['x']), 'x.csv');

    expect(clicks[0].rel).toBe('noopener');
  });
});
