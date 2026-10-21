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
  error?: string
  /** Venera 数据目录（用户设置优先，缺省为系统默认位置）。 */
  baseDir?: string
  /** baseDir 的来源：setting 表示用户在界面里设置过。 */
  baseDirSource?: 'setting' | 'default'
  dbPath?: string
}

export interface VeneraImportDetail {
  comic_id: string
  book_id?: string
  status: 'success' | 'failed'
  chapters?: number
  pages?: number
  error?: string
}

export interface VeneraImportResponse {
  imported: number
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

export interface LocalConfirmResponse {
  status: string
  imported_books: Array<{
    book_id: string
    title: string
    chapters_count: number
    pages_count: number
    status: string
  }>
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
