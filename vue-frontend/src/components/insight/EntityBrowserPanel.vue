<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useInsightStore } from '@/stores/insightStore'
import {
  listInsightEntities,
  rebuildInsightEntityAlignment,
  type V2InsightEntity,
} from '@/api/v2/insight'
import { confirmProductAction } from '@/composables/useProductConfirm'
import { showToast } from '@/utils/toast'
import UiButton from '@/components/ui/UiButton.vue'
import UiInput from '@/components/ui/UiInput.vue'

const route = useRoute()
const insightStore = useInsightStore()

const entities = ref<V2InsightEntity[]>([])
const isLoading = ref(false)
const errorMessage = ref('')
const selectedCategory = ref<string>('all')
const searchQuery = ref('')
const expandedEntityIds = ref<Set<string>>(new Set())
const isRebuildingAlignment = ref(false)
const rebuildTaskId = ref('')
const rebuildBookId = ref('')
const rebuildProgressLabel = ref('')

const categories = [
  { key: 'all', label: '全部', icon: '🌐' },
  { key: 'characters', label: '角色', icon: '👤' },
  { key: 'abilities', label: '能力', icon: '⚡' },
  { key: 'items', label: '物品', icon: '🎒' },
  { key: 'locations', label: '地点', icon: '📍' },
  { key: 'concepts', label: '概念', icon: '💡' },
]

const categoryCountMap = computed(() => {
  const map: Record<string, number> = { all: entities.value.length }
  for (const cat of categories) {
    if (cat.key !== 'all') {
      map[cat.key] = 0
    }
  }
  for (const ent of entities.value) {
    if (map[ent.category] !== undefined) {
      map[ent.category]++
    }
  }
  return map
})

const normalizedQuery = computed(() => searchQuery.value.trim().toLowerCase())

const filteredEntities = computed(() => {
  const query = normalizedQuery.value
  return entities.value.filter(ent => {
    if (selectedCategory.value !== 'all' && ent.category !== selectedCategory.value) {
      return false
    }
    if (!query) return true
    const haystack = [ent.canonical_name, ...(ent.aliases ?? [])]
      .map(value => String(value ?? '').toLowerCase())
      .join('\n')
    return haystack.includes(query)
  })
})

const groupedEntities = computed(() => {
  if (selectedCategory.value !== 'all') {
    return [
      {
        key: selectedCategory.value,
        label: categories.find(c => c.key === selectedCategory.value)?.label ?? selectedCategory.value,
        icon: categories.find(c => c.key === selectedCategory.value)?.icon ?? '🏷️',
        list: filteredEntities.value,
      },
    ]
  }

  const groups: Array<{ key: string; label: string; icon: string; list: V2InsightEntity[] }> = []
  for (const cat of categories) {
    if (cat.key === 'all') continue
    const list = filteredEntities.value.filter(e => e.category === cat.key)
    if (list.length > 0) {
      groups.push({
        key: cat.key,
        label: cat.label,
        icon: cat.icon,
        list,
      })
    }
  }
  return groups
})

async function fetchEntities() {
  const queryBook = typeof route?.query?.book === 'string' ? route.query.book.trim() : ''
  const bookId = insightStore.currentBookId || queryBook
  if (!bookId) {
    entities.value = []
    return
  }

  isLoading.value = true
  errorMessage.value = ''
  try {
    const res = await listInsightEntities(bookId)
    entities.value = res.entities || []
  } catch (err: any) {
    errorMessage.value = err?.message || '加载实体数据失败'
    entities.value = []
  } finally {
    isLoading.value = false
  }
}

function toggleExpand(id: string) {
  if (expandedEntityIds.value.has(id)) {
    expandedEntityIds.value.delete(id)
  } else {
    expandedEntityIds.value.add(id)
  }
}

function isExpanded(id: string): boolean {
  return expandedEntityIds.value.has(id)
}

function resetRebuildState(): void {
  isRebuildingAlignment.value = false
  rebuildTaskId.value = ''
  rebuildBookId.value = ''
  rebuildProgressLabel.value = ''
}

