<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useInsightStore } from '@/stores/insightStore'
import * as insightApi from '@/api/insight'
import { listInsightEntities, type V2InsightEntity } from '@/api/v2/insight'
import type { TimelineData, TimelineEvent, PlotThread, TimelineArc } from '@/types/insightTimelineTypes'
import UiButton from '@/components/ui/UiButton.vue'
import BaseModal from '@/components/common/BaseModal.vue'
import UiSpinner from '@/components/ui/UiSpinner.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import ProductStatusBanner from '@/components/product/ProductStatusBanner.vue'
import { triggerBlobDownload } from '@/utils/browserDownload'
import { downloadBlob } from '@/api/download'
import { insightKnowledgeArchiveUrl } from '@/api/v2/insight'
import { showToast } from '@/utils/toast'

const emit = defineEmits<{
  (e: 'view-entity', entityId: string): void
}>()

const route = useRoute()
const insightStore = useInsightStore()

const isLoading = ref(false)
const isRegenerating = ref(false)
const isExportingArchive = ref(false)
const errorMessage = ref('')
const pendingMessage = ref('')

const timelineData = ref<TimelineData | null>(null)
const mainEntities = ref<V2InsightEntity[]>([])

// 缩略图大图预览弹窗
const previewImageUrl = ref<string | null>(null)
const previewZoomed = ref(false)

const bookId = computed(() => {
  const queryBook = typeof route?.query?.book === 'string' ? route.query.book.trim() : ''
  return insightStore.currentBookId || queryBook || ''
})

// 故事概要文本
const storySummary = computed(() => {
  return timelineData.value?.story_summary || ''
})

// 剧情阶段
const plotArcs = computed<TimelineArc[]>(() => {
  return timelineData.value?.plot_arcs || []
})

// 剧情线与伏笔
const plotThreads = computed<PlotThread[]>(() => {
  return timelineData.value?.plot_threads || []
})

// 关键时间线事件
const timelineEvents = computed<TimelineEvent[]>(() => {
  return timelineData.value?.events || []
})

// 主要角色列表（优先使用实体知识库，按出场频次从高到低排序）
const displayCharacters = computed(() => {
  return [...mainEntities.value].sort((a, b) => b.frequency - a.frequency)
})

// 数据统计指标
const stats = computed(() => {
  return {
    arcsCount: plotArcs.value.length,
    threadsCount: plotThreads.value.length,
    eventsCount: timelineEvents.value.length,
    charactersCount: displayCharacters.value.length,
  }
})

// 格式化段落文本
const summaryParagraphs = computed(() => {
  if (!storySummary.value) return []
  return storySummary.value
    .split(/\n\s*\n|\n/)
    .map(p => p.trim())
    .filter(Boolean)
})

async function loadSynopsisData() {
  const targetBookId = bookId.value
  if (!targetBookId) {
    timelineData.value = null
    mainEntities.value = []
    return
  }

  isLoading.value = true
  errorMessage.value = ''
  pendingMessage.value = ''

  try {
    const [timelineRes, entitiesRes] = await Promise.allSettled([
      insightApi.getTimeline(targetBookId),
      listInsightEntities(targetBookId, 'characters'),
    ])

    if (timelineRes.status === 'fulfilled') {
      timelineData.value = timelineRes.value
    } else {
      console.warn('获取时间线数据失败:', timelineRes.reason)
      timelineData.value = null
    }

    if (entitiesRes.status === 'fulfilled') {
      mainEntities.value = entitiesRes.value.entities || []
    } else {
      console.warn('获取角色实体失败:', entitiesRes.reason)
      mainEntities.value = []
    }

    if (!timelineData.value && mainEntities.value.length === 0) {
      errorMessage.value = '暂无故事梗概数据，请确认是否已构建知识库。'
    }
  } catch (err: any) {
    console.error('加载故事梗概失败:', err)
    errorMessage.value = err?.message || '加载故事梗概失败'
  } finally {
    isLoading.value = false
  }
}

