import { apiClient } from '@/api/client'
import { createUuid } from '@/utils/uuid'
import type { components } from '@/api/generated/v2'
import type { TextStyleSettings } from '@/types/settings'

export type V2BookDetail = components['schemas']['BookDetail']
export type V2ContainerImportAccepted = components['schemas']['JobBatchAccepted']
export type V2PageList = components['schemas']['PageList']
export type V2PageSummary = components['schemas']['PageSummary']

const API_ROOT = '/api/v2'

export function newIdempotencyKey(): string {
  return createUuid()
}

export async function listChapterPages(
  chapterId: string,
  options: { all?: boolean; cursor?: number; limit?: number; signal?: AbortSignal } = {},
): Promise<V2PageList> {
  const query = new URLSearchParams()
  if (options.all) query.set('all', '1')
  if (options.cursor !== undefined) query.set('cursor', String(options.cursor))
  if (options.limit !== undefined) query.set('limit', String(options.limit))
  const suffix = query.size > 0 ? `?${query}` : ''
  return apiClient.get<V2PageList>(
    `${API_ROOT}/chapters/${encodeURIComponent(chapterId)}/pages${suffix}`,
    { signal: options.signal },
  )
}

export async function getPageSummary(
  pageId: string,
  signal?: AbortSignal,
): Promise<V2PageSummary> {
  return apiClient.get<V2PageSummary>(
    `${API_ROOT}/pages/${encodeURIComponent(pageId)}`,
    { signal },
  )
}

export async function getBook(bookId: string, signal?: AbortSignal): Promise<V2BookDetail> {
  return apiClient.get<V2BookDetail>(
    `${API_ROOT}/books/${encodeURIComponent(bookId)}`,
    { signal },
  )
}

export async function createContainerImportJob(
  chapterId: string,
  file: File,
  textStyle: TextStyleSettings,
): Promise<V2ContainerImportAccepted> {
  const body = new FormData()
  body.append('file', file, file.name)
  body.append('textStyle', JSON.stringify(textStyle))
  return apiClient.upload<V2ContainerImportAccepted>(
    `${API_ROOT}/chapters/${encodeURIComponent(chapterId)}/container-import-jobs`,
    body,
    { headers: { 'Idempotency-Key': newIdempotencyKey() } },
  )
}

export async function deletePage(pageId: string): Promise<void> {
  await apiClient.delete(`${API_ROOT}/pages/${encodeURIComponent(pageId)}`)
}

export async function clearChapterPages(chapterId: string): Promise<number> {
  const result = await apiClient.delete<{ deletedCount: number }>(
    `${API_ROOT}/chapters/${encodeURIComponent(chapterId)}/pages`,
  )
  return result.deletedCount
}