async function rebuildAlignment(): Promise<void> {
  const queryBook = typeof route?.query?.book === 'string' ? route.query.book.trim() : ''
  const bookId = insightStore.currentBookId || queryBook
  if (!bookId || isRebuildingAlignment.value) return

  isRebuildingAlignment.value = true
  rebuildBookId.value = bookId
  rebuildProgressLabel.value = '等待确认...'
  const confirmed = await confirmProductAction({
    title: '重建实体对齐',
    message:
      '确定要重新对齐实体吗？现有实体、别名与共现关系会保留到新版本构建并发布完成。',
    confirmText: '重建',
    cancelText: '取消',
    tone: 'primary',
  })
  if (!confirmed) {
    if (bookId === (insightStore.currentBookId || queryBook)) resetRebuildState()
    return
  }
  if (bookId !== (insightStore.currentBookId || queryBook) || rebuildTaskId.value) return

  rebuildProgressLabel.value = '准备启动...'
  try {
    const taskId = await rebuildInsightEntityAlignment(bookId)
    rebuildTaskId.value = taskId.jobIds[0]
    rebuildBookId.value = bookId
    rebuildProgressLabel.value = '任务已启动'
    showToast('实体对齐重建任务已启动', 'success')
  } catch (error) {
    const message = error instanceof Error ? error.message : '重建实体对齐失败'
    showToast(message, 'error')
    resetRebuildState()
  }
}

watch(
  () => [insightStore.currentBookId, route?.query?.book],
  () => {
    fetchEntities()
  },
  { immediate: true },
)

onMounted(() => {
  fetchEntities()
})
</script>

