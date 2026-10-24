import { apiClient } from '@/api/client'
import type { components } from '@/api/generated/v2'
import { newIdempotencyKey } from './content'

const ROOT = '/api/v2/insight'

export type V2AcceptedJob = components['schemas']['JobBatchAccepted']
export type V2InsightAnalysisJobAccepted = components['schemas']['InsightAnalysisJobAccepted']
export type V2InsightArtifact = components['schemas']['InsightArtifact']
export type V2InsightBootstrap = components['schemas']['InsightBootstrap']
export type V2InsightNote = components['schemas']['InsightNote']
export type V2InsightPageDetail = components['schemas']['InsightPageDetail']
export type V2InsightPageSummary = components['schemas']['InsightPageSummary']
export type V2InsightQaStatus = components['schemas']['InsightQaStatus']
export type V2InsightOverviewTemplateList = components['schemas']['InsightOverviewTemplateList']
export type V2InsightRecentPageAnalysisList = components['schemas']['InsightRecentPageAnalysisList']

type V2InsightChapterList = components['schemas']['InsightChapterList']
type V2InsightNoteCreateCommand = components['schemas']['InsightNoteCreateCommand']
type V2InsightNoteList = components['schemas']['InsightNoteList']
type V2InsightNoteUpdateCommand = components['schemas']['InsightNoteUpdateCommand']
type V2InsightPageList = components['schemas']['InsightPageList']
type V2InsightTimeline = components['schemas']['InsightTimeline']

export function getInsightBootstrap(): Promise<V2InsightBootstrap> {
  return apiClient.get(`${ROOT}/bootstrap`)
}

export function listInsightChapters(bookId: string): Promise<V2InsightChapterList> {
  return apiClient.get(`${ROOT}/books/${encodeURIComponent(bookId)}/chapters`)
}

export function listInsightPages(
  bookId: string,
  options: { chapterId?: string; cursor?: number; limit?: number } = {}
): Promise<V2InsightPageList> {
  return apiClient.get(`${ROOT}/books/${encodeURIComponent(bookId)}/pages`, {
    params: {
      cursor: options.cursor ?? 0,
      limit: options.limit ?? 50,
      ...(options.chapterId ? { chapterId: options.chapterId } : {}),
    },
  })
}

export function listRecentInsightPageAnalyses(
  bookId: string,
  limit = 5,
): Promise<V2InsightRecentPageAnalysisList> {
  return apiClient.get(
    `${ROOT}/books/${encodeURIComponent(bookId)}/recent-page-analyses`,
    { params: { limit } },
  )
}

export function getInsightPage(
  pageId: string,
  options: { runId?: string } = {}
): Promise<V2InsightPageDetail> {
  const url = `${ROOT}/pages/${encodeURIComponent(pageId)}`
  return options.runId
    ? apiClient.get(url, { params: { runId: options.runId } })
    : apiClient.get(url)
}

export function createInsightAnalysisJob(command: {
  bookId: string
  chapterIds?: string[]
  pageIds?: string[]
  scope: 'chapter' | 'full' | 'incremental' | 'page'
}): Promise<V2InsightAnalysisJobAccepted> {
  return apiClient.post(`${ROOT}/analysis-jobs`, command, {
    headers: { 'Idempotency-Key': newIdempotencyKey() },
  })
}

export function getInsightOverview(bookId: string, template: string): Promise<V2InsightArtifact> {
  return apiClient.get(`${ROOT}/artifacts/overviews/${encodeURIComponent(template)}`, {
    params: { bookId },
  })
}

export function listInsightOverviewTemplates(bookId: string): Promise<V2InsightOverviewTemplateList> {
  return apiClient.get(`${ROOT}/artifacts/overviews`, {
    params: { bookId },
  })
}

export function rebuildInsightOverview(bookId: string, template: string): Promise<V2AcceptedJob> {
  return apiClient.post(
    `${ROOT}/artifacts/overviews/${encodeURIComponent(template)}`,
    { bookId },
    { headers: { 'Idempotency-Key': newIdempotencyKey() } }
  )
}

export function getInsightTimeline(
  bookId: string,
  options: {
    eventCursor?: number
    eventLimit?: number
    characterCursor?: string
    characterLimit?: number
  } = {}
): Promise<V2InsightTimeline> {
  return apiClient.get(`${ROOT}/timeline`, {
    params: {
      bookId,
      eventCursor: options.eventCursor ?? 0,
      eventLimit: options.eventLimit ?? 50,
      characterLimit: options.characterLimit ?? 50,
      ...(options.characterCursor ? { characterCursor: options.characterCursor } : {}),
    },
  })
}

