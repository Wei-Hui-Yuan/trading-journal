/**
 * Handing a blob to the browser as a file.
 *
 * Split out from `api.ts` because it is DOM work rather than transport: the
 * bytes are already in hand by the time this runs, and the only question left
 * is how the browser is persuaded to save them.
 */

/**
 * How long the object URL is kept alive after the click.
 *
 * Revoking synchronously is the obvious thing and it is wrong: some browsers
 * have not started reading the URL by the time `click()` returns, and the
 * download is cancelled with no error anywhere. A short delay costs one
 * temporary URL for a moment and removes that class of failure. It is not left
 * unrevoked -- an un-revoked blob URL pins the whole file in memory until the
 * tab closes, and these exports are the largest thing this app produces.
 */
const REVOKE_DELAY_MS = 1_000;

/**
 * Save a blob under `filename`.
 *
 * The anchor is appended to the document before being clicked. A detached
 * element's click is ignored by Firefox, which is the kind of difference that
 * shows up only on the one browser nobody tested.
 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
}
