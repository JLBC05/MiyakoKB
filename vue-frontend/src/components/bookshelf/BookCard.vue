<script setup lang="ts">
import type { BookData, TagData } from '@/types'
import ProductChipList from '@/components/product/ProductChipList.vue'
import type { ProductChipItem } from '@/components/product/ProductChipList.vue'
import ProductRecordCard from '@/components/product/ProductRecordCard.vue'
import ProductHeaderAction from '@/components/product/ProductHeaderAction.vue'
import { computed, ref, watch } from 'vue'
import UiCheckbox from '@/components/ui/UiCheckbox.vue'
import TaskStatusBadge from '@/components/task-center/TaskStatusBadge.vue'

interface Props {
  book: BookData
  tags?: TagData[]
  selectable?: boolean
  selected?: boolean
  knowledgeStatus?: {
    status: 'completed' | 'partial' | 'none'
    totalChapters: number
    analyzedChapters: number
  }
}

const props = withDefaults(defineProps<Props>(), {
  tags: () => [],
  selectable: false,
  selected: false,
  knowledgeStatus: undefined,
})

const emit = defineEmits<{
  click: []
  select: [selected: boolean]
  buildKnowledge: []
}>()

const coverFailed = ref(false)

const hasVisibleCover = computed(() => {
  return Boolean(props.book.cover && props.book.cover.length > 0 && !coverFailed.value)
})

const tagItems = computed<ProductChipItem[]>(() => {
  return props.book.tags?.map(tag => ({
    id: tag,
    label: tag,
    tone: 'custom',
    backgroundColor: getTagColor(tag),
    textColor: 'var(--color-text-inverse)',
  })) ?? []
})

watch(() => props.book.cover, () => {
  coverFailed.value = false
})

function handleClick() {
  if (props.selectable) return
  emit('click')
}

function getTagColor(tagName: string): string {
  const tagInfo = props.tags.find(tag => tag.name === tagName)
  return tagInfo?.color || 'var(--color-action-brand)'
}

function handleImageError() {
  coverFailed.value = true
}
</script>

<template>
  <div class="book-card-shell">
    <ProductRecordCard
      :as="selectable ? 'article' : 'button'"
      class="book-card"
      :accent="selected"
      :aria-label="selectable ? `批量选择书籍：${book.title}` : `打开书籍：${book.title}`"
      @click="handleClick"
    >
      <div class="book-card__cover">
        <img
          v-if="hasVisibleCover"
          class="book-card__cover-image"
          :src="book.cover"
          :alt="book.title"
          loading="lazy"
          @error="handleImageError"
        >
        <div v-else class="book-card__cover-placeholder" aria-label="无封面">📖</div>
      </div>

      <div class="book-card__info">
        <h3 class="book-card__title" :title="book.title">{{ book.title }}</h3>
        <div class="book-card__meta-row">
          <span class="book-card__chapter-count">{{ book.chapterCount ?? book.chapters?.length ?? 0 }} 章节</span>
          <span
            class="knowledge-badge"
            :class="knowledgeStatus?.status || 'none'"
            :title="knowledgeStatus?.status === 'completed' ? '知识库已构建完成' : knowledgeStatus?.status === 'partial' ? `已构建 ${knowledgeStatus.analyzedChapters} / ${knowledgeStatus.totalChapters} 话` : '尚未构建知识库'"
          >
            <template v-if="knowledgeStatus?.status === 'completed'">
              ✓ 已构建知识库
            </template>
            <template v-else-if="knowledgeStatus?.status === 'partial'">
              ⚡ 部分构建 ({{ knowledgeStatus.analyzedChapters }}/{{ knowledgeStatus.totalChapters }}话)
            </template>
            <template v-else>
              ○ 未构建知识库
            </template>
          </span>
        </div>
        <ProductChipList
          v-if="tagItems.length > 0"
          class="book-card__tags"
          aria-label="书籍标签"
          :items="tagItems"
        />
        <div v-if="!selectable" class="book-card__actions" @click.stop>
          <ProductHeaderAction
            class="build-knowledge-btn"
            variant="solid"
            tone="primary"
            label="⚡ 构建知识库"
            title="开始构建该漫画的知识库"
            @click.stop="emit('buildKnowledge')"
          />
        </div>
      </div>
    </ProductRecordCard>
    <UiCheckbox
      v-if="selectable"
      class="book-card-shell__selection"
      :model-value="selected"
      :aria-label="`选择书籍：${book.title}`"
      @change="$emit('select', $event)"
    />
    <TaskStatusBadge
      class="book-card-shell__task-status"
      :book-id="book.id"
      :summary="book.jobStatusSummary"
    />
  </div>
</template>

<style scoped>
.book-card-shell {
  position: relative;
  min-width: 0;
}

