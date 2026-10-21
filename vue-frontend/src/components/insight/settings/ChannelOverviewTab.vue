<script setup lang="ts">
import { computed, ref } from 'vue'

import UiButton from '@/components/ui/UiButton.vue'
import * as insightApi from '@/api/insight'
import { getProviderDisplayName } from '@/config/aiProviders'
import { useInsightStore } from '@/stores/insightStore'
import { useSettingsStore } from '@/stores/settings'
import InsightSettingsPanel from './InsightSettingsPanel.vue'

type ChannelKey = 'vlm' | 'chat' | 'embedding' | 'reranker'
type SettingsTabId = 'vlm' | 'llm' | 'embedding' | 'reranker'

type ChannelDefinition = {
  key: ChannelKey
  label: string
  domain: string
  tab: SettingsTabId
  purpose: string
}

type ChannelRow = ChannelDefinition & {
  providerLabel: string
  model: string
  baseUrl: string
  keyState: string
  keyMissing: boolean
}

const CHANNELS: readonly ChannelDefinition[] = [
  { key: 'vlm', label: 'VLM 视觉模型', domain: 'insight_vlm', tab: 'vlm', purpose: '页面分析、角色蒸馏取样' },
  { key: 'chat', label: 'Chat 对话/裁决', domain: 'insight_chat', tab: 'llm', purpose: '实体裁决、概览、问答生成' },
  { key: 'embedding', label: 'Embedding 向量', domain: 'insight_embedding', tab: 'embedding', purpose: '语义索引与页面召回' },
  { key: 'reranker', label: 'Reranker 重排序', domain: 'insight_reranker', tab: 'reranker', purpose: '检索结果精排' },
]

const emit = defineEmits<{
  (e: 'showMessage', message: string, type: 'success' | 'error'): void
  (e: 'goto', tab: SettingsTabId): void
}>()

const insightStore = useInsightStore()
const settingsStore = useSettingsStore()

const testingKey = ref<ChannelKey | null>(null)

function channelConfig(key: ChannelKey) {
  if (key === 'vlm') return insightStore.config.vlm
  if (key === 'chat') return insightStore.config.llm
  if (key === 'embedding') return insightStore.config.embedding
  return insightStore.config.reranker
}

function storedKeyState(domain: string, provider: string): { label: string; missing: boolean } {
  const summary = settingsStore.credentialSummaries.find(
    item => item.domain === domain && item.provider === provider,
  )
  if (!summary) return { label: '未填写', missing: true }
  if (summary.hasKey) return { label: '已保存', missing: false }
  return { label: '未填写', missing: true }
}

const rows = computed<ChannelRow[]>(() => CHANNELS.map((channel) => {
  const config = channelConfig(channel.key)
  const typed = config.apiKey.trim()
  const stored = storedKeyState(channel.domain, config.provider)
  return {
    ...channel,
    providerLabel: getProviderDisplayName(config.provider) || config.provider,
    model: config.model || '未指定',
    baseUrl: config.baseUrl || '',
    keyState: typed ? '本次已填写（未保存）' : stored.label,
    keyMissing: !typed && stored.missing,
  }
}))

const missingCount = computed(() => rows.value.filter(row => row.keyMissing).length)

async function runTest(row: ChannelRow): Promise<void> {
  if (testingKey.value) return
  testingKey.value = row.key
  const config = channelConfig(row.key)
  const payload = {
    provider: config.provider,
    api_key: config.apiKey,
    model: config.model,
    base_url: config.baseUrl,
  }
  try {
    const result = row.key === 'chat'
      ? await insightApi.testLlmConnection({ ...payload, use_same_as_vlm: insightStore.config.llm.useSameAsVlm })
      : row.key === 'vlm'
        ? await insightApi.testVlmConnection(payload)
        : row.key === 'embedding'
          ? await insightApi.testEmbeddingConnection(payload)
          : await insightApi.testRerankerConnection(payload)
    if (result.success) {
      emit('showMessage', `${row.label} 连接成功`, 'success')
    } else {
      emit('showMessage', `${row.label} 连接失败：${result.message || '未返回原因'}`, 'error')
    }
  } catch (error) {
    emit('showMessage', `${row.label} 连接失败：${error instanceof Error ? error.message : '网络错误'}`, 'error')
  } finally {
    testingKey.value = null
  }
}