<template>
  <div class="entity-browser-panel">
    <!-- Header / Filter Bar -->
    <div class="entity-browser-panel__header">
      <div class="entity-browser-panel__filters">
        <UiButton
          v-for="cat in categories"
          :key="cat.key"
          class="entity-filter-chip"
          :class="{ 'is-active': selectedCategory === cat.key }"
          variant="ghost"
          size="xs"
          @click="selectedCategory = cat.key"
        >
          <span class="entity-filter-chip__icon">{{ cat.icon }}</span>
          <span class="entity-filter-chip__label">{{ cat.label }}</span>
          <span class="entity-filter-chip__count">{{ categoryCountMap[cat.key] ?? 0 }}</span>
        </UiButton>
      </div>

      <div class="entity-browser-panel__actions">
        <UiInput
          v-model="searchQuery"
          class="entity-search-input"
          type="search"
          placeholder="搜索实体或别名"
          aria-label="搜索实体或别名"
          clearable
        />
        <UiButton
          variant="secondary"
          size="sm"
          :loading="isLoading"
          @click="fetchEntities"
        >
          刷新
        </UiButton>
        <UiButton
          variant="secondary"
          size="sm"
          :loading="isRebuildingAlignment"
          :disabled="isRebuildingAlignment"
          @click="rebuildAlignment"
        >
          {{ isRebuildingAlignment ? '重建中…' : '重建实体对齐' }}
        </UiButton>
      </div>
    </div>

    <!-- Rebuild progress -->
    <div v-if="rebuildProgressLabel" class="entity-browser-panel__rebuild-status" aria-live="polite">
      {{ rebuildProgressLabel }}
      <span v-if="rebuildTaskId" class="entity-browser-panel__rebuild-task">任务 {{ rebuildTaskId }}</span>
    </div>

    <!-- Error message -->
    <div v-if="errorMessage" class="entity-browser-panel__error">
      {{ errorMessage }}
    </div>

    <!-- Loading State -->
    <div v-if="isLoading && entities.length === 0" class="entity-browser-panel__loading">
      <div class="entity-browser-panel__spinner"></div>
      <span>正在加载实体图谱...</span>
    </div>

    <!-- No match -->
    <div
      v-else-if="!isLoading && entities.length > 0 && filteredEntities.length === 0"
      class="entity-browser-panel__empty"
    >
      <span class="entity-browser-panel__empty-icon">🔍</span>
      <p class="entity-browser-panel__empty-text">没有匹配「{{ searchQuery }}」的实体</p>
      <p class="entity-browser-panel__empty-subtext">试试更短的关键词，或切换到其它分类。</p>
      <UiButton variant="secondary" size="sm" @click="searchQuery = ''">清除搜索</UiButton>
    </div>

    <!-- Empty State -->
    <div v-else-if="!isLoading && entities.length === 0" class="entity-browser-panel__empty">
      <span class="entity-browser-panel__empty-icon">📭</span>
      <p class="entity-browser-panel__empty-text">当前书籍暂无实体对齐数据</p>
      <p class="entity-browser-panel__empty-subtext">完成漫画洞察分析后将自动生成标准实体库与别名映射。</p>
    </div>

    <!-- Entities Content -->
    <div v-else class="entity-browser-panel__content">
      <div
        v-for="group in groupedEntities"
        :key="group.key"
        class="entity-group-section"
      >
        <div class="entity-group-section__header">
          <span class="entity-group-section__icon">{{ group.icon }}</span>
          <h3 class="entity-group-section__title">{{ group.label }}</h3>
          <span class="entity-group-section__badge">{{ group.list.length }}</span>
        </div>

        <div class="entity-card-grid">
          <div
            v-for="ent in group.list"
            :key="ent.entity_id"
            class="entity-card"
            :class="{ 'is-expanded': isExpanded(ent.entity_id) }"
            @click="toggleExpand(ent.entity_id)"
          >
            <div class="entity-card__header">
              <div class="entity-card__title-row">
                <span class="entity-card__name">{{ ent.canonical_name }}</span>
                <span class="entity-card__frequency-badge" title="全篇出现频次">
                  {{ ent.frequency }} 次
                </span>
              </div>
              <div class="entity-card__id">{{ ent.entity_id }}</div>
            </div>

            <!-- Aliases -->
            <div v-if="ent.aliases && ent.aliases.length > 0" class="entity-card__aliases">
              <span class="entity-card__aliases-label">别名:</span>
              <div class="entity-card__alias-tags">
                <span
                  v-for="(alias, idx) in ent.aliases"
                  :key="idx"
                  class="entity-alias-tag"
                >
                  {{ alias }}
                </span>
              </div>
            </div>
            <div v-else class="entity-card__no-aliases">
              无别名
            </div>

            <!-- Footer / Toggle -->
            <div class="entity-card__footer">
              <span class="entity-card__pages-summary">
                出现在 {{ ent.pages ? ent.pages.length : 0 }} 页
              </span>
              <span class="entity-card__expand-arrow">
                {{ isExpanded(ent.entity_id) ? '▲ 收起' : '▼ 展开详情' }}
              </span>
            </div>

            <!-- Expanded Details: Page list -->
            <div
              v-if="isExpanded(ent.entity_id)"
              class="entity-card__details"
              @click.stop
            >
              <div class="entity-card__detail-row">
                <span class="entity-card__detail-label">出现页码:</span>
                <div class="entity-card__page-chips">
                  <span
                    v-for="p in ent.pages"
                    :key="p"
                    class="entity-page-chip"
                  >
                    第 {{ p }} 页
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.entity-browser-panel {
  --entity-browser-stroke-whisper: rgba(255, 255, 255, 0.08);
  --entity-browser-surface-faint: rgba(255, 255, 255, 0.04);
  --entity-browser-text: #94a3b8;
  --entity-browser-text-blue: #f8fafc;
  --entity-browser-surface: #3b82f6;
  --entity-browser-text-white: #ffffff;
  --entity-browser-surface-soft: rgba(0, 0, 0, 0.2);
  --entity-browser-surface-whisper: rgba(239, 68, 68, 0.1);
  --entity-browser-stroke-light: rgba(239, 68, 68, 0.3);
  --entity-browser-text-red: #ef4444;
  --entity-browser-stroke-whisper-white: rgba(255, 255, 255, 0.1);
  --entity-browser-surface-medium: rgba(30, 41, 59, 0.5);
  --entity-browser-surface-heavy: rgba(30, 41, 59, 0.8);
  --entity-browser-text-medium: rgba(59, 130, 246, 0.5);
  --entity-browser-surface-soft-blue: rgba(59, 130, 246, 0.15);
  --entity-browser-stroke-light-blue: rgba(59, 130, 246, 0.3);
  --entity-browser-text-blue-mid: #60a5fa;
  --entity-browser-text-blue-mid-alt2: #64748b;
  --entity-browser-surface-faint-white: rgba(255, 255, 255, 0.06);
  --entity-browser-text-blue-bright: #cbd5e1;
  --entity-browser-surface-whisper-blue: rgba(59, 130, 246, 0.1);
  --entity-browser-stroke-soft: rgba(59, 130, 246, 0.2);
  --entity-browser-text-blue-bright-alt2: #93c5fd;

  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 1.5rem;
  overflow-y: auto;
  box-sizing: border-box;
}

