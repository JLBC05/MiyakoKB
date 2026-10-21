<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount, watch } from 'vue'
import AppShell from '@/components/ui/AppShell.vue'
import MiyakoNavbar from '@/components/common/MiyakoNavbar.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiField from '@/components/ui/UiField.vue'
import UiInput from '@/components/ui/UiInput.vue'
import UiSelect from '@/components/ui/UiSelect.vue'
import UiCheckbox from '@/components/ui/UiCheckbox.vue'
import ProductActionRow from '@/components/product/ProductActionRow.vue'
import ProductStatusBanner from '@/components/product/ProductStatusBanner.vue'
import { useInsightStore } from '@/stores/insightStore'
import { useSettingsAutoSave } from '@/composables/useSettingsAutoSave'
import { isRevisionConflictError, REVISION_CONFLICT_MESSAGE } from '@/utils/settingsConflict'
import * as insightApi from '@/api/insight'

import VlmSettingsTab from '@/components/insight/settings/VlmSettingsTab.vue'
import LlmSettingsTab from '@/components/insight/settings/LlmSettingsTab.vue'
import EmbeddingSettingsTab from '@/components/insight/settings/EmbeddingSettingsTab.vue'
import RerankerSettingsTab from '@/components/insight/settings/RerankerSettingsTab.vue'
import BatchSettingsTab from '@/components/insight/settings/BatchSettingsTab.vue'
import PromptsSettingsTab from '@/components/insight/settings/PromptsSettingsTab.vue'

const insightStore = useInsightStore()

type SettingsTabId = 'vlm' | 'llm' | 'embedding' | 'reranker' | 'analysis' | 'prompts'

const settingsTabs = [
  { id: 'vlm', label: 'VLM 视觉模型', icon: '🖼️', desc: '用于分镜画面识别与实体初筛' },
  { id: 'llm', label: 'Chat 对话/裁决', icon: '💬', desc: '用于实体语义裁决、剧情问答与概览生成' },
  { id: 'embedding', label: 'Embedding 向量', icon: '🔢', desc: '用于画面与分镜语义检索、多模态 RAG 索引' },
  { id: 'reranker', label: 'Reranker 重排序', icon: '🔄', desc: '用于二次检索精排，提高问答检索准度' },
  { id: 'analysis', label: '知识库分析参数', icon: '⚙️', desc: '配置 SLUG_MODE（拼音/罗马音）、实体类别与批次' },
  { id: 'prompts', label: '提示词配置', icon: '📝', desc: '分镜分析、段落汇总、全书总结提示词' },
] satisfies Array<{ id: SettingsTabId; label: string; icon: string; desc: string }>

const activeTab = ref<SettingsTabId>('vlm')
const isLoadingConfig = ref(true)
const backendConfigReady = ref(false)
const statusMessage = ref('')
const statusType = ref<'success' | 'error' | ''>('')
const messageTone = computed(() => (statusType.value === 'error' ? 'danger' : 'success'))

let statusTimer: ReturnType<typeof setTimeout> | null = null
let savedConfig: ReturnType<typeof insightStore.getConfigForApi> | null = null
let isMounted = true

// 知识库分析参数本地响应式状态
const knowledgeMode = ref(true)
const slugMode = ref<'pinyin' | 'romanized'>('pinyin')
const extraEntities = ref<string[]>([
  'characters',
  'abilities',
  'items',
  'locations',
  'concepts',
])

const slugOptions = [
  { value: 'pinyin', label: '拼音标识 (Pinyin) - 格式: character_hu_zhang_you_ren' },
  { value: 'romanized', label: '标准罗马音 (Romanized) - 格式: character_itadori_yuji' },
]

const availableEntityCategories = [
  { key: 'characters', label: '角色 (Characters)', desc: '登场人物、声优、身份变体' },
  { key: 'abilities', label: '能力/招式 (Abilities)', desc: '术式、超能力、金融策略' },
  { key: 'items', label: '物品/道具 (Items)', desc: '核心道具、书籍、专属物件' },
  { key: 'locations', label: '地点/场景 (Locations)', desc: '学校、走廊、特定建筑与房间' },
  { key: 'concepts', label: '概念/设定 (Concepts)', desc: '术语、规则、金额、专有名词' },
]

const needsReload = ref(false)

function showMessage(text: string, type: 'success' | 'error'): void {
  statusMessage.value = text
  statusType.value = type
  if (statusTimer) clearTimeout(statusTimer)
  statusTimer = setTimeout(() => {
    if (!isMounted) return
    statusMessage.value = ''
    statusType.value = ''
  }, 4000)
}

function reportSaveError(error: unknown): void {
  if (isRevisionConflictError(error)) {
    needsReload.value = true
    showMessage(REVISION_CONFLICT_MESSAGE, 'error')
    return
  }
  showMessage('保存失败: ' + (error instanceof Error ? error.message : '网络错误'), 'error')
}

