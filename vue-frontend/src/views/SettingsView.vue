<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount } from 'vue'
import AppShell from '@/components/ui/AppShell.vue'
import MiyakoNavbar from '@/components/common/MiyakoNavbar.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiField from '@/components/ui/UiField.vue'
import UiInput from '@/components/ui/UiInput.vue'
import UiSelect from '@/components/ui/UiSelect.vue'
import UiCheckbox from '@/components/ui/UiCheckbox.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import ProductStatusBanner from '@/components/product/ProductStatusBanner.vue'
import { useInsightStore } from '@/stores/insightStore'
import { useSettingsAutoSave } from '@/composables/useSettingsAutoSave'
import { isRevisionConflictError, REVISION_CONFLICT_MESSAGE } from '@/utils/settingsConflict'
import { parseSettingsValidationIssue, type SettingsTabId, type SettingsValidationIssue } from '@/utils/settingsValidation'
import { SETTINGS_TABS as settingsTabs, filterSettingsTabs } from './settingsTabs'
import {
  DEFAULT_BATCH_CONFIG,
  defaultEmbeddingConfig,
  defaultLlmConfig,
  defaultRerankerConfig,
  defaultVlmConfig,
} from '@/components/insight/settings/settingsDefaults'
import {
  buildImportTransaction,
  buildSettingsExport,
  parseSettingsExport,
  settingsExportFilename,
} from '@/utils/settingsTransfer'
import { getV2Settings, saveV2SettingsTransaction } from '@/api/v2/settings'
import { triggerBlobDownload } from '@/utils/browserDownload'
import * as insightApi from '@/api/insight'

import ChannelOverviewTab from '@/components/insight/settings/ChannelOverviewTab.vue'
import VlmSettingsTab from '@/components/insight/settings/VlmSettingsTab.vue'
import LlmSettingsTab from '@/components/insight/settings/LlmSettingsTab.vue'
import EmbeddingSettingsTab from '@/components/insight/settings/EmbeddingSettingsTab.vue'
import RerankerSettingsTab from '@/components/insight/settings/RerankerSettingsTab.vue'
import BatchSettingsTab from '@/components/insight/settings/BatchSettingsTab.vue'
import PromptsSettingsTab from '@/components/insight/settings/PromptsSettingsTab.vue'

const insightStore = useInsightStore()



const activeTab = ref<SettingsTabId>('channels')
const settingsQuery = ref('')
const normalizedQuery = computed(() => settingsQuery.value.trim().toLowerCase())
const visibleTabs = computed(() => filterSettingsTabs(settingsTabs, settingsQuery.value))
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
const validationIssue = ref<SettingsValidationIssue | null>(null)
const isExportingSettings = ref(false)
const isImportingSettings = ref(false)

const DEFAULT_EXTRA_ENTITIES = ['characters', 'abilities', 'items', 'locations', 'concepts']

const RESTORE_HINTS: Partial<Record<SettingsTabId, string>> = {
  vlm: '把图片边长、批内并发、重试与请求参数恢复成推荐值，密钥与供应商保持不变。',
  llm: '把请求参数与重试恢复成推荐值，密钥与供应商保持不变。',
  embedding: '把 RPM、重试与超时恢复成推荐值，密钥与供应商保持不变。',
  reranker: '把重试与超时恢复成推荐值，密钥与供应商保持不变。',
  analysis: '把分层架构、每批页数、上文批次数、实体类别与 ID 规范恢复成标准配置。',
}

const restoreHint = computed(() => RESTORE_HINTS[activeTab.value] ?? '')
const canRestoreDefaults = computed(() => Boolean(restoreHint.value))

function restoreDefaults(): void {
  const tab = activeTab.value
  if (tab === 'vlm') {
    insightStore.updateVlmConfig(defaultVlmConfig(insightStore.config.vlm.provider))
  } else if (tab === 'llm') {
    insightStore.updateLlmConfig(defaultLlmConfig(insightStore.config.llm.provider))
  } else if (tab === 'embedding') {
    insightStore.updateEmbeddingConfig(defaultEmbeddingConfig(insightStore.config.embedding.provider))
  } else if (tab === 'reranker') {
    insightStore.updateRerankerConfig(defaultRerankerConfig(insightStore.config.reranker.provider))
  } else if (tab === 'analysis') {
    insightStore.updateBatchConfig({ ...DEFAULT_BATCH_CONFIG, customLayers: [] })
    knowledgeMode.value = true
    slugMode.value = 'pinyin'
    extraEntities.value = [...DEFAULT_EXTRA_ENTITIES]
  } else {
    return
  }
  showMessage(`已恢复「${tabLabel(tab)}」的默认值，稍后自动保存`, 'success')
}