.entity-browser-panel__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 1rem;
  margin-bottom: 1.5rem;
  padding-bottom: 1rem;
  border-bottom: 1px solid var(--insight-view-sidebar-divider, var(--entity-browser-stroke-whisper));
}

.entity-browser-panel__filters {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.entity-filter-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.375rem 0.75rem;
  background: var(--insight-surface-secondary, var(--entity-browser-surface-faint));
  border: 1px solid var(--insight-view-sidebar-divider, var(--entity-browser-stroke-whisper));
  border-radius: 9999px;
  color: var(--insight-text-secondary, var(--entity-browser-text));
  font-size: 0.875rem;
  cursor: pointer;
  transition: all 0.15s ease;
}

.entity-filter-chip:hover {
  background: var(--insight-surface-tertiary, var(--entity-browser-stroke-whisper));
  color: var(--insight-text-primary, var(--entity-browser-text-blue));
}

.entity-filter-chip.is-active {
  background: var(--color-action-primary-fill, var(--entity-browser-surface));
  border-color: var(--color-action-primary-fill, var(--entity-browser-surface));
  color: var(--entity-browser-text-white);
}

.entity-filter-chip__count {
  font-size: 0.75rem;
  padding: 0.1rem 0.4rem;
  background: var(--entity-browser-surface-soft);
  border-radius: 9999px;
}

.entity-browser-panel__error {
  padding: 0.75rem 1rem;
  background: var(--entity-browser-surface-whisper);
  border: 1px solid var(--entity-browser-stroke-light);
  border-radius: 0.5rem;
  color: var(--entity-browser-text-red);
  margin-bottom: 1rem;
}

.entity-browser-panel__rebuild-status {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
  padding: 0.6rem 1rem;
  background: var(--entity-browser-surface-whisper-blue);
  border: 1px solid var(--entity-browser-stroke-soft);
  border-radius: 0.5rem;
  color: var(--entity-browser-text-blue-bright-alt2);
  margin-bottom: 1rem;
}

.entity-browser-panel__rebuild-task {
  font-family: var(--font-mono);
  font-size: 0.75rem;
  color: var(--entity-browser-text-blue-mid);
}

.entity-browser-panel__loading,
.entity-browser-panel__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 4rem 1rem;
  color: var(--insight-text-secondary, var(--entity-browser-text));
  text-align: center;
}

.entity-browser-panel__spinner {
  width: 2rem;
  height: 2rem;
  border: 3px solid var(--entity-browser-stroke-whisper-white);
  border-top-color: var(--color-action-primary-fill, var(--entity-browser-surface));
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin-bottom: 1rem;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.entity-browser-panel__empty-icon {
  font-size: 3rem;
  margin-bottom: 1rem;
}

.entity-browser-panel__empty-text {
  font-size: 1.125rem;
  font-weight: 500;
  color: var(--insight-text-primary, var(--entity-browser-text-blue));
  margin-bottom: 0.5rem;
}

.entity-browser-panel__empty-subtext {
  font-size: 0.875rem;
  color: var(--insight-text-secondary, var(--entity-browser-text));
}

.entity-group-section {
  margin-bottom: 2rem;
}

.entity-group-section__header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 1rem;
}