const { isSaving, hasUnsavedChanges } = useSettingsAutoSave({
  source: () => {
    const base = insightStore.getConfigForApi() as any
    // 注入当前知识库扩展参数，兼顾 base.config.batch 与 base.analysis?.batch / base.batch
    const targetBatch = base.config?.batch ?? base.analysis?.batch ?? base.batch ?? (base.batch = {})
    targetBatch.knowledgeMode = knowledgeMode.value
    targetBatch.slugMode = slugMode.value
    targetBatch.extraEntities = extraEntities.value
    return base
  },
  ready: () => backendConfigReady.value,
  save: async () => {
    if (!savedConfig) throw new Error('请先加载后端设置')
    const current = insightStore.getConfigForApi() as any
    const targetBatch = current.config?.batch ?? current.analysis?.batch ?? current.batch ?? (current.batch = {})
    targetBatch.knowledgeMode = knowledgeMode.value
    targetBatch.slugMode = slugMode.value
    targetBatch.extraEntities = extraEntities.value

    savedConfig = await insightApi.saveGlobalConfig(current, savedConfig)
    needsReload.value = false
    showMessage('设置已自动保存并同步到后端', 'success')
  },
  onError: reportSaveError,
})

function applyLoadedConfig(remote: any): void {
  savedConfig = remote
  if (typeof insightStore.setConfigFromApi === 'function') {
    insightStore.setConfigFromApi(remote)
  } else if (typeof (insightStore as any).loadConfigFromApi === 'function') {
    (insightStore as any).loadConfigFromApi(remote)
  }

  // 读取后端扩展参数，支持 remote.config.batch、remote.analysis?.batch 或 remote.batch
  const batchConfig = remote.config?.batch ?? remote.analysis?.batch ?? remote.batch
  if (batchConfig?.slugMode) {
    slugMode.value = batchConfig.slugMode === 'romanized' ? 'romanized' : 'pinyin'
  }
  if (typeof batchConfig?.knowledgeMode === 'boolean') {
    knowledgeMode.value = batchConfig.knowledgeMode
  }
  if (Array.isArray(batchConfig?.extraEntities)) {
    extraEntities.value = batchConfig.extraEntities
  }

  backendConfigReady.value = true
}

async function reloadSettings(): Promise<void> {
  isLoadingConfig.value = true
  backendConfigReady.value = false
  try {
    const remote = await insightApi.getGlobalConfig() as any
    if (!isMounted) return
    applyLoadedConfig(remote)
    needsReload.value = false
    showMessage('已重新加载后端设置', 'success')
  } catch (error) {
    if (!isMounted) return
    showMessage('加载后端设置失败: ' + (error instanceof Error ? error.message : '网络错误'), 'error')
  } finally {
    if (isMounted) isLoadingConfig.value = false
  }
}

onMounted(async () => {
  isMounted = true
  isLoadingConfig.value = true
  try {
    const remote = await insightApi.getGlobalConfig() as any
    if (!isMounted) return
    applyLoadedConfig(remote)
  } catch (error) {
    if (!isMounted) return
    showMessage('加载后端设置失败: ' + (error instanceof Error ? error.message : '网络错误'), 'error')
  } finally {
    if (isMounted) isLoadingConfig.value = false
  }
})

onBeforeUnmount(() => {
  isMounted = false
  if (statusTimer) clearTimeout(statusTimer)
})

function toggleEntityCategory(key: string): void {
  const idx = extraEntities.value.indexOf(key)
  if (idx >= 0) {
    if (extraEntities.value.length <= 1) {
      showMessage('至少需保留一个实体提取类别', 'error')
      return
    }
    extraEntities.value.splice(idx, 1)
  } else {
    extraEntities.value.push(key)
  }
}

async function handleManualSave(): Promise<void> {
  // 手动保存时强制提交：跳过 sameSettingValue 比较，确保新填的 API Key 一定写入后端
  try {
    if (!savedConfig) throw new Error('请先加载后端设置')
    const current = insightStore.getConfigForApi() as any
    const targetBatch = current.config?.batch ?? current.analysis?.batch ?? current.batch ?? (current.batch = {})
    targetBatch.knowledgeMode = knowledgeMode.value
    targetBatch.slugMode = slugMode.value
    targetBatch.extraEntities = extraEntities.value
    savedConfig = await insightApi.saveGlobalConfig(current, savedConfig, { force: true })
    needsReload.value = false
    showMessage('设置保存成功', 'success')
  } catch (err) {
    reportSaveError(err)
  }
}
</script>

