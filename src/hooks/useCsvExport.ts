'use client';

import { useMutation } from '@tanstack/react-query';

import { downloadExport } from '@/lib/api';
import { saveBlob } from '@/lib/download';
import type { CsvExport, ExportDataset } from '@/types/api';

/**
 * Download one dataset as CSV.
 *
 * A mutation rather than a query, despite being a read. A query would fetch on
 * mount, which would start four downloads the moment Settings opens; and there
 * is nothing to cache, because an export is a snapshot someone asked for by
 * name -- serving a remembered copy is the one thing it must not do. The API
 * says the same with `Cache-Control: no-store`.
 *
 * Nothing is invalidated on success: the server read and wrote nothing, so no
 * other query's data went stale.
 *
 * `variables` carries the dataset while the request is in flight, which is what
 * lets four buttons share one mutation and only the clicked one show a spinner.
 */
export function useExportCsv() {
  return useMutation<CsvExport, Error, ExportDataset>({
    mutationFn: downloadExport,
    onSuccess: ({ blob, filename }) => saveBlob(blob, filename),
  });
}
