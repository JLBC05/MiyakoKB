import { getProviderBaseUrl, getProviderDefaultModel, normalizeProviderId } from '@/config/aiProviders'
import type {
  BatchConfig,
  StoreEmbeddingConfig,
  StoreLlmConfig,
  StoreRerankerConfig,
  StoreVlmConfig,
} from '@/types/insightStoreTypes'

/**
 * 各设置域的「恢复默认」取值。
 *
 * 默认值只在这里定义一次：页签上的「恢复默认」按钮和新建配置走同一套来源，
 * 避免界面默认值与代码默认值漂移。供应商相关的字段（Base URL / 模型）按当前供应商
 * 从清单取，而不是写死某个厂商。
 */

const DEFAULT_RETRY = 3

export function defaultVlmConfig(provider: string): StoreVlmConfig {
  const normalized = normalizeProviderId(provider)
  return {
    provider,
    apiKey: '',
    model: getProviderDefaultModel(normalized, 'vlm'),
    baseUrl: getProviderBaseUrl(normalized, 'vlm'),
    openaiOptions: {
      request: { forceJsonOutput: true, temperature: 0.3 },
      execution: {
        useStream: false,
        rpmLimit: 0,
        transportRetries: DEFAULT_RETRY,
        businessRetries: DEFAULT_RETRY,
      },
    },
    imageMaxSize: 1024,
    analyzeConcurrency: 3,
  }
}

export function defaultLlmConfig(provider: string): StoreLlmConfig {
  const normalized = normalizeProviderId(provider)
  return {
    useSameAsVlm: false,
    provider,
    apiKey: '',
    model: getProviderDefaultModel(normalized, 'chat'),
    baseUrl: getProviderBaseUrl(normalized, 'chat'),
    openaiOptions: {
      request: { forceJsonOutput: false },
      execution: {
        useStream: true,
        rpmLimit: 0,
        transportRetries: DEFAULT_RETRY,
        businessRetries: DEFAULT_RETRY,
      },
    },
  }
}

export function defaultEmbeddingConfig(provider: string): StoreEmbeddingConfig {
  const normalized = normalizeProviderId(provider)
  return {
    provider,
    apiKey: '',
    model: getProviderDefaultModel(normalized, 'embedding'),
    baseUrl: getProviderBaseUrl(normalized, 'embedding'),
    rpmLimit: 0,
    transportRetries: DEFAULT_RETRY,
    businessRetries: DEFAULT_RETRY,
    timeoutSeconds: 0,
  }
}

export function defaultRerankerConfig(provider: string): StoreRerankerConfig {
  const normalized = normalizeProviderId(provider)
  return {
    provider,
    apiKey: '',
    model: getProviderDefaultModel(normalized, 'reranker'),
    baseUrl: getProviderBaseUrl(normalized, 'rerank'),
    transportRetries: DEFAULT_RETRY,
    businessRetries: DEFAULT_RETRY,
    timeoutSeconds: 0,
  }
}

export const DEFAULT_BATCH_CONFIG: BatchConfig = {
  pagesPerBatch: 5,
  contextBatchCount: 3,
  architecturePreset: 'chapter_based',
  customLayers: [],
}