function goto(row: ChannelRow): void {
  emit('goto', row.tab)
}
</script>

<template>
  <InsightSettingsPanel
    class="channel-overview-tab"
    description="四个模型通道共用一个保存动作：这里只看清各自指向哪里、密钥是否就绪，改参数请点对应行的「去配置」。"
  >
    <p v-if="missingCount > 0" class="channel-overview__hint">
      有 {{ missingCount }} 个通道还没有可用的 API Key，构建知识库时会直接失败。
    </p>

    <div class="channel-overview" role="table" aria-label="模型通道总览">
      <div class="channel-overview__row channel-overview__row--head" role="row">
        <span role="columnheader">通道</span>
        <span role="columnheader">供应商</span>
        <span role="columnheader">模型</span>
        <span role="columnheader">Base URL</span>
        <span role="columnheader">API Key</span>
        <span role="columnheader">操作</span>
      </div>

      <div v-for="row in rows" :key="row.key" class="channel-overview__row" role="row">
        <span class="channel-overview__name" role="cell">
          <strong>{{ row.label }}</strong>
          <small>{{ row.purpose }}</small>
        </span>
        <span role="cell">{{ row.providerLabel }}</span>
        <span class="channel-overview__model" role="cell">{{ row.model }}</span>
        <span class="channel-overview__base" role="cell">{{ row.baseUrl || '默认' }}</span>
        <span
          class="channel-overview__key"
          :class="{ 'channel-overview__key--missing': row.keyMissing }"
          role="cell"
        >{{ row.keyState }}</span>
        <span class="channel-overview__actions" role="cell">
          <UiButton
            variant="secondary"
            size="sm"
            :loading="testingKey === row.key"
            :disabled="testingKey !== null"
            @click="runTest(row)"
          >
            测试
          </UiButton>
          <UiButton variant="ghost" size="sm" @click="goto(row)">去配置</UiButton>
        </span>
      </div>
    </div>
  </InsightSettingsPanel>
</template>

<style scoped>
.channel-overview-tab {
  --channel-overview-grid-columns: minmax(180px, 1.4fr) minmax(110px, 1fr) minmax(140px, 1.2fr) minmax(160px, 1.4fr) minmax(120px, 1fr) minmax(150px, auto);
}

.channel-overview__hint {
  margin: 0 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-status-warning);
  border-radius: 8px;
  background: var(--color-status-warning-surface-soft);
  color: var(--color-text-default);
  font-size: 0.86rem;
}

.channel-overview {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  overflow: hidden;
}

.channel-overview__row {
  display: grid;
  grid-template-columns: var(--channel-overview-grid-columns);
  gap: 12px;
  align-items: center;
  padding: 10px 14px;
  border-top: 1px solid var(--color-border);
  color: var(--color-text-default);
  font-size: 0.86rem;
}

.channel-overview__row:first-child {
  border-top: none;
}

.channel-overview__row--head {
  background: var(--color-surface-quiet);
  color: var(--color-text-muted);
  font-size: 0.78rem;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}

.channel-overview__name {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.channel-overview__name small {
  color: var(--color-text-muted);
  font-size: 0.76rem;
}

.channel-overview__model,
.channel-overview__base {
  overflow-wrap: anywhere;
  font-family: var(--font-family-mono, monospace);
  font-size: 0.8rem;
}

.channel-overview__key {
  color: var(--color-status-success);
}

.channel-overview__key--missing {
  color: var(--color-status-warning);
}

.channel-overview__actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
}

@media (--breakpoint-lg-down) {
  .channel-overview__row {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  }

  .channel-overview__row--head {
    display: none;
  }
}
</style>