// 重新生成时间线
async function handleRegenerate() {
  const targetBookId = bookId.value
  if (!targetBookId || isRegenerating.value) return

  isRegenerating.value = true
  pendingMessage.value = '故事时间线重新生成任务已下发，请稍候…'
  try {
    await insightApi.regenerateTimeline(targetBookId)
    showToast('时间线重新生成任务已提交，将在后台执行', 'info')
    // 短暂轮询或延迟后重新拉取
    setTimeout(() => {
      loadSynopsisData()
      isRegenerating.value = false
      pendingMessage.value = ''
    }, 4000)
  } catch (err: any) {
    errorMessage.value = err?.message || '重新生成时间线失败'
    isRegenerating.value = false
    pendingMessage.value = ''
  }
}

// 导出完整的 Markdown 故事梗概
function handleExportMarkdown() {
  if (!timelineData.value && mainEntities.value.length === 0) return

  const lines: string[] = []
  lines.push(`# 漫画故事梗概与时间线\n`)

  if (storySummary.value) {
    lines.push(`## 故事概要\n`)
    lines.push(storySummary.value)
    lines.push(`\n`)
  }

  if (displayCharacters.value.length > 0) {
    lines.push(`## 主要出场角色\n`)
    for (const c of displayCharacters.value) {
      const aliasStr = c.aliases.length > 0 ? ` (别名: ${c.aliases.join('、')})` : ''
      lines.push(`- **${c.canonical_name}**${aliasStr}：出场频次 ${c.frequency} 次，首登场第 ${c.pages[0] ?? 1} 页`)
    }
    lines.push(`\n`)
  }

  if (plotArcs.value.length > 0) {
    lines.push(`## 剧情核心阶段\n`)
    for (const arc of plotArcs.value) {
      const range = arc.page_range ? ` (P.${arc.page_range.start} - P.${arc.page_range.end})` : ''
      lines.push(`### ${arc.name}${range}\n${arc.description}\n`)
    }
    lines.push(`\n`)
  }

  if (plotThreads.value.length > 0) {
    lines.push(`## 关键伏笔与剧情线\n`)
    for (const thread of plotThreads.value) {
      lines.push(`- **[${thread.type} | ${thread.status}] ${thread.name}**：${thread.description || '暂无详细描述'}`)
    }
    lines.push(`\n`)
  }

  if (timelineEvents.value.length > 0) {
    lines.push(`## ⏳ 剧情关键时间线\n`)
    timelineEvents.value.forEach((ev, idx) => {
      const pageStr = ev.page_numbers?.length ? ` [P.${ev.page_numbers.join(', ')}]` : ''
      lines.push(`${idx + 1}. **${pageStr}** ${ev.summary}`)
    })
    lines.push(`\n`)
  }

  const content = lines.join('\n')
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
  triggerBlobDownload(blob, `故事梗概_${bookId.value.slice(0, 8)}.md`)
  showToast('故事梗概 Markdown 已导出', 'success')
}

// 整库导出：把实体网络、时间线、概览、逐页分析与角色资料打成一个 zip。
async function handleExportKnowledgeArchive(): Promise<void> {
  if (isExportingArchive.value) return
  isExportingArchive.value = true
  try {
    const result = await downloadBlob({
      url: insightKnowledgeArchiveUrl(bookId.value),
      fallbackFilename: 'knowledge-archive.zip',
      fallbackErrorMessage: '导出知识库失败',
    })
    triggerBlobDownload(result.blob, result.filename)
    showToast('知识库已导出为 zip 压缩包', 'success')
  } catch (archiveError) {
    showToast(
      archiveError instanceof Error ? archiveError.message : '导出知识库失败',
      'error',
    )
  } finally {
    isExportingArchive.value = false
  }
}

function getThumbnail(event: TimelineEvent): string | null {
  if (!timelineData.value?.page_thumbnails) return null
  const pageNum = event.page_numbers?.[0]
  if (pageNum !== undefined && timelineData.value.page_thumbnails[pageNum]) {
    return timelineData.value.page_thumbnails[pageNum]
  }
  return null
}

