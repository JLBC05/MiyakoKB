import type { SettingsTabId } from '@/utils/settingsValidation'

export type SettingsTab = {
  id: SettingsTabId
  label: string
  icon: string
  desc: string
  keywords: string[]
}

/**
 * 设置页的页签清单与搜索关键词。
 *
 * 关键词按「用户会怎么找」写（视觉/并发/密钥/slug…），而不是照抄字段名，
 * 因为参数名是英文 camelCase，用户通常先想到中文概念。
 */
export const SETTINGS_TABS: SettingsTab[] = [
  {
    id: 'channels',
    label: '模型通道总览',
    icon: '🧩',
    desc: '四条模型通道的供应商、模型与密钥状态一览',
    keywords: ['通道', '总览', '密钥', 'api key', '供应商', 'provider', '测试连接'],
  },
  {
    id: 'vlm',
    label: 'VLM 视觉模型',
    icon: '🖼️',
    desc: '用于分镜画面识别与实体初筛',
    keywords: ['视觉', '图片', '并发', '批内并发', '最大边长', 'rpm', '模型', 'base url', '密钥'],
  },
  {
    id: 'llm',
    label: 'Chat 对话/裁决',
    icon: '💬',
    desc: '用于实体语义裁决、剧情问答与概览生成',
    keywords: ['对话', '裁决', '问答', '复用 vlm', '模型', '温度', 'json', '流式', '密钥'],
  },
  {
    id: 'embedding',
    label: 'Embedding 向量',
    icon: '🔢',
    desc: '用于画面与分镜语义检索、多模态 RAG 索引',
    keywords: ['向量', '嵌入', '检索', 'rpm', '重试', '超时', '模型'],
  },
  {
    id: 'reranker',
    label: 'Reranker 重排序',
    icon: '🔄',
    desc: '用于二次检索精排，提高问答检索准度',
    keywords: ['重排', '精排', '重试', '超时', '模型'],
  },
  {
    id: 'analysis',
    label: '知识库分析参数',
    icon: '⚙️',
    desc: '配置 SLUG_MODE（拼音/罗马音）、实体类别与批次',
    keywords: ['slug', '拼音', '罗马音', '实体', '类别', '批次', '每批页数', '上文', '分层', '架构'],
  },
  {
    id: 'prompts',
    label: '提示词配置',
    icon: '📝',
    desc: '分镜分析、段落汇总、全书总结提示词',
    keywords: ['提示词', 'prompt', '分镜', '段落', '全书'],
  },
]

/** 空查询返回全部页签；否则按标签、描述与关键词做不区分大小写的包含匹配。 */
export function filterSettingsTabs(
  tabs: readonly SettingsTab[],
  query: string,
): SettingsTab[] {
  const normalized = query.trim().toLowerCase()
  if (!normalized) return [...tabs]
  return tabs.filter(tab => [tab.label, tab.desc, ...tab.keywords]
    .join('\n')
    .toLowerCase()
    .includes(normalized))
}