export function rebuildInsightTimeline(bookId: string): Promise<V2AcceptedJob> {
  return apiClient.post(
    `${ROOT}/timeline`,
    { bookId },
    { headers: { 'Idempotency-Key': newIdempotencyKey() } }
  )
}

export function getInsightQaStatus(
  bookId: string,
  mode: 'generic' | 'exact' | 'global' = 'generic'
): Promise<V2InsightQaStatus> {
  return apiClient.get(`${ROOT}/qa/status`, { params: { bookId, mode } })
}

export function rebuildInsightCompressedContext(bookId: string): Promise<V2AcceptedJob> {
  return apiClient.post(
    `${ROOT}/books/${encodeURIComponent(bookId)}/compressed-context/rebuild`,
    {},
    { headers: { 'Idempotency-Key': newIdempotencyKey() } }
  )
}

export function rebuildInsightVectors(bookId: string): Promise<V2AcceptedJob> {
  return apiClient.post(
    `${ROOT}/books/${encodeURIComponent(bookId)}/vector-rebuild`,
    {},
    { headers: { 'Idempotency-Key': newIdempotencyKey() } }
  )
}

export function rebuildInsightEntityAlignment(bookId: string): Promise<V2AcceptedJob> {
  return apiClient.post(
    `${ROOT}/books/${encodeURIComponent(bookId)}/entity-alignment/rebuild`,
    {},
    { headers: { 'Idempotency-Key': newIdempotencyKey() } }
  )
}

export function listInsightNotes(
  bookId: string,
  options: { cursor?: string; kind?: 'qa' | 'text'; limit?: number } = {}
): Promise<V2InsightNoteList> {
  return apiClient.get(`${ROOT}/notes`, {
    params: {
      bookId,
      limit: options.limit ?? 50,
      ...(options.cursor ? { cursor: options.cursor } : {}),
      ...(options.kind ? { kind: options.kind } : {}),
    },
  })
}

export function getInsightNote(noteId: string): Promise<V2InsightNote> {
  return apiClient.get(`${ROOT}/notes/${encodeURIComponent(noteId)}`)
}

export function createInsightNote(command: V2InsightNoteCreateCommand): Promise<V2InsightNote> {
  return apiClient.post(`${ROOT}/notes`, command, {
    headers: { 'Idempotency-Key': newIdempotencyKey() },
  })
}

export function updateInsightNote(
  noteId: string,
  command: V2InsightNoteUpdateCommand
): Promise<V2InsightNote> {
  return apiClient.patch(`${ROOT}/notes/${encodeURIComponent(noteId)}`, command, {
    headers: { 'Idempotency-Key': newIdempotencyKey() },
  })
}

export function deleteInsightNote(
  noteId: string,
  baseRevision: number
): Promise<{ deleted: boolean }> {
  return apiClient.delete(
    `${ROOT}/notes/${encodeURIComponent(noteId)}?baseRevision=${baseRevision}`,
    { headers: { 'Idempotency-Key': newIdempotencyKey() } }
  )
}

export function createInsightExport(bookId: string): Promise<V2AcceptedJob> {
  return apiClient.post(
    `${ROOT}/books/${encodeURIComponent(bookId)}/exports`,
    {},
    { headers: { 'Idempotency-Key': newIdempotencyKey() } }
  )
}

export function insightCurrentExportUrl(
  bookId: string,
  template: string,
  format = 'markdown'
): string {
  const query = new URLSearchParams({ template, format })
  return `${ROOT}/books/${encodeURIComponent(bookId)}/export/current?${query}`
}

export function insightPageExportUrl(pageId: string, format = 'markdown'): string {
  return `${ROOT}/pages/${encodeURIComponent(pageId)}/export?format=${encodeURIComponent(format)}`
}

export function insightQaUrl(bookId: string): string {
  return `${ROOT}/books/${encodeURIComponent(bookId)}/qa`
}

export type V2InsightEntity = components['schemas']['InsightEntity']
export type V2InsightGraphNode = components['schemas']['InsightGraphNode']
export type V2InsightGraphEdge = components['schemas']['InsightGraphEdge']
export type V2InsightGraphData = components['schemas']['InsightGraphData']

export function listInsightEntities(
  bookId: string,
  category?: string
): Promise<{ entities: V2InsightEntity[] }> {
  return apiClient.get(`${ROOT}/entities`, {
    params: {
      book_id: bookId,
      ...(category ? { category } : {}),
    },
  })
}

