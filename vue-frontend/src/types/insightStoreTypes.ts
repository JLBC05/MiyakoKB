import type { OpenAICompatibleOptions } from './settings'
import type { components } from '@/api/generated/v2'

export type AnalysisStatus = 'idle' | components['schemas']['JobStatus']

export type OverviewTemplateType =
  | 'no_spoiler'
  | 'story_summary'
  | 'recap'
  | 'character_guide'
  | 'world_setting'
  | 'highlights'
  | 'reading_notes'

/** 后端已把精确/全库问答合并为单一通用模式，前端只使用 generic。 */
export type QAMode = 'generic'

export interface ChapterInfo {
  id: string
  title: string
  pageRange?: { start: number; end: number }
  startPage: number
  endPage: number
  analyzed: boolean
  analyzedCount?: number
  summary?: string
}

export interface QAMatchedEntity {
  entity_id?: string
  canonical_name: string
  entity_type?: string
  mention_count?: number
  matched_mention?: string
}

export interface QAResolvedEntities {
  matched_entities: QAMatchedEntity[]
  is_structured_query?: boolean
}

export interface QACitation {
  /** 给人看的页码：默认是章内页码，元数据缺失时才回退全书页码（见 pageNumberSource）。 */
  page: number
  pageId?: string
  /** page 的语义来源：chapter = 章内页码，run_global = 本 run 范围内的全书页码。 */
  pageNumberSource?: 'chapter' | 'run_global'
  /** 章内第几页；元数据缺失时为 null。 */
  pageInChapter?: number | null
  /** 全书（本 run 范围）页码，与模型看到的页码一致。 */
  globalPageNumber?: number | null
  chapterOrdinal?: number
  chapterTitle?: string
  sourceUrl?: string
  excerpt?: string
  source?: 'page' | 'entity'
}

export interface QAMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  isLoading?: boolean
  mode?: QAMode
  citations?: QACitation[]
  saved?: boolean
  resolvedEntities?: QAResolvedEntities
  warnings?: string[]
}

export interface BatchConfig {
  pagesPerBatch: number
  contextBatchCount: number
  architecturePreset: string
  customLayers: Array<{ name: string; units: number; align: boolean }>
  knowledgeMode?: boolean
  extraEntities?: string[]
  slugMode?: 'pinyin' | 'romanized'
}

export type StoreOpenAICompatibleOptions = OpenAICompatibleOptions

export interface StoreVlmConfig {
  provider: string
  apiKey: string
  model: string
  baseUrl: string
  openaiOptions: StoreOpenAICompatibleOptions
  imageMaxSize: number
  analyzeConcurrency: number
}

export interface StoreLlmConfig {
  useSameAsVlm: boolean
  provider: string
  apiKey: string
  model: string
  baseUrl: string
  openaiOptions: StoreOpenAICompatibleOptions
}

export interface StoreEmbeddingConfig {
  provider: string
  apiKey: string
  model: string
  baseUrl: string
  rpmLimit: number
  transportRetries: number
  businessRetries: number
  timeoutSeconds: number
}

export interface StoreRerankerConfig {
  provider: string
  apiKey: string
  model: string
  baseUrl: string
  transportRetries: number
  businessRetries: number
  timeoutSeconds: number
}

export interface StoreImageGenConfig {
  provider: string
  apiKey: string
  model: string
  baseUrl: string
  transportRetries: number
  businessRetries: number
  timeoutSeconds: number
}

export interface StoreAnalysisProgress {
  current: number
  total: number
  status: AnalysisStatus
  message?: string
}

export interface StoreInsightConfig {
  vlm: StoreVlmConfig
  llm: StoreLlmConfig
  embedding: StoreEmbeddingConfig
  reranker: StoreRerankerConfig
  imageGen: StoreImageGenConfig
  batch: BatchConfig
  prompts: Record<string, string>
}

export type InsightVlmProviderDraft = Omit<StoreVlmConfig, 'provider'>
export type InsightLlmProviderDraft = Omit<StoreLlmConfig, 'provider' | 'useSameAsVlm'>
export type InsightEmbeddingProviderDraft = Omit<StoreEmbeddingConfig, 'provider'>
export type InsightRerankerProviderDraft = Omit<StoreRerankerConfig, 'provider'>
export type InsightImageGenProviderDraft = Omit<StoreImageGenConfig, 'provider'>

export interface InsightProviderDrafts {
  vlm: Record<string, InsightVlmProviderDraft>
  llm: Record<string, InsightLlmProviderDraft>
  embedding: Record<string, InsightEmbeddingProviderDraft>
  reranker: Record<string, InsightRerankerProviderDraft>
  imageGen: Record<string, InsightImageGenProviderDraft>
}

export interface InsightSettingsSnapshot {
  config: StoreInsightConfig
  providerDrafts: InsightProviderDrafts
}