/**
 * 大图预览地址：优先取该页原图（page_sources），
 * 缺失时退回缩略图，避免「放大反而更小更糊」。
 */
function getPreviewSource(event: TimelineEvent): string | null {
  const pageNum = event.page_numbers?.[0]
  if (pageNum !== undefined && timelineData.value?.page_sources?.[pageNum]) {
    return timelineData.value.page_sources[pageNum]
  }
  return getThumbnail(event)
}

function openThumbnailPreview(event: TimelineEvent) {
  const url = getPreviewSource(event)
  if (!url) return
  previewImageUrl.value = url
  previewZoomed.value = false
}

function closeThumbnailPreview() {
  previewImageUrl.value = null
}

watch(
  () => [insightStore.currentBookId, route?.query?.book],
  () => {
    loadSynopsisData()
  },
  { immediate: true },
)

onMounted(() => {
  loadSynopsisData()
})
</script>

<template>
  <div class="story-synopsis-panel">
    <!-- 顶部操作条 -->
    <header class="synopsis-header">
      <div class="synopsis-header__title-group">
        <div class="synopsis-header__badge">全景画卷</div>
        <h2 class="synopsis-header__title">故事梗概</h2>
        <span class="synopsis-header__sub">三位一体全景架构：故事概要 · 核心角色 · 关键时间线</span>
      </div>

      <!-- 统计指标胶囊 -->
      <div v-if="!isLoading && (storySummary || timelineEvents.length > 0)" class="synopsis-stat-capsules">
        <div class="stat-capsule" title="剧情发展阶段">
          <span class="stat-capsule__num">{{ stats.arcsCount }}</span>
          <span class="stat-capsule__label">剧情阶段</span>
        </div>
        <div class="stat-capsule" title="主要出场角色">
          <span class="stat-capsule__num">{{ stats.charactersCount }}</span>
          <span class="stat-capsule__label">出场角色</span>
        </div>
        <div class="stat-capsule" title="剧情关键事件">
          <span class="stat-capsule__num">{{ stats.eventsCount }}</span>
          <span class="stat-capsule__label">关键事件</span>
        </div>
        <div v-if="stats.threadsCount > 0" class="stat-capsule" title="关键伏笔与悬念">
          <span class="stat-capsule__num">{{ stats.threadsCount }}</span>
          <span class="stat-capsule__label">关键伏笔</span>
        </div>
      </div>

      <!-- 右侧操作按钮 -->
      <div class="synopsis-header__actions">
        <UiButton
          size="sm"
          variant="secondary"
          :disabled="isLoading || (!storySummary && timelineEvents.length === 0)"
          @click="handleExportMarkdown"
        >
          <UiIcon name="download" :size="14" /> 导出 Markdown
        </UiButton>
        <UiButton
          size="sm"
          variant="secondary"
          :disabled="isExportingArchive"
          title="把实体网络、时间线、概览、逐页分析与角色资料打成一个 zip"
          @click="handleExportKnowledgeArchive"
        >
          <UiIcon name="folder-open" :size="14" /> {{ isExportingArchive ? '导出中…' : '导出知识库' }}
        </UiButton>
        <UiButton
          size="sm"
          variant="primary"
          :disabled="isLoading || isRegenerating"
          @click="handleRegenerate"
        >
          <UiIcon name="refresh" :size="14" /> 重新生成时间线
        </UiButton>
      </div>
    </header>

    <!-- 状态提示 -->
    <ProductStatusBanner
      v-if="errorMessage"
      class="synopsis-banner"
      tone="danger"
      aria-live="assertive"
    >
      {{ errorMessage }}
    </ProductStatusBanner>

    <ProductStatusBanner
      v-if="pendingMessage"
      class="synopsis-banner"
      tone="neutral"
      aria-live="polite"
    >
      {{ pendingMessage }}
    </ProductStatusBanner>

    <!-- 加载中状态 -->
    <div v-if="isLoading" class="synopsis-loading">
      <UiSpinner :size="36" />
      <p class="synopsis-loading__text">正在提取故事概要、规范角色库与关键时间线…</p>
    </div>

    <!-- 空数据状态 -->
    <div v-else-if="!storySummary && timelineEvents.length === 0 && displayCharacters.length === 0" class="synopsis-empty">
      <UiIcon class="synopsis-empty__icon" name="book-open" :size="40" />
      <h3 class="synopsis-empty__title">暂无故事梗概</h3>
      <p class="synopsis-empty__desc">
        当前漫画尚未生成时间线与故事概要。可在漫画库或书籍详情页点击“构建知识库”启动深度分析。
      </p>
      <UiButton size="sm" variant="primary" @click="handleRegenerate">
        立即生成时间线
      </UiButton>
    </div>

    <!-- 主体内容区 -->
    <div v-else class="synopsis-body">
      <!-- 左右双栏布局 -->
      <div class="synopsis-columns">
        <!-- 左栏：故事概要与剧情伏笔 -->
        <div class="synopsis-left-col">
          <!-- 故事概要大卡片 -->
          <section class="synopsis-card synopsis-summary-card">
            <div class="synopsis-card__header">
              <UiIcon class="synopsis-card__icon" name="book-open" :size="18" />
              <h3 class="synopsis-card__title">宏观故事概要</h3>
            </div>
            <div class="synopsis-summary-card__content">
              <p
                v-for="(para, idx) in summaryParagraphs"
                :key="idx"
                class="synopsis-summary-para"
              >
                {{ para }}
              </p>
            </div>
          </section>

          <!-- 剧情核心阶段 (Plot Arcs) -->
          <section v-if="plotArcs.length > 0" class="synopsis-card">
            <div class="synopsis-card__header">
              <UiIcon class="synopsis-card__icon" name="map-pin" :size="18" />
              <h3 class="synopsis-card__title">剧情核心阶段</h3>
            </div>
            <div class="synopsis-arcs-list">
              <div v-for="arc in plotArcs" :key="arc.id" class="arc-item">
                <div class="arc-item__header">
                  <h4 class="arc-item__name">{{ arc.name }}</h4>
                  <span v-if="arc.page_range" class="arc-item__range">
                    P.{{ arc.page_range.start }} - P.{{ arc.page_range.end }}
                  </span>
                </div>
                <p class="arc-item__desc">{{ arc.description }}</p>
              </div>
            </div>
          </section>

          <!-- 关键伏笔与悬念 (Plot Threads) -->
          <section v-if="plotThreads.length > 0" class="synopsis-card">
            <div class="synopsis-card__header">
              <UiIcon class="synopsis-card__icon" name="link" :size="18" />
              <h3 class="synopsis-card__title">关键伏笔与主线悬念</h3>
            </div>
            <div class="synopsis-threads-list">
              <div
                v-for="thread in plotThreads"
                :key="thread.id"
                class="thread-item"
                :class="{ 'thread-item--resolved': thread.status === '已解决' }"
              >
                <div class="thread-item__tags">
                  <span class="thread-type-badge">{{ thread.type }}</span>
                  <span
                    class="thread-status-badge"
                    :class="thread.status === '已解决' ? 'status--resolved' : 'status--open'"
                  >
                    {{ thread.status }}
                  </span>
                </div>
                <h4 class="thread-item__name">{{ thread.name }}</h4>
                <p v-if="thread.description" class="thread-item__desc">
                  {{ thread.description }}
                </p>
              </div>
            </div>
          </section>
        </div>

        <!-- 右栏：核心主要角色（打通规范实体库） -->
        <div class="synopsis-right-col">
          <section class="synopsis-card synopsis-characters-card">
            <div class="synopsis-card__header">
              <UiIcon class="synopsis-card__icon" name="users" :size="18" />
              <h3 class="synopsis-card__title">核心主要角色</h3>
              <span class="synopsis-card__badge">{{ displayCharacters.length }} 位</span>
            </div>
            <p class="synopsis-card__subtitle">
              打通实体知识库，按出场频次排序并聚合别名变体
            </p>

            <div class="synopsis-characters-grid">
              <div
                v-for="char in displayCharacters"
                :key="char.entity_id"
                class="char-card"
                @click="emit('view-entity', char.entity_id)"
              >
                <div class="char-card__avatar">
                  {{ char.canonical_name.slice(0, 1) }}
                </div>
                <div class="char-card__info">
                  <div class="char-card__name-row">
                    <span class="char-card__name">{{ char.canonical_name }}</span>
                    <span class="char-card__freq" title="总出场频次">
                      <UiIcon name="bar-chart" :size="12" /> {{ char.frequency }}
                    </span>
                  </div>
                  <div class="char-card__slug">
                    {{ char.entity_id }}
                  </div>
                  <div v-if="char.aliases.length > 0" class="char-card__aliases">
                    <span
                      v-for="alias in char.aliases.slice(0, 3)"
                      :key="alias"
                      class="alias-pill"
                    >
                      {{ alias }}
                    </span>
                    <span v-if="char.aliases.length > 3" class="alias-pill alias-pill--more">
                      +{{ char.aliases.length - 3 }}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>

      <!-- 下方全宽：关键剧情时间线 (Story Timeline) -->
      <section class="synopsis-card synopsis-timeline-card">
        <div class="synopsis-card__header">
          <UiIcon class="synopsis-card__icon" name="clock" :size="18" />
          <h3 class="synopsis-card__title">剧情关键发展时间线</h3>
          <span class="synopsis-card__badge">{{ timelineEvents.length }} 个节点</span>
        </div>
        <p class="synopsis-card__subtitle">
          按剧情先后逻辑梳理的叙事脉络与重大转折事件
        </p>

        <div class="timeline-track-container">
          <div class="timeline-track">
            <div
              v-for="(event, idx) in timelineEvents"
              :key="event.eventId || idx"
              class="timeline-node"
            >
              <!-- 时间轴指示器 -->
              <div class="timeline-indicator">
                <div class="timeline-dot">
                  <span class="timeline-dot__num">{{ idx + 1 }}</span>
                </div>
                <div v-if="idx < timelineEvents.length - 1" class="timeline-line"></div>
              </div>

              <!-- 事件内容卡片 -->
              <div class="timeline-content-card">
                <div class="timeline-card__meta">
                  <span v-if="event.page_numbers?.length" class="timeline-card__page-tag">
                    <UiIcon name="file-text" :size="12" /> P.{{ event.page_numbers.join(', ') }}
                  </span>
                  <span v-if="event.importance" class="timeline-card__imp-tag">
                    {{ event.importance }}
                  </span>
                </div>

                <p class="timeline-card__summary">
                  {{ event.summary }}
                </p>

                <!-- 页面画面微缩图 -->
                <div
                  v-if="getThumbnail(event)"
                  class="timeline-card__thumb-wrap"
                  @click="openThumbnailPreview(event)"
                  title="点击查看页面大图"
                >
                  <img
                    :src="getThumbnail(event)!"
                    alt="剧情画面"
                    class="timeline-card__thumb-img"
                    loading="lazy"
                  />
                  <div class="timeline-card__thumb-hover"><UiIcon name="search" :size="14" /> 放大</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>

    <!-- 缩略图大图弹窗 -->
    <BaseModal
      :model-value="Boolean(previewImageUrl)"
      title="图片预览"
      size="full"
      backdrop="strong"
      backdrop-effect="blur-sm"
      body-padding="none"
      @update:model-value="closeThumbnailPreview"
    >
      <div class="thumbnail-modal-card" :class="{ 'thumbnail-modal-card--zoomed': previewZoomed }">
        <img
          :src="previewImageUrl ?? undefined"
          alt="全屏预览"
          class="thumbnail-modal-img"
          :class="{ 'thumbnail-modal-img--zoomed': previewZoomed }"
          title="点击切换原始尺寸"
          @click="previewZoomed = !previewZoomed"
        >
      </div>
      <p class="thumbnail-modal-hint">
        点击图片切换原始尺寸 · 放大后可在弹窗内滚动查看（{{ previewZoomed ? '原始尺寸' : '适合窗口' }}）
      </p>
    </BaseModal>
  </div>
