/**
 * 设置保存失败时的错误定位。
 *
 * 后端校验错误带点分路径（`provider_settings.insight_vlm.imageMaxSize must be an integer
 * from 64 to 4096`、`insight.analysis.batch has unknown keys: …`），前端只弹一句全局提示会让
 * 用户不知道该去哪个页签改哪个字段。这里把路径拆成「页签 + 字段中文名 + 原文」，让设置页能
 * 直接跳到出错页签并在顶部就地说明。
 */

export type SettingsTabId =
  | 'channels'
  | 'vlm'
  | 'llm'
  | 'embedding'
  | 'reranker'
  | 'analysis'
  | 'prompts'

export type SettingsValidationIssue = {
  tab: SettingsTabId
  fieldLabel: string
  detail: string
}

/** 后端域前缀 → 设置页签。按长度倒序匹配，避免 `insight` 抢先吃掉 `insight_vlm`。 */
const DOMAIN_TABS: ReadonlyArray<readonly [string, SettingsTabId]> = [
  ['provider_settings.insight_vlm', 'vlm'],
  ['provider_settings.insight_chat', 'llm'],
  ['provider_settings.insight_embedding', 'embedding'],
  ['provider_settings.insight_reranker', 'reranker'],
  ['insight.analysis', 'analysis'],
  ['insight', 'analysis'],
]

/** 载荷字段 → 界面上看到的字段名。 */
const FIELD_LABELS: Record<string, string> = {
  imageMaxSize: '最大图片边长',
  analyzeConcurrency: '批内并发数',
  modelName: '模型名称',
  customBaseUrl: 'Base URL',
  openaiOptions: '请求参数',
  temperature: '温度',
  force_json_output: '强制 JSON 输出',
  use_stream: '流式输出',
  extra_body: '附加请求体',
  rpm_limit: 'RPM 限制',
  transport_retries: '传输重试次数',
  business_retries: '业务重试次数',
  timeoutSeconds: '超时时间',
  pagesPerBatch: '每批页数',
  contextBatchCount: '上文参考批次数',
  architecturePreset: '分层架构',
  customLayers: '自定义层级',
  unitsPerGroup: '每组合并数',
  knowledgeMode: '知识库模式',
  slugMode: '实体 ID 规范',
  extraEntities: '实体类别',
  provider: '供应商',
  useSameAsVlm: '复用 VLM 设置',
}

const UNKNOWN_FIELD_LABEL = '设置项'

const FIELD_TOKEN_RE = /[A-Za-z_][A-Za-z0-9_]*/g

function resolveTab(message: string): SettingsTabId | null {
  for (const [prefix, tab] of DOMAIN_TABS) {
    if (message.startsWith(prefix)) return tab
  }
  if (message.includes('provider_settings.insight_vlm')) return 'vlm'
  if (message.includes('provider_settings.insight_chat')) return 'llm'
  if (message.includes('provider_settings.insight_embedding')) return 'embedding'
  if (message.includes('provider_settings.insight_reranker')) return 'reranker'
  if (message.includes('insight')) return 'analysis'
  return null
}

function resolveFieldLabel(message: string): string {
  // 收集消息里的标识符，取最后一个已知字段名（路径末段通常就是出错字段）。
  const tokens: string[] = []
  for (const match of message.matchAll(FIELD_TOKEN_RE)) {
    tokens.push(match[0])
  }
  for (let index = tokens.length - 1; index >= 0; index -= 1) {
    const label = FIELD_LABELS[tokens[index]]
    if (label) return label
  }
  return UNKNOWN_FIELD_LABEL
}

/**
 * 把后端的校验错误翻成「去哪个页签、改哪个字段」。
 * 非校验类错误（网络、修订冲突等）返回 null，交给原有提示路径。
 */
export function parseSettingsValidationIssue(message: unknown): SettingsValidationIssue | null {
  if (typeof message !== 'string') return null
  const text = message.trim()
  if (!text) return null
  if (text.includes('已被其他页面')) return null
  const tab = resolveTab(text)
  if (!tab) return null
  return {
    tab,
    fieldLabel: resolveFieldLabel(text),
    detail: text,
  }
}