function tabLabel(tab: SettingsTabId): string {
  return settingsTabs.find(item => item.id === tab)?.label ?? '设置'
}

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
    validationIssue.value = null
    showMessage(REVISION_CONFLICT_MESSAGE, 'error')
    return
  }
  const rawMessage = error instanceof Error ? error.message : '网络错误'
  const issue = parseSettingsValidationIssue(rawMessage)
  if (issue) {
    // 校验错误能定位到具体字段：跳到该页签并在顶部说明，避免用户逐个页签试。
    validationIssue.value = issue
    activeTab.value = issue.tab
  } else {
    validationIssue.value = null
  }
  showMessage('保存失败: ' + rawMessage, 'error')
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
    validationIssue.value = null
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

async function exportSettings(): Promise<void> {
  if (isExportingSettings.value) return
  isExportingSettings.value = true
  try {
    const document = await getV2Settings()
    const now = new Date()
    const backup = buildSettingsExport(document, now.toISOString())
    triggerBlobDownload(
      new Blob([JSON.stringify(backup, null, 2)], { type: 'application/json' }),
      settingsExportFilename(now),
    )
    showMessage('已导出设置备份（不含密钥）', 'success')
  } catch (error) {
    showMessage('导出设置失败: ' + (error instanceof Error ? error.message : '网络错误'), 'error')
  } finally {
    isExportingSettings.value = false
  }
}

async function importSettingsFile(file: File): Promise<void> {
  isImportingSettings.value = true
  try {
    const backup = parseSettingsExport(await file.text())
    const current = await getV2Settings()
    const plan = buildImportTransaction(backup, current)
    if (!plan.settingCount && !plan.providerCount) {
      showMessage('备份里没有可导入的配置项', 'error')
      return
    }
    await saveV2SettingsTransaction({
      settings: plan.transaction.settings,
      providerSettings: plan.transaction.providerSettings,
      credentialEdits: [],
    })
    const skipped = plan.skippedDomains.length ? `，跳过 ${plan.skippedDomains.length} 项` : ''
    showMessage(
      `已导入 ${plan.settingCount} 个设置域、${plan.providerCount} 个供应商配置${skipped}；密钥需要重新填写`,
      'success',
    )
    await reloadSettings()
  } catch (error) {
    showMessage('导入设置失败: ' + (error instanceof Error ? error.message : '网络错误'), 'error')
  } finally {
    isImportingSettings.value = false
  }
}