</template>

<style scoped>
.story-synopsis-panel {
  --synopsis-accent-faint: rgba(0, 0, 0, 0.03);
  --synopsis-accent-faint-black: rgba(0, 0, 0, 0.05);
  --synopsis-shadow-medium: rgba(0, 0, 0, 0.5);
  --synopsis-accent-whisper: rgba(139, 92, 246, 0.12);
  --synopsis-accent-whisper-violet: rgba(139, 92, 246, 0.1);
  --synopsis-accent-soft: rgba(139, 92, 246, 0.2);
  --synopsis-accent: #8b5cf6;
  --synopsis-accent-amber: #f59e0b;
  --synopsis-accent-soft-amber: rgba(245, 158, 11, 0.15);
  --synopsis-accent-whisper-amber: rgba(245, 158, 11, 0.12);
  --synopsis-accent-teal: #10b981;
  --synopsis-accent-soft-teal: rgba(16, 185, 129, 0.15);
  --synopsis-accent-red-mid: #ef4444;
  --synopsis-accent-whisper-red: rgba(239, 68, 68, 0.1);
  --synopsis-accent-soft-blue: rgba(148, 163, 184, 0.15);
  --synopsis-accent-black: #000;
  --synopsis-shadow: 0 1px 3px var(--synopsis-accent-faint);
  --synopsis-shadow-gray: 0 1px 3px var(--synopsis-accent-faint-black);
  --synopsis-accent-gray: 0 10px 30px var(--synopsis-shadow-medium);
  --synopsis-surface: var(--synopsis-accent-whisper);
  --synopsis-surface-gray: var(--synopsis-accent-whisper-violet);
  --synopsis-shadow-gray-mid: var(--synopsis-accent-soft);
  --synopsis-surface-gray-mid: var(--synopsis-accent);
  --synopsis-text: var(--synopsis-accent-amber);
  --synopsis-surface-gray-mid-alt2: var(--synopsis-accent-soft-amber);
  --synopsis-surface-gray-mid-alt3: var(--synopsis-accent-whisper-amber);
  --synopsis-text-gray: var(--synopsis-accent-teal);
  --synopsis-surface-gray-mid-alt4: var(--synopsis-accent-soft-teal);
  --synopsis-text-gray-mid: var(--synopsis-accent-red-mid);
  --synopsis-surface-gray-mid-alt5: var(--synopsis-accent-whisper-red);
  --synopsis-surface-gray-mid-alt6: var(--synopsis-accent-soft-blue);
  --synopsis-surface-gray-mid-alt7: var(--synopsis-shadow-medium);
  --synopsis-surface-gray-mid-alt9: var(--synopsis-accent-black);

  display: flex;
  flex-direction: column;
  gap: 20px;
  width: 100%;
  animation: fadeIn 0.25s ease-out;
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

/* 顶部操作头 */
.synopsis-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 16px 20px;
  box-shadow: var(--synopsis-shadow-gray);
  flex-wrap: wrap;
}