<template>
  <AppShell class="settings-page" viewport-mode="page">
    <MiyakoNavbar />

    <main class="settings-main">
      <div class="settings-container">
        <!-- 侧栏导航 -->
        <aside class="settings-sidebar">
          <div class="settings-sidebar__header">
            <h2 class="settings-sidebar__title">系统设置</h2>
            <p class="settings-sidebar__subtitle">MiyakoKB 核心分析与模型参数</p>
          </div>

          <nav class="settings-nav" aria-label="设置分类">
            <UiButton
              v-for="tab in settingsTabs"
              :key="tab.id"
              variant="tab"
              class="settings-nav__item"
              :class="{ 'settings-nav__item--active': activeTab === tab.id }"
              @click="activeTab = tab.id"
            >
              <span class="settings-nav__icon">{{ tab.icon }}</span>
              <div class="settings-nav__text">
                <span class="settings-nav__label">{{ tab.label }}</span>
                <span class="settings-nav__desc">{{ tab.desc }}</span>
              </div>
            </UiButton>
          </nav>

          <div class="settings-sidebar__footer">
            <UiButton
              variant="primary"
              class="settings-save-btn"
              :loading="isSaving"
              @click="handleManualSave"
            >
              {{ isSaving ? '保存中…' : '立即保存全部更改' }}
            </UiButton>
            <p v-if="hasUnsavedChanges && !isSaving" class="settings-save-hint">
              有未保存的更改，将自动保存
            </p>
          </div>
        </aside>

        <!-- 主设置区域 -->
        <section class="settings-content">
          <!-- 状态提示 -->
          <ProductStatusBanner
            v-if="statusMessage"
            class="settings-status-banner"
            :tone="messageTone"
            :title="statusType === 'error' ? '提示' : '成功'"
          >
            {{ statusMessage }}
            <template v-if="needsReload" #actions>
              <UiButton variant="secondary" size="sm" @click="reloadSettings">重新加载</UiButton>
            </template>
          </ProductStatusBanner>

          <div v-if="isLoadingConfig" class="settings-loading-card">
            <span>正在读取后端模型与知识库配置…</span>
          </div>

          <div v-else class="settings-panel-card">
            <!-- 1. VLM 视觉模型 -->
            <div v-show="activeTab === 'vlm'" class="settings-tab-pane">
              <VlmSettingsTab
                @update:config="insightStore.updateVlmConfig($event)"
                @show-message="showMessage"
              />
            </div>

            <!-- 2. Chat 对话 / 语义裁决 -->
            <div v-show="activeTab === 'llm'" class="settings-tab-pane">
              <LlmSettingsTab
                @update:config="insightStore.updateLlmConfig($event)"
                @show-message="showMessage"
              />
            </div>

            <!-- 3. Embedding 向量模型 -->
            <div v-show="activeTab === 'embedding'" class="settings-tab-pane">
              <EmbeddingSettingsTab
                @update:config="insightStore.updateEmbeddingConfig($event)"
                @show-message="showMessage"
              />
            </div>

            <!-- 4. Reranker 重排序 -->
            <div v-show="activeTab === 'reranker'" class="settings-tab-pane">
              <RerankerSettingsTab
                @update:config="insightStore.updateRerankerConfig($event)"
                @show-message="showMessage"
              />
            </div>

            <!-- 5. 知识库专属分析参数 -->
            <div v-show="activeTab === 'analysis'" class="settings-tab-pane">
              <div class="analysis-settings-panel">
                <h3 class="panel-section-title">知识图谱与实体对齐参数</h3>
                <p class="panel-section-desc">控制实体提取时使用的 ID 生成规范、全自动语义归并及多模态分析流水线。</p>

                <!-- SLUG MODE -->
                <div class="settings-param-block">
                  <UiField
                    variant="settings"
                    label="实体 Slug 命名规范 (SLUG_MODE)"
                    hint="设置知识库写入 analysis_runs 与实体对齐产物时生成的唯一 ID 规范。"
                  >
                    <UiSelect
                      v-model="slugMode"
                      :options="slugOptions"
                    />
                  </UiField>
                </div>

                <!-- 知识模式开关 -->
                <div class="settings-param-block">
                  <UiField
                    variant="settings"
                    label="知识库模式 (knowledgeMode)"
                    hint="开启后启用角色、能力、物品、地点、概念全维度五分类知识抽取。"
                  >
                    <UiCheckbox
                      v-model="knowledgeMode"
                      label="启用知识模式深度图谱抽取"
                    />
                  </UiField>
                </div>

                <!-- 实体类别多选 -->
                <div class="settings-param-block">
                  <label class="settings-subheading">启用的实体提取类别 (extraEntities)</label>
                  <div class="entity-categories-grid">
                    <div
                      v-for="cat in availableEntityCategories"
                      :key="cat.key"
                      class="category-card"
                      :class="{ 'category-card--active': extraEntities.includes(cat.key) }"
                      @click="toggleEntityCategory(cat.key)"
                    >
                      <div class="category-card__header">
                        <span class="category-card__title">{{ cat.label }}</span>
                        <span class="category-card__check">{{ extraEntities.includes(cat.key) ? '✓' : '+' }}</span>
                      </div>
                      <span class="category-card__desc">{{ cat.desc }}</span>
                    </div>
                  </div>
                </div>

                <!-- 分批分析参数 -->
                <div class="settings-param-block">
                  <h4 class="settings-subheading">分层架构与批次设定</h4>
                  <BatchSettingsTab
                    @update:config="insightStore.updateBatchConfig($event)"
                  />
                </div>
              </div>
            </div>

            <!-- 6. 提示词配置 -->
            <div v-show="activeTab === 'prompts'" class="settings-tab-pane">
              <PromptsSettingsTab
                @update:prompts="insightStore.updatePrompts($event)"
                @show-message="showMessage"
              />
            </div>
          </div>
        </section>
      </div>
    </main>
  </AppShell>