.book-card {
  --product-record-card-background: var(--color-surface-card);
  --product-record-card-radius: var(--radius-lg);
  --product-record-card-padding: 0;
  --product-record-card-gap: 0;
  --product-record-card-shadow: 0 4px 12px var(--book-card-shadow);
  --product-record-card-shadow-hover: 0 12px 32px var(--book-card-hover-shadow);
  --book-card-hover-border: var(--color-focus-brand-subtle);
  --book-card-shadow: var(--shadow-soft);
  --book-card-hover-shadow: var(--shadow-medium);
  --book-card-cover-overlay: var(--color-overlay-backdrop-strong);
  --book-card-cover-placeholder: var(--color-text-inverse);

  display: block;
  border: 0;
  overflow: hidden;
  position: relative;
  transition: transform 0.3s ease, box-shadow 0.3s ease;
}

.book-card-shell__selection,
.book-card-shell__task-status {
  position: absolute;
  top: 10px;
  z-index: var(--z-local);
}

.book-card-shell__selection {
  left: 10px;
  padding: 6px;
  background: var(--color-surface-card);
  border-radius: var(--radius-sm);
}

.book-card-shell__task-status {
  right: 10px;
}

.book-card::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: var(--radius-lg);
  border: 2px solid transparent;
  transition: border-color 0.2s ease;
  pointer-events: none;
}

.book-card:hover {
  transform: translateY(-6px) scale(1.02);
  box-shadow: 0 12px 32px var(--book-card-hover-shadow);
}

.book-card:hover::after,
.book-card:focus-visible::after {
  border-color: var(--book-card-hover-border);
}

.book-card:active {
  transform: translateY(-2px) scale(1.01);
}

.book-card__cover {
  aspect-ratio: 3 / 4;
  background: linear-gradient(135deg, var(--color-action-brand-fill, var(--color-action-brand)) 0%, var(--color-action-brand-fill-strong, var(--color-action-brand-strong)) 100%);
  display: block;
  overflow: hidden;
  position: relative;
  border-radius: var(--radius-lg) var(--radius-lg) 0 0;
}

.book-card__cover-image {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center;
  transition: transform 0.3s ease;
}

.book-card:hover .book-card__cover-image,
.book-card:focus-visible .book-card__cover-image {
  transform: scale(1.05);
}

.book-card__cover::before {
  content: '查看详情';
  position: absolute;
  inset: 0;
  background: var(--book-card-cover-overlay);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-inverse);
  font-size: 0.9rem;
  font-weight: 500;
  opacity: 0;
  transition: opacity 0.2s ease;
  z-index: var(--z-local);
}

.book-card:hover .book-card__cover::before,
.book-card:focus-visible .book-card__cover::before {
  opacity: 1;
}

.book-card__cover-placeholder {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  color: var(--book-card-cover-placeholder);
  font-size: 3rem;
  font-weight: 400;
  letter-spacing: 0;
  white-space: nowrap;
}

.book-card__info {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.book-card__title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--color-text-default);
  margin: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1.3;
}

.book-card__meta-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  margin: 3px 0;
  flex-wrap: wrap;
}

.book-card__chapter-count {
  font-size: 0.8rem;
  color: var(--color-text-supporting);
  margin: 0;
}

.knowledge-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 9999px;
  line-height: 1.2;
  white-space: nowrap;
  letter-spacing: 0.02em;
}

.knowledge-badge.completed {
  background: color-mix(in srgb, var(--color-action-success-strong) 15%, transparent);
  color: var(--color-action-success-strong);
  border: 1px solid color-mix(in srgb, var(--color-action-success-strong) 35%, transparent);
}

.knowledge-badge.partial {
  background: color-mix(in srgb, var(--color-surface-brand-strong) 15%, transparent);
  color: var(--color-surface-brand-strong);
  border: 1px solid color-mix(in srgb, var(--color-surface-brand-strong) 35%, transparent);
}

.knowledge-badge.none {
  background: color-mix(in srgb, var(--color-text-supporting) 12%, transparent);
  color: var(--color-text-supporting);
  border: 1px solid color-mix(in srgb, var(--color-text-supporting) 20%, transparent);
}

.book-card__tags {
  margin-top: 4px;
}

.book-card__actions {
  margin-top: 8px;
  display: flex;
}

.build-knowledge-btn {
  --product-header-action-padding: 4px 10px;
  --product-header-action-radius: 6px;
  --product-header-action-min-height: 26px;
  --product-header-action-font-size: 0.78rem;
  --product-header-action-gap: 4px;

  font-weight: 600;
  transition: opacity 0.2s, transform 0.1s;
}

.build-knowledge-btn:hover {
  opacity: 0.9;
  transform: translateY(-1px);
}

.build-knowledge-btn:active {
  transform: translateY(0);
}
</style>