.synopsis-header__title-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.synopsis-header__badge {
  align-self: flex-start;
  font-size: 11px;
  font-weight: 700;
  color: var(--color-brand-primary);
  background: var(--synopsis-surface);
  padding: 2px 8px;
  border-radius: 6px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.synopsis-header__title {
  font-size: 20px;
  font-weight: 700;
  color: var(--color-text-primary);
  margin: 0;
}

.synopsis-header__sub {
  font-size: 13px;
  color: var(--color-text-muted);
}

.synopsis-stat-capsules {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.stat-capsule {
  display: flex;
  align-items: baseline;
  gap: 4px;
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-subtle, var(--color-border));
  border-radius: 20px;
  padding: 4px 12px;
}

.stat-capsule__num {
  font-size: 15px;
  font-weight: 700;
  color: var(--synopsis-text);
}

.stat-capsule__label {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.synopsis-header__actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* 状态 Banner */
.synopsis-banner {
  margin: 0;
}

/* 加载中与空状态 */
.synopsis-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 64px 20px;
  gap: 16px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
}

.synopsis-loading__text {
  font-size: 14px;
  color: var(--color-text-secondary);
}

.synopsis-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 64px 24px;
  gap: 12px;
  background: var(--color-surface);
  border: 1px dashed var(--color-border);
  border-radius: 12px;
}

