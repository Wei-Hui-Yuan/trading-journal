/**
 * Turn a pasted or picked chart screenshot into the smallest byte-identical
 * encoding the browser can produce.
 *
 * Two decisions here run against the obvious ones, and both were measured on
 * a 1866x1244 thinkorswim capture rather than assumed:
 *
 * NO DOWNSCALING. Resizing a chart is not like resizing a photo. The image is
 * mostly flat fills, hairline moving averages and small text, so a resample
 * replaces a few hundred exact colours with thousands of blended ones — which
 * costs readability on precisely the numbers you zoomed in to check, AND
 * makes the file bigger. Measured: the same capture went 50 KB → 230 KB as
 * PNG after a downscale to 1600px.
 *
 * NO JPEG. It is built for photographic gradients, and on flat backgrounds
 * with thin coloured lines it rings around every edge. Measured: 246–330 KB,
 * five times the lossless PNG, while visibly degrading the text.
 *
 * What is left is PNG and WebP, both lossless-capable. The canvas API does
 * not expose WebP's lossless mode — `toBlob('image/webp', 1)` is
 * quality-100 LOSSY in Chromium, which for this content measured 109 KB
 * against PNG's 50 KB, so it is both bigger and worse. Rather than encode
 * that assumption, this asks: both are produced, the WebP is decoded back and
 * compared pixel-for-pixel against the source, and it wins only if it is
 * genuinely identical AND smaller. If the browser ever gains a real lossless
 * path, this starts using it with no change here.
 */

/** What the compressor produced, and what it cost. */
export interface CompressedChart {
  blob: Blob;
  /** 'image/png' or 'image/webp' — both accepted by the API. */
  mime: string;
  width: number;
  height: number;
  /** Size of the file the user actually supplied. */
  originalBytes: number;
  /** Size being uploaded. */
  encodedBytes: number;
  /**
   * True when the uploaded bytes decode back to the source pixels exactly.
   * PNG always sets this; WebP only when it verified identical. Surfaced so
   * the UI can be honest rather than promising fidelity it did not check.
   */
  lossless: boolean;
}

/** Anything the API will accept as a source image. */
export const ACCEPTED_INPUT = 'image/png,image/jpeg,image/webp,image/gif,image/bmp';

/**
 * Largest source we will decode.
 *
 * Not a limit on the upload — the encoded result is far smaller — but on how
 * much the tab will allocate. A canvas holds 4 bytes per pixel uncompressed,
 * so a 60 MP screenshot would want 240 MB of RAM before a single byte is
 * written.
 */
const MAX_SOURCE_PIXELS = 40_000_000;

async function decode(file: Blob): Promise<ImageBitmap> {
  try {
    return await createImageBitmap(file);
  } catch {
    throw new Error('That file could not be read as an image.');
  }
}

function toBlob(canvas: HTMLCanvasElement, mime: string, quality?: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, mime, quality));
}

/** Raw RGBA of a blob, for comparing an encode against its source. */
async function pixelsOf(source: Blob | ImageBitmap, width: number, height: number) {
  const bitmap = source instanceof Blob ? await decode(source) : source;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) throw new Error('This browser would not provide a 2D canvas context.');
  ctx.drawImage(bitmap, 0, 0);
  if (source instanceof Blob) bitmap.close();
  return ctx.getImageData(0, 0, width, height).data;
}

function identical(a: Uint8ClampedArray, b: Uint8ClampedArray): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

/**
 * Encode `file` for upload, preferring the smallest encoding that is still a
 * pixel-exact copy of what was handed in.
 */
export async function compressChartImage(file: Blob): Promise<CompressedChart> {
  const bitmap = await decode(file);
  const { width, height } = bitmap;

  if (width * height > MAX_SOURCE_PIXELS) {
    bitmap.close();
    throw new Error(
      `That image is ${(width * height / 1e6).toFixed(0)} megapixels, which is too large to process in the browser.`
    );
  }

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) {
    bitmap.close();
    throw new Error('This browser would not provide a 2D canvas context.');
  }
  // No smoothing and no scaling: this is a 1:1 blit, so the source pixels
  // survive into the encoder untouched.
  ctx.drawImage(bitmap, 0, 0);
  const sourcePixels = ctx.getImageData(0, 0, width, height).data;
  bitmap.close();

  const png = await toBlob(canvas, 'image/png');
  if (!png) throw new Error('The browser could not encode the image.');

  let best: Blob = png;
  let mime = 'image/png';
  let lossless = true;

  // WebP is only worth taking if it is smaller AND provably identical.
  // Chromium's canvas encoder is lossy even at quality 1, so this normally
  // declines it — but the check is what makes that a measurement rather than
  // an assumption baked into the code.
  const webp = await toBlob(canvas, 'image/webp', 1);
  if (webp && webp.size < png.size) {
    try {
      const roundTripped = await pixelsOf(webp, width, height);
      if (identical(sourcePixels, roundTripped)) {
        best = webp;
        mime = 'image/webp';
      }
    } catch {
      // Could not decode it back, so it cannot be verified. Keep the PNG.
    }
  }

  return {
    blob: best,
    mime,
    width,
    height,
    originalBytes: file.size,
    encodedBytes: best.size,
    lossless,
  };
}

/** Human-readable byte size, for the upload affordance. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * Pull an image out of a paste event, if there is one.
 *
 * Pasting is the path that matters: the screenshot is already on the
 * clipboard from the charting platform, and requiring a save-then-pick step
 * for every plan is most of the friction in attaching one at all.
 */
export function imageFromPaste(event: ClipboardEvent): File | null {
  const items = event.clipboardData?.items;
  if (!items) return null;
  for (const item of Array.from(items)) {
    if (item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) return file;
    }
  }
  return null;
}
