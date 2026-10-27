import { apiClient } from '@/api/client'

export interface VeneraComic {
  id: string
  title: string
  subtitle: string
  tags: string[]
  directory: string
  cover: string
  coverPath: string
  hasCover?: boolean
  comicType: number
  chapterCount: number
  downloadedChapters: string[]
  createdAt: number
}

export interface VeneraLibraryResponse {
  comics: VeneraComic[]
  total: number
  veneraFound: boolean
  /** Venera 数据目录（用户设置优先，缺省为系统默认位置）。 */
  baseDir?: string
  /** baseDir 的来源：setting 表示用户在界面里设置过。 */
  baseDirSource?: 'setting' | 'default'
  dbPath?: string
}

/** 一条具体的导入问题：能定位到章、页或封面，用来替代过去被吞掉的失败。 */
export interface ImportIssue {
  scope: 'book' | 'chapter' | 'page' | 'cover'
  code: string
  reason: string
  chapter?: { ordinal: number; title: string } | null
  page?: number | null
}

export interface ImportIssueSummary {
  pagesFailed: number
  chaptersFailed: number
  bookFaults: number
  coverFailed: number
}

export interface ImportedChapterRef {
  ordinal: number
  title: string
}

export interface VeneraImportDetail {
  comic_id: string
  book_id?: string | null
  title?: string
  /** partial 表示部分章节或页面被跳过，详见 issues。 */
  status: 'success' | 'partial' | 'failed'
  chapters?: number
  chaptersWritten?: number
  pages?: number
  pagesWritten?: number
  /** 命中同一本已有书并续传，而不是新建一本。 */
  resumed?: boolean
  skippedChapters?: ImportedChapterRef[]
  issues?: ImportIssue[]
  summary?: ImportIssueSummary
  /** 导入后立刻做的完整性核对；`ok` 为 null 表示核对本身没能跑起来（见 error）。 */
  integrity?: IntegrityReport | null
}

export interface VeneraImportResponse {
  imported: number
  partial: number
  failed: number
  details: VeneraImportDetail[]
}

export async function getVeneraLibrary(): Promise<VeneraLibraryResponse> {
  return apiClient.get<VeneraLibraryResponse>('/api/v2/import/venera/library')
}

export function getVeneraCoverUrl(comicId: string): string {
  return `/api/v2/import/venera/cover?comicId=${encodeURIComponent(comicId)}`
}

export async function importVeneraComics(comicIds: string[]): Promise<VeneraImportResponse> {
  return apiClient.post<VeneraImportResponse>('/api/v2/import/venera', {
    comic_ids: comicIds,
  })
}

export interface LocalChapterPreview {
  temp_id: string
  title: string
  ordinal: number | null
  source_type: 'folder' | 'archive'
  source_path: string
  page_count: number
  recognition_layer: 'filename' | 'metadata' | 'ai_vision' | 'fallback'
  confidence: 'high' | 'medium' | 'low'
  needs_review: boolean
  needs_ordinal?: boolean
}

export interface LocalBookPreview {
  temp_id: string
  title: string
  root_path: string
  cover_preview_path?: string | null
  total_chapters: number
  needs_manual_review: boolean
  chapters: LocalChapterPreview[]
}

export interface LocalPreviewResponse {
  status: string
  ai_vision_needed_count: number
  books: LocalBookPreview[]
}

export interface LocalChapterConfirmPayload {
  title: string
  ordinal: number
  source_type: 'folder' | 'archive'
  source_path: string
}

export interface LocalBookConfirmPayload {
  title: string
  cover_source_path?: string | null
  chapters: LocalChapterConfirmPayload[]
}

export interface LocalImportedBook {
  book_id: string | null
  title: string
  status: 'success' | 'partial' | 'failed'
  chapters?: number
  chaptersWritten?: number
  pages?: number
  pagesWritten?: number
  resumed?: boolean
  skippedChapters?: ImportedChapterRef[]
  issues?: ImportIssue[]
  summary?: ImportIssueSummary
  /** 导入后立刻做的完整性核对；`ok` 为 null 表示核对本身没能跑起来（见 error）。 */
  integrity?: AggregateIntegrityReport | null
}

export interface LocalConfirmResponse {
  status: 'success' | 'partial' | 'failed'
  imported: number
  partial: number
  failed: number
  imported_books: LocalImportedBook[]
}

export async function previewLocalManga(paths: string[], enableAiVision = false): Promise<LocalPreviewResponse> {
  return apiClient.post<LocalPreviewResponse>('/api/v2/import/local/preview', {
    paths,
    enable_ai_vision: enableAiVision,
  })
}

export async function confirmLocalImport(books: LocalBookConfirmPayload[]): Promise<LocalConfirmResponse> {
  return apiClient.post<LocalConfirmResponse>('/api/v2/import/local/confirm', {
    books,
  })
}

/** 核对失败时后端只降级：`ok` 为 null，并给出 error。 */
export type AggregateIntegrityReport = Omit<Partial<IntegrityReport>, 'ok'> & {
  ok: boolean | null
  error?: string
}

export interface IntegrityChapterVerdict {
  ordinal: number
  label: string
  images: number | null
  status:
    | 'missing_in_database'
    | 'empty_in_database'
    | 'missing_pages'
    | 'pages_from_elsewhere'
    | 'fewer_pages_than_disk'
    | 'unreadable'
    | 'not_downloaded'
  detail: string
}

export interface IntegrityReport {
  source: 'venera' | 'local'
  target: string
  bookId: string | null
  filesystemChapters: number
  importedChapters: number
  lost: IntegrityChapterVerdict[]
  incomplete: IntegrityChapterVerdict[]
  notDownloaded: IntegrityChapterVerdict[]
  problems: IntegrityChapterVerdict[]
  notes: string[]
  ok: boolean
}

/**
 * 核对「磁盘上实际存在的章节是否都进了库」。以文件系统为准；
 * Venera 登记但磁盘没有的章节会落在 notDownloaded，不算丢件。
 */
export async function checkImportIntegrity(payload: {
  source: 'venera' | 'local'
  bookId: string
  comicId?: string
  paths?: string[]
}): Promise<IntegrityReport> {
  return apiClient.post<IntegrityReport>('/api/v2/import/integrity-check', payload)
}