export function getInsightGraph(
  bookId: string,
  options: { category?: string; minWeight?: number } = {}
): Promise<V2InsightGraphData> {
  return apiClient.get(`${ROOT}/graph`, {
    params: {
      book_id: bookId,
      ...(options.category ? { category: options.category } : {}),
      ...(options.minWeight ? { min_weight: options.minWeight } : {}),
    },
  })
}

export interface BookKnowledgeStatus {
  status: 'completed' | 'partial' | 'none'
  totalChapters: number
  analyzedChapters: number
}

export interface BooksKnowledgeStatusResponse {
  items: Record<string, BookKnowledgeStatus>
}

export function getBooksKnowledgeStatus(): Promise<BooksKnowledgeStatusResponse> {
  return apiClient.get(`${ROOT}/books/knowledge-status`)
}

export type V2InsightCharacterSkill = components['schemas']['InsightCharacterSkill']
export type V2InsightCharacterSkillStatus = components['schemas']['InsightCharacterSkillStatus']
export type V2InsightCharacterSkillCharacter = components['schemas']['InsightCharacterSkillCharacter']
export type V2InsightLorebook = components['schemas']['InsightLorebook']
export type V2DistillKind = 'skill' | 'card'

function kindSuffix(kind: V2DistillKind | undefined): string {
  return kind && kind !== 'skill' ? `?kind=${kind}` : ''
}

export function listInsightCharacterSkills(
  bookId: string,
  options: { minPages?: number } = {}
): Promise<{ characters: V2InsightCharacterSkillCharacter[] }> {
  const query = new URLSearchParams()
  if (options.minPages !== undefined) query.set('minPages', String(options.minPages))
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return apiClient.get(
    `${ROOT}/books/${encodeURIComponent(bookId)}/character-skills${suffix}`
  )
}

export function buildInsightCharacterSkill(
  bookId: string,
  command: { entityId: string; maxVisionPages?: number; kind?: V2DistillKind }
): Promise<V2InsightCharacterSkillStatus> {
  return apiClient.post(
    `${ROOT}/books/${encodeURIComponent(bookId)}/character-skills`,
    command,
    { headers: { 'Idempotency-Key': newIdempotencyKey() } }
  )
}

export function getInsightCharacterSkill(
  bookId: string,
  entityId: string,
  options: { kind?: V2DistillKind } = {}
): Promise<V2InsightCharacterSkill> {
  return apiClient.get(
    `${ROOT}/books/${encodeURIComponent(bookId)}/character-skills/${encodeURIComponent(entityId)}${kindSuffix(options.kind)}`
  )
}

export function getInsightCharacterSkillStatus(
  bookId: string,
  entityId: string,
  options: { kind?: V2DistillKind } = {}
): Promise<V2InsightCharacterSkillStatus> {
  return apiClient.get(
    `${ROOT}/books/${encodeURIComponent(bookId)}/character-skills/${encodeURIComponent(entityId)}/status${kindSuffix(options.kind)}`
  )
}

export function characterSkillArchiveUrl(
  bookId: string,
  entityId: string,
  kind?: V2DistillKind
): string {
  return `${ROOT}/books/${encodeURIComponent(bookId)}/character-skills/${encodeURIComponent(entityId)}/archive${kindSuffix(kind)}`
}

/** 整库导出（zip）：实体网络、时间线、概览、逐页分析与已蒸馏角色资料。 */
export function insightKnowledgeArchiveUrl(bookId: string): string {
  return `${ROOT}/books/${encodeURIComponent(bookId)}/knowledge-archive`
}

export function buildInsightLorebook(
  bookId: string,
  command: { maxEntities?: number } = {}
): Promise<V2InsightCharacterSkillStatus> {
  return apiClient.post(`${ROOT}/books/${encodeURIComponent(bookId)}/lorebooks`, command, {
    headers: { 'Idempotency-Key': newIdempotencyKey() },
  })
}

export function getInsightLorebook(bookId: string): Promise<V2InsightLorebook> {
  return apiClient.get(`${ROOT}/books/${encodeURIComponent(bookId)}/lorebooks`)
}

export function getInsightLorebookStatus(bookId: string): Promise<V2InsightCharacterSkillStatus> {
  return apiClient.get(`${ROOT}/books/${encodeURIComponent(bookId)}/lorebooks/status`)
}

export function lorebookArchiveUrl(bookId: string): string {
  return `${ROOT}/books/${encodeURIComponent(bookId)}/lorebooks/archive`
}