.synopsis-empty__icon {
  font-size: 48px;
  line-height: 1;
}

.synopsis-empty__title {
  font-size: 18px;
  font-weight: 700;
  color: var(--color-text-primary);
  margin: 0;
}

.synopsis-empty__desc {
  font-size: 14px;
  color: var(--color-text-secondary);
  max-width: 480px;
  margin: 0 0 8px;
}

/* 主体内容 */
.synopsis-body {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.synopsis-columns {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
  gap: 20px;
}

@media (--breakpoint-xl-down) {
  .synopsis-columns {
    grid-template-columns: 1fr;
  }
}

.synopsis-left-col,
.synopsis-right-col {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* 通用卡片容器 */
.synopsis-card {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 20px;
  box-shadow: var(--synopsis-shadow-gray);
}

.synopsis-card__header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.synopsis-card__icon {
  font-size: 18px;
}

.synopsis-card__title {
  font-size: 16px;
  font-weight: 700;
  color: var(--color-text-primary);
  margin: 0;
  flex: 1;
}

.synopsis-card__badge {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-brand-primary);
  background: var(--synopsis-surface);
  padding: 2px 8px;
  border-radius: 12px;
}

.synopsis-card__subtitle {
  font-size: 12px;
  color: var(--color-text-muted);
  margin: 0 0 14px;
}

/* 故事概要大卡片 */
.synopsis-summary-card__content {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 8px;
}

.synopsis-summary-para {
  font-size: 14.5px;
  line-height: 1.75;
  color: var(--color-text-primary);
  margin: 0;
  text-indent: 2em;
  letter-spacing: 0.2px;
}

/* 剧情核心阶段 (Plot Arcs) */
.synopsis-arcs-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 8px;
}

