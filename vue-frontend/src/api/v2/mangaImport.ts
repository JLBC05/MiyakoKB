import { apiClient } from '@/api/client'

export interface VeneraComic {
  id: string
  title: string
  subtitle: string
  tags: string[]
  directory: string
  cover: string
  coverPath: string
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

export async function importVeneraComics(comicIds: string[]): Promise<VeneraImportResponse> {
  return apiClient.post<VeneraImportResponse>('/api/v2/import/venera', {
    comic_ids: comicIds,
  })
}