.entity-group-section__icon {
  font-size: 1.25rem;
}

.entity-group-section__title {
  margin: 0;
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--insight-text-primary, var(--entity-browser-text-blue));
}

.entity-group-section__badge {
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.125rem 0.5rem;
  background: var(--insight-surface-tertiary, var(--entity-browser-stroke-whisper-white));
  border-radius: 9999px;
  color: var(--insight-text-secondary, var(--entity-browser-text));
}

.entity-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1rem;
}

.entity-card {
  background: var(--insight-surface-secondary, var(--entity-browser-surface-medium));
  border: 1px solid var(--insight-view-sidebar-divider, var(--entity-browser-stroke-whisper));
  border-radius: 0.75rem;
  padding: 1rem;
  cursor: pointer;
  transition: all 0.2s ease;
  display: flex;
  flex-direction: column;
}

.entity-card:hover {
  background: var(--insight-surface-tertiary, var(--entity-browser-surface-heavy));
  border-color: var(--color-action-primary-fill, var(--entity-browser-text-medium));
  transform: translateY(-2px);
  box-shadow: 0 4px 12px var(--entity-browser-surface-soft);
}

.entity-card.is-expanded {
  border-color: var(--color-action-primary-fill, var(--entity-browser-surface));
}

.entity-card__title-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.5rem;
}

.entity-card__name {
  font-size: 1rem;
  font-weight: 600;
  color: var(--insight-text-primary, var(--entity-browser-text-blue));
  word-break: break-all;
}

.entity-card__frequency-badge {
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.125rem 0.375rem;
  background: var(--entity-browser-surface-soft-blue);
  border: 1px solid var(--entity-browser-stroke-light-blue);
  color: var(--entity-browser-text-blue-mid);
  border-radius: 0.375rem;
  white-space: nowrap;
}

.entity-card__id {
  font-size: 0.75rem;
  color: var(--insight-text-tertiary, var(--entity-browser-text-blue-mid-alt2));
  margin-top: 0.25rem;
  font-family: monospace;
}

.entity-card__aliases {
  margin-top: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.entity-card__aliases-label {
  font-size: 0.75rem;
  color: var(--insight-text-secondary, var(--entity-browser-text));
}

.entity-card__alias-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}

.entity-alias-tag {
  font-size: 0.75rem;
  background: var(--entity-browser-surface-faint-white);
  border: 1px solid var(--entity-browser-stroke-whisper);
  padding: 0.1rem 0.375rem;
  border-radius: 0.25rem;
  color: var(--insight-text-secondary, var(--entity-browser-text-blue-bright));
}

.entity-card__no-aliases {
  margin-top: 0.75rem;
  font-size: 0.75rem;
  color: var(--insight-text-tertiary, var(--entity-browser-text-blue-mid-alt2));
  font-style: italic;
}

.entity-card__footer {
  margin-top: auto;
  padding-top: 0.75rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-top: 1px dashed var(--insight-view-sidebar-divider, var(--entity-browser-surface-faint-white));
}

.entity-card__pages-summary {
  font-size: 0.75rem;
  color: var(--insight-text-secondary, var(--entity-browser-text));
}

.entity-card__expand-arrow {
  font-size: 0.75rem;
  color: var(--entity-browser-surface);
}

.entity-card__details {
  margin-top: 0.75rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--insight-view-sidebar-divider, var(--entity-browser-stroke-whisper));
}

.entity-card__detail-row {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
}

.entity-card__detail-label {
  font-size: 0.75rem;
  font-weight: 500;
  color: var(--insight-text-secondary, var(--entity-browser-text));
}

.entity-card__page-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  max-height: 120px;
  overflow-y: auto;
}

.entity-page-chip {
  font-size: 0.7rem;
  background: var(--entity-browser-surface-whisper-blue);
  border: 1px solid var(--entity-browser-stroke-soft);
  color: var(--entity-browser-text-blue-bright-alt2);
  padding: 0.1rem 0.35rem;
  border-radius: 0.25rem;
}
</style>