.arc-item {
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-subtle, var(--color-border));
  border-radius: 8px;
  padding: 12px 14px;
}

.arc-item__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 4px;
}

.arc-item__name {
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text-primary);
  margin: 0;
}

.arc-item__range {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--color-brand-primary);
  background: var(--synopsis-surface-gray);
  padding: 2px 6px;
  border-radius: 4px;
}

.arc-item__desc {
  font-size: 13px;
  color: var(--color-text-secondary);
  line-height: 1.55;
  margin: 0;
}

/* 关键伏笔 (Plot Threads) */
.synopsis-threads-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 10px;
  margin-top: 8px;
}

.thread-item {
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-subtle, var(--color-border));
  border-radius: 8px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.thread-item--resolved {
  opacity: 0.75;
}

.thread-item__tags {
  display: flex;
  align-items: center;
  gap: 6px;
}

.thread-type-badge {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: var(--synopsis-surface-gray-mid-alt6);
  padding: 1px 6px;
  border-radius: 4px;
}

.thread-status-badge {
  font-size: 11px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 4px;
}

.status--open {
  color: var(--synopsis-text);
  background: var(--synopsis-surface-gray-mid-alt2);
}

.status--resolved {
  color: var(--synopsis-text-gray);
  background: var(--synopsis-surface-gray-mid-alt4);
}

.thread-item__name {
  font-size: 13.5px;
  font-weight: 700;
  color: var(--color-text-primary);
  margin: 0;
}

.thread-item__desc {
  font-size: 12.5px;
  color: var(--color-text-secondary);
  line-height: 1.45;
  margin: 0;
}

/* 主要角色网格 */
.synopsis-characters-grid {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 520px;
  overflow-y: auto;
  padding-right: 4px;
}

.char-card {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-subtle, var(--color-border));
  border-radius: 10px;
  padding: 10px 14px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.char-card:hover {
  border-color: var(--color-brand-primary);
  transform: translateX(4px);
  background: var(--color-surface);
}