function pickSettingsBackup(): void {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = 'application/json,.json'
  input.addEventListener('change', () => {
    const file = input.files?.[0]
    if (file) void importSettingsFile(file)
  })
  input.click()
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
          </div>

          <div class="settings-search">
            <UiInput
              v-model="settingsQuery"
              type="search"
              clearable
              aria-label="搜索设置项"
              placeholder="搜索设置项（模型、并发、密钥…）"
            />
          </div>

          <nav class="settings-nav" aria-label="设置分类">
            <UiButton
              v-for="tab in visibleTabs"
              :key="tab.id"
              variant="tab"
              class="settings-nav__item"
              :class="{ 'settings-nav__item--active': activeTab === tab.id }"
              @click="activeTab = tab.id"
            >
              <UiIcon class="settings-nav__icon" :name="tab.icon" :size="18" />
              <div class="settings-nav__text">
                <span class="settings-nav__label">{{ tab.label }}</span>
              </div>
            </UiButton>
          </nav>

          <div v-if="normalizedQuery && !visibleTabs.length" class="settings-search-empty">
            <p>没有匹配「{{ settingsQuery }}」的设置项</p>
            <UiButton variant="ghost" size="sm" @click="settingsQuery = ''">清除搜索</UiButton>
          </div>

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
            <div class="settings-transfer-row">
              <UiButton variant="ghost" size="sm" :loading="isExportingSettings" @click="exportSettings">
                导出设置
              </UiButton>
              <UiButton variant="ghost" size="sm" :loading="isImportingSettings" @click="pickSettingsBackup">
                导入设置
              </UiButton>
            </div>
            <p class="settings-transfer-hint">备份不含密钥，导入后需重新填写 API Key。</p>
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
            <ProductStatusBanner
              v-if="validationIssue"
              class="settings-validation-banner"
              tone="danger"
              role="alert"
              :title="`${tabLabel(validationIssue.tab)} · ${validationIssue.fieldLabel}`"
            >
              <p class="settings-validation-banner__detail">{{ validationIssue.detail }}</p>
              <p class="settings-validation-banner__hint">已切换到该页签，修改后会自动重新保存。</p>
            </ProductStatusBanner>

            <div v-if="canRestoreDefaults" class="settings-restore-row">
              <p class="settings-restore-row__hint">{{ restoreHint }}</p>
              <UiButton variant="secondary" size="sm" @click="restoreDefaults">恢复该页签默认值</UiButton>
            </div>

            <!-- 0. 模型通道总览 -->
            <div v-show="activeTab === 'channels'" class="settings-tab-pane">
              <ChannelOverviewTab
                @show-message="showMessage"
                @goto="activeTab = $event"
              />
            </div>

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
                        <span class="category-card__check">
                          <UiIcon :name="extraEntities.includes(cat.key) ? 'check' : 'plus'" :size="14" />
                        </span>
                      </div>
                      <span class="category-card__desc">{{ cat.desc }}</span>
                    </div>
                  </div>
                </div>

                <!-- 分批分析参数 -->
                <div class="settings-param-block">
                  <h4 class="settings-subheading">分层架构与批次设定</h4>
                  <ProductStatusBanner
                    class="settings-cost-note"
                    tone="info"
                    title="性能与成本怎么估"
                  >
                    <ul class="settings-cost-note__list">
                      <li>页分析请求数 = 总页数 ÷ 每批页数；每批再按「批内并发数」切成多个分片同时发出，墙钟约等于单请求时延 × 批数 ÷ 并发数。</li>
                      <li>实测参考：387 页、每批 5 页、并发 3 → 页分析约 19 分钟；实体对齐按图批量裁决，1507 页整本 269 个请求、约 1 小时 52 分；向量索引 387 页约 48 秒。</li>
                      <li>成本 ≈（页面图 + 提示词）输入 token × 单价 + 输出 token × 单价；开启思考模式时推理 token 计入输出，容易吃光预算，视觉分析建议关闭。</li>
                      <li>并发不是越大越快：供应商限流会返回 429，实际吞吐由「最慢的那个分片」决定。</li>
                    </ul>
                  </ProductStatusBanner>

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

@media (--breakpoint-lg-down) {
  .settings-main {
    padding: 16px;
  }

  .settings-container {
    grid-template-columns: 1fr;
    gap: 16px;
  }

  .settings-sidebar {
    position: static;
  }
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

.settings-validation-banner {
  --product-status-banner-margin-inline: 0;

  margin-bottom: 14px;
}

.settings-validation-banner__detail,
.settings-validation-banner__hint {
  margin: 0;
  font-size: 0.82rem;
}

.settings-validation-banner__hint {
  margin-top: 4px;
  color: var(--color-text-muted);
}

.settings-search {
  margin-bottom: 10px;
}

.settings-search-empty {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
  margin-bottom: 10px;
  color: var(--color-text-muted);
  font-size: 0.8rem;
}

.settings-search-empty p {
  margin: 0;
}

.settings-restore-row {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface-quiet);
}

.settings-restore-row__hint {
  margin: 0;
  color: var(--color-text-muted);
  font-size: 0.8rem;
}

.settings-transfer-row {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.settings-transfer-hint {
  margin: 6px 0 0;
  color: var(--color-text-muted);
  font-size: 0.74rem;
  line-height: 1.4;
}

.settings-cost-note {
  --product-status-banner-margin-inline: 0;

  margin-bottom: 16px;
}

.settings-cost-note__list {
  margin: 0;
  padding-left: 18px;
  color: var(--color-text-muted);
  font-size: 0.8rem;
  line-height: 1.6;
}
</style>