</template>

<style scoped>
.settings-page {
  --settings-shadow-panel: 0 1px 3px rgba(0, 0, 0, 0.03);
  --settings-shadow-active: 0 1px 3px rgba(0, 0, 0, 0.04);
  --settings-tint-active: rgba(153, 119, 204, 0.08);

  background-color: var(--color-bg-base);
  color: var(--color-text-primary);
  display: flex;
  flex-direction: column;
}

.settings-main {
  flex: 1;
  max-width: 1680px;
  width: 100%;
  margin: 0 auto;
  padding: 24px;
}

.settings-container {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 24px;
  align-items: start;
}

.settings-sidebar {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 20px;
  box-shadow: var(--settings-shadow-panel);
  position: sticky;
  top: 78px;
}

.settings-sidebar__header {
  margin-bottom: 20px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--color-border);
}

.settings-sidebar__title {
  font-size: 1.25rem;
  font-weight: 700;
  color: var(--color-text-primary);
  margin-bottom: 4px;
}

.settings-sidebar__subtitle {
  font-size: 0.82rem;
  color: var(--color-text-secondary);
}

.settings-nav {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.settings-nav__item {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 12px 14px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: transparent;
  text-align: left;
  cursor: pointer;
  transition: all 0.15s ease;
  width: 100%;
}

.settings-nav__item:hover {
  background-color: var(--color-surface-secondary);
}

.settings-nav__item--active {
  background-color: var(--color-surface-secondary);
  border-color: var(--color-border);
  box-shadow: var(--settings-shadow-active);
}

.settings-nav__icon {
  font-size: 1.25rem;
  line-height: 1.2;
}

.settings-nav__text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.settings-nav__label {
  font-size: 0.92rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

.settings-nav__item--active .settings-nav__label {
  color: var(--color-brand-primary);
}

.settings-nav__desc {
  font-size: 0.78rem;
  color: var(--color-text-secondary);
  line-height: 1.35;
}

.settings-sidebar__footer {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--color-border);
}

.settings-save-btn {
  width: 100%;
  justify-content: center;
}

.settings-save-hint {
  margin: 6px 0 0;
  color: var(--color-text-muted);
  font-size: 0.78rem;
  text-align: center;
}

.settings-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.settings-status-banner {
  margin-bottom: 8px;
}

.settings-loading-card {
  padding: 40px;
  text-align: center;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  color: var(--color-text-secondary);
}

.settings-panel-card {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 28px;
  box-shadow: var(--settings-shadow-panel);
}

.settings-tab-pane {
  min-height: 500px;
}

.panel-section-title {
  font-size: 1.2rem;
  font-weight: 700;
  margin-bottom: 6px;
  color: var(--color-text-primary);
}

.panel-section-desc {
  font-size: 0.88rem;
  color: var(--color-text-secondary);
  margin-bottom: 24px;
}

.settings-param-block {
  margin-bottom: 28px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--color-border);
}

.settings-subheading {
  display: block;
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin-bottom: 12px;
}

.entity-categories-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
  margin-top: 8px;
}

.category-card {
  padding: 12px 14px;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background-color: var(--color-surface-secondary);
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.category-card:hover {
  border-color: var(--color-brand-primary);
}

.category-card--active {
  background-color: var(--settings-tint-active);
  border-color: var(--color-brand-primary);
}

.category-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.category-card__title {
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--color-text-primary);
}

.category-card--active .category-card__title {
  color: var(--color-brand-primary);
}

.category-card__check {
  font-size: 0.9rem;
  font-weight: 700;
  color: var(--color-brand-primary);
}

.category-card__desc {
  font-size: 0.78rem;
  color: var(--color-text-secondary);
}
</style>