.char-card__avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: var(--synopsis-surface-gray-mid);
  color: var(--color-text-inverse);
  font-weight: 700;
  font-size: 15px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.char-card__info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.char-card__name-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.char-card__name {
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.char-card__freq {
  font-size: 12px;
  font-weight: 600;
  color: var(--synopsis-text-gray-mid);
  background: var(--synopsis-surface-gray-mid-alt5);
  padding: 1px 6px;
  border-radius: 10px;
  flex-shrink: 0;
}

.char-card__slug {
  font-size: 11.5px;
  font-family: monospace;
  color: var(--color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.char-card__aliases {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  margin-top: 2px;
}

.alias-pill {
  font-size: 11px;
  color: var(--color-text-secondary);
  background: var(--synopsis-surface-gray-mid-alt6);
  padding: 1px 6px;
  border-radius: 4px;
}

.alias-pill--more {
  color: var(--color-text-muted);
}

/* 故事时间线 (Vertical Timeline) */
.timeline-track-container {
  margin-top: 16px;
  padding: 8px 0;
}

.timeline-track {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.timeline-node {
  display: flex;
  gap: 16px;
  position: relative;
}

.timeline-indicator {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 32px;
  flex-shrink: 0;
}

.timeline-dot {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--color-brand-primary);
  color: var(--color-text-inverse);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  box-shadow: 0 0 0 4px var(--synopsis-shadow-gray-mid);
  z-index: var(--z-local);
}

.timeline-line {
  flex: 1;
  width: 2px;
  background: var(--color-border);
  margin: 6px 0;
}

.timeline-content-card {
  flex: 1;
  background: var(--color-bg-base);
  border: 1px solid var(--color-border-subtle, var(--color-border));
  border-radius: 10px;
  padding: 14px 18px;
  margin-bottom: 20px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  box-shadow: var(--synopsis-shadow);
}

.timeline-card__meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.timeline-card__page-tag {
  font-size: 12px;
  font-weight: 700;
  color: var(--color-brand-primary);
  background: var(--synopsis-surface);
  padding: 2px 8px;
  border-radius: 4px;
}

.timeline-card__imp-tag {
  font-size: 11px;
  font-weight: 600;
  color: var(--synopsis-text);
  background: var(--synopsis-surface-gray-mid-alt3);
  padding: 2px 6px;
  border-radius: 4px;
}

.timeline-card__summary {
  font-size: 14px;
  line-height: 1.65;
  color: var(--color-text-primary);
  margin: 0;
}

.timeline-card__thumb-wrap {
  align-self: flex-start;
  position: relative;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid var(--color-border);
  cursor: pointer;
  width: 128px;
  height: 176px;
}

.timeline-card__thumb-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform 0.2s ease;
}

.timeline-card__thumb-wrap:hover .timeline-card__thumb-img {
  transform: scale(1.05);
}

.timeline-card__thumb-hover {
  position: absolute;
  inset: 0;
  background: var(--synopsis-surface-gray-mid-alt7);
  color: var(--color-text-inverse);
  font-size: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  transition: opacity 0.2s ease;
}

.timeline-card__thumb-wrap:hover .timeline-card__thumb-hover {
  opacity: 1;
}

/* 大图预览内容（弹窗外壳由 BaseModal 提供） */
.thumbnail-modal-card {
  position: relative;
  max-width: 90vw;
  max-height: 90vh;
  background: var(--synopsis-surface-gray-mid-alt9);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: var(--synopsis-accent-gray);
}

.thumbnail-modal-card--zoomed {
  overflow: auto;
  max-width: 92vw;
  max-height: 82vh;
}

.thumbnail-modal-img {
  max-width: 100%;
  max-height: 85vh;
  object-fit: contain;
  display: block;
  cursor: zoom-in;
}

.thumbnail-modal-img--zoomed {
  max-width: none;
  max-height: none;
  width: auto;
  height: auto;
  cursor: zoom-out;
}

.thumbnail-modal-hint {
  margin: 8px 0 0;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
}
</style>
