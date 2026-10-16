<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useInsightStore } from '@/stores/insightStore'
import { listInsightEntities, type V2InsightEntity } from '@/api/v2/insight'
import UiButton from '@/components/ui/UiButton.vue'

const insightStore = useInsightStore()

const entities = ref<V2InsightEntity[]>([])
const isLoading = ref(false)
const errorMessage = ref('')
const selectedCategory = ref<string>('all')
const expandedEntityIds = ref<Set<string>>(new Set())

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

const filteredEntities = computed(() => {
  if (selectedCategory.value === 'all') {
    return entities.value
  }
  return entities.value.filter(ent => ent.category === selectedCategory.value)
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
    const list = entities.value.filter(e => e.category === cat.key)
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
  const bookId = insightStore.currentBookId
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

watch(() => insightStore.currentBookId, () => {
  fetchEntities()
}, { immediate: true })

onMounted(() => {
  fetchEntities()
})
</script>

<template>
  <div class="entity-browser-panel">
    <!-- Header / Filter Bar -->
    <div class="entity-browser-panel__header">
      <div class="entity-browser-panel__filters">
        <button
          v-for="cat in categories"
          :key="cat.key"
          type="button"
          class="entity-filter-chip"
          :class="{ 'is-active': selectedCategory === cat.key }"
          @click="selectedCategory = cat.key"
        >
          <span class="entity-filter-chip__icon">{{ cat.icon }}</span>
          <span class="entity-filter-chip__label">{{ cat.label }}</span>
          <span class="entity-filter-chip__count">{{ categoryCountMap[cat.key] ?? 0 }}</span>
        </button>
      </div>

      <div class="entity-browser-panel__actions">
        <UiButton
          variant="secondary"
          size="sm"
          :loading="isLoading"
          @click="fetchEntities"
        >
          刷新
        </UiButton>
      </div>
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
  border-bottom: 1px solid var(--insight-view-sidebar-divider, rgba(255, 255, 255, 0.08));
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
  background: var(--insight-surface-secondary, rgba(255, 255, 255, 0.04));
  border: 1px solid var(--insight-view-sidebar-divider, rgba(255, 255, 255, 0.08));
  border-radius: 9999px;
  color: var(--insight-text-secondary, #94a3b8);
  font-size: 0.875rem;
  cursor: pointer;
  transition: all 0.15s ease;
}

.entity-filter-chip:hover {
  background: var(--insight-surface-tertiary, rgba(255, 255, 255, 0.08));
  color: var(--insight-text-primary, #f8fafc);
}

.entity-filter-chip.is-active {
  background: var(--color-action-primary-fill, #3b82f6);
  border-color: var(--color-action-primary-fill, #3b82f6);
  color: #ffffff;
}

.entity-filter-chip__count {
  font-size: 0.75rem;
  padding: 0.1rem 0.4rem;
  background: rgba(0, 0, 0, 0.2);
  border-radius: 9999px;
}

.entity-browser-panel__error {
  padding: 0.75rem 1rem;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 0.5rem;
  color: #ef4444;
  margin-bottom: 1rem;
}

.entity-browser-panel__loading,
.entity-browser-panel__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 4rem 1rem;
  color: var(--insight-text-secondary, #94a3b8);
  text-align: center;
}

.entity-browser-panel__spinner {
  width: 2rem;
  height: 2rem;
  border: 3px solid rgba(255, 255, 255, 0.1);
  border-top-color: var(--color-action-primary-fill, #3b82f6);
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
  color: var(--insight-text-primary, #f8fafc);
  margin-bottom: 0.5rem;
}

.entity-browser-panel__empty-subtext {
  font-size: 0.875rem;
  color: var(--insight-text-secondary, #94a3b8);
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
  color: var(--insight-text-primary, #f8fafc);
}

.entity-group-section__badge {
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.125rem 0.5rem;
  background: var(--insight-surface-tertiary, rgba(255, 255, 255, 0.1));
  border-radius: 9999px;
  color: var(--insight-text-secondary, #94a3b8);
}

.entity-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1rem;
}

.entity-card {
  background: var(--insight-surface-secondary, rgba(30, 41, 59, 0.5));
  border: 1px solid var(--insight-view-sidebar-divider, rgba(255, 255, 255, 0.08));
  border-radius: 0.75rem;
  padding: 1rem;
  cursor: pointer;
  transition: all 0.2s ease;
  display: flex;
  flex-direction: column;
}

.entity-card:hover {
  background: var(--insight-surface-tertiary, rgba(30, 41, 59, 0.8));
  border-color: var(--color-action-primary-fill, rgba(59, 130, 246, 0.5));
  transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
}

.entity-card.is-expanded {
  border-color: var(--color-action-primary-fill, #3b82f6);
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
  color: var(--insight-text-primary, #f8fafc);
  word-break: break-all;
}

.entity-card__frequency-badge {
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.125rem 0.375rem;
  background: rgba(59, 130, 246, 0.15);
  border: 1px solid rgba(59, 130, 246, 0.3);
  color: #60a5fa;
  border-radius: 0.375rem;
  white-space: nowrap;
}

.entity-card__id {
  font-size: 0.75rem;
  color: var(--insight-text-tertiary, #64748b);
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
  color: var(--insight-text-secondary, #94a3b8);
}

.entity-card__alias-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}

.entity-alias-tag {
  font-size: 0.75rem;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.08);
  padding: 0.1rem 0.375rem;
  border-radius: 0.25rem;
  color: var(--insight-text-secondary, #cbd5e1);
}

.entity-card__no-aliases {
  margin-top: 0.75rem;
  font-size: 0.75rem;
  color: var(--insight-text-tertiary, #64748b);
  font-style: italic;
}

.entity-card__footer {
  margin-top: auto;
  padding-top: 0.75rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-top: 1px dashed var(--insight-view-sidebar-divider, rgba(255, 255, 255, 0.06));
}

.entity-card__pages-summary {
  font-size: 0.75rem;
  color: var(--insight-text-secondary, #94a3b8);
}

.entity-card__expand-arrow {
  font-size: 0.75rem;
  color: #3b82f6;
}

.entity-card__details {
  margin-top: 0.75rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--insight-view-sidebar-divider, rgba(255, 255, 255, 0.08));
}

.entity-card__detail-row {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
}

.entity-card__detail-label {
  font-size: 0.75rem;
  font-weight: 500;
  color: var(--insight-text-secondary, #94a3b8);
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
  background: rgba(59, 130, 246, 0.1);
  border: 1px solid rgba(59, 130, 246, 0.2);
  color: #93c5fd;
  padding: 0.1rem 0.35rem;
  border-radius: 0.25rem;
}
</style>
