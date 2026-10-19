<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useInsightStore } from '@/stores/insightStore'
import { useTaskCenterStore } from '@/stores/taskCenterStore'
import AppShell from '@/components/ui/AppShell.vue'
import MiyakoNavbar from '@/components/common/MiyakoNavbar.vue'
import BookSelector from '@/components/insight/BookSelector.vue'
import KnowledgeGraphPanel from '@/components/insight/KnowledgeGraphPanel.vue'
import EntityBrowserPanel from '@/components/insight/EntityBrowserPanel.vue'
import StorySynopsisPanel from '@/components/insight/StorySynopsisPanel.vue'
import ProductStatusBanner from '@/components/product/ProductStatusBanner.vue'
import UiButton from '@/components/ui/UiButton.vue'
import * as insightApi from '@/api/insight'
import { getBookDetail } from '@/api/bookshelf'
import { resolveAnalysisStatus } from '@/utils/insightStatus'
import type { BookData } from '@/types'

const route = useRoute()
const router = useRouter()
const insightStore = useInsightStore()
const taskCenterStore = useTaskCenterStore()

type GraphTabId = 'network' | 'entities' | 'synopsis'

const graphTabs: Array<{ id: GraphTabId; label: string; icon: string }> = [
  { id: 'network', label: '关系网络', icon: '🕸️' },
  { id: 'entities', label: '实体浏览', icon: '🏷️' },
  { id: 'synopsis', label: '故事梗概', icon: '📖' },
]

const activeTab = ref<GraphTabId>('network')

function handleViewEntity(_entityId: string) {
  activeTab.value = 'entities'
}

let bookLoadSequence = 0
let isGraphViewMounted = false

const loadedBookDetail = ref<{
  id: string
  title: string
  cover?: string
  totalPages: number
} | null>(null)

const currentBook = computed(() => {
  if (loadedBookDetail.value?.id === insightStore.currentBookId) return loadedBookDetail.value
  return null
})

const hasLoadedBook = computed(() => currentBook.value !== null)

function applyLoadedBook(
  book: BookData,
  chapters: Awaited<ReturnType<typeof insightApi.getInsightChapters>>,
): void {
  loadedBookDetail.value = {
    id: book.id,
    title: book.title,
    cover: book.cover,
    totalPages: book.totalPages ?? 0,
  }
  insightStore.setBookTotalPages(book.totalPages ?? 0)
  insightStore.setChapters(chapters)
}

function applyAnalysisStatus(
  response: Awaited<ReturnType<typeof insightApi.getAnalysisStatus>>,
): void {
  insightStore.setAnalyzedPagesCount(response.analyzedPagesCount)
  insightStore.setAnalysisStatus(resolveAnalysisStatus(response))

  if (response.currentTask) {
    insightStore.setCurrentTaskId(response.currentTask.jobId)
    insightStore.updateProgress(
      response.currentTask.progress.analyzedPages,
      response.currentTask.progress.totalPages,
    )
    return
  }

  insightStore.setCurrentTaskId(null)
  insightStore.updateProgress(0, 0)
}

function isCurrentBookLoad(loadId: number, bookId: string): boolean {
  return isGraphViewMounted && loadId === bookLoadSequence && insightStore.currentBookId === bookId
}

async function loadBook(bookId: string): Promise<void> {
  if (!bookId) return

  const sequence = ++bookLoadSequence
  insightStore.setCurrentBook(bookId)
  insightStore.setLoading(true)
  insightStore.setError(null)

  try {
    const [book, chapters, status] = await Promise.all([
      getBookDetail(bookId),
      insightApi.getInsightChapters(bookId),
      insightApi.getAnalysisStatus(bookId),
    ])

    if (!isCurrentBookLoad(sequence, bookId)) return

    applyLoadedBook(book, chapters)
    applyAnalysisStatus(status)
  } catch (error) {
    if (!isCurrentBookLoad(sequence, bookId)) return
    const message = error instanceof Error ? error.message : '书籍信息加载失败'
    insightStore.setError(message)
  } finally {
    if (isCurrentBookLoad(sequence, bookId)) {
      insightStore.setLoading(false)
    }
  }
}

function routeBookId(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function retryCurrentBook(): void {
  if (insightStore.currentBookId) void loadBook(insightStore.currentBookId)
}

function resetBookSelection(): void {
  bookLoadSequence += 1
  loadedBookDetail.value = null
  insightStore.setCurrentBook(null)
  insightStore.setLoading(false)
}

function chooseAnotherBook(): void {
  resetBookSelection()
  void router.replace({ query: {} })
}

function handleBookSelect(bookId: string): void {
  if (!bookId) {
    chooseAnotherBook()
    return
  }
  void router.replace({ query: { ...route.query, book: bookId } })
}

onMounted(() => {
  isGraphViewMounted = true
  const bookId = routeBookId(route.query.book)
  if (bookId) {
    void loadBook(bookId)
  } else {
    resetBookSelection()
  }
})

onUnmounted(() => {
  isGraphViewMounted = false
  bookLoadSequence += 1
  insightStore.setLoading(false)
})

watch(
  () => routeBookId(route.query.book),
  bookId => {
    if (!isGraphViewMounted) return
    if (!bookId) {
      resetBookSelection()
      return
    }
    if (bookId !== insightStore.currentBookId) void loadBook(bookId)
  },
)
</script>

<template>
  <AppShell class="graph-page" viewport-mode="page">
    <MiyakoNavbar :book-title="currentBook?.title" />

    <main class="graph-main">
      <!-- 顶部次级栏：漫画选择与子 Tab 切换 -->
      <section class="graph-header-bar">
        <div class="graph-header-bar__left">
          <BookSelector
            :model-value="insightStore.currentBookId ?? ''"
            @select="handleBookSelect"
          />
          <span v-if="currentBook" class="graph-header-bar__meta">
            共 {{ currentBook.totalPages }} 页 · 已分析 {{ insightStore.analyzedPageCount }} 页
          </span>
        </div>

        <div v-if="hasLoadedBook" class="graph-header-bar__tabs">
          <UiButton
            v-for="tab in graphTabs"
            :key="tab.id"
            variant="tab"
            class="graph-tab-btn"
            :class="{ 'graph-tab-btn--active': activeTab === tab.id }"
            @click="activeTab = tab.id"
          >
            <span class="graph-tab-btn__icon">{{ tab.icon }}</span>
            <span>{{ tab.label }}</span>
          </UiButton>
        </div>
      </section>

      <!-- 内容区状态 -->
      <section class="graph-content">
        <ProductStatusBanner
          v-if="insightStore.isLoading"
          class="graph-status-banner"
          tone="neutral"
          title="正在加载知识图谱"
          aria-live="polite"
        >
          正在读取实体对齐数据与全景摘要…
        </ProductStatusBanner>

        <ProductStatusBanner
          v-else-if="insightStore.error"
          class="graph-status-banner"
          tone="danger"
          title="数据加载失败"
          aria-live="assertive"
        >
          {{ insightStore.error }}
          <template #actions>
            <UiButton size="sm" variant="secondary" @click="retryCurrentBook">
              重试
            </UiButton>
            <UiButton size="sm" variant="ghost" @click="chooseAnotherBook">
              选择其他书籍
            </UiButton>
          </template>
        </ProductStatusBanner>

        <!-- 未选择书籍时的提示 -->
        <div v-else-if="!hasLoadedBook" class="graph-empty-container">
          <div class="graph-empty-card">
            <span class="graph-empty-icon" aria-hidden="true">🏷️</span>
            <h2 class="graph-empty-title">请选择一本漫画</h2>
            <p class="graph-empty-desc">
              在上方选择书籍即可查看由深度多模态分析生成的规范实体、别名对齐网络与全景世界观。
            </p>
          </div>
        </div>

        <!-- 正常内容展示 -->
        <div v-else class="graph-panel-wrapper">
          <div v-show="activeTab === 'network'" class="graph-panel-pane">
            <KnowledgeGraphPanel />
          </div>

          <div v-show="activeTab === 'entities'" class="graph-panel-pane">
            <EntityBrowserPanel />
          </div>

          <div v-show="activeTab === 'synopsis'" class="graph-panel-pane">
            <StorySynopsisPanel @view-entity="handleViewEntity" />
          </div>
        </div>
      </section>
    </main>
  </AppShell>
</template>

<style scoped>
.graph-page {
  --graph-shadow-bar: 0 1px 3px rgba(0, 0, 0, 0.03);
  --graph-shadow-tab-active: 0 1px 3px rgba(0, 0, 0, 0.08);
  --graph-shadow-card: 0 2px 8px rgba(0, 0, 0, 0.04);

  background-color: var(--color-bg-base);
  color: var(--color-text-primary);
  display: flex;
  flex-direction: column;
}

.graph-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  max-width: 1680px;
  width: 100%;
  margin: 0 auto;
  padding: 16px 24px 32px;
  gap: 16px;
}

.graph-header-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 12px 20px;
  box-shadow: var(--graph-shadow-bar);
}

.graph-header-bar__left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.graph-header-bar__meta {
  font-size: 0.88rem;
  color: var(--color-text-secondary);
}

.graph-header-bar__tabs {
  display: flex;
  align-items: center;
  gap: 6px;
  background-color: var(--color-surface-secondary);
  padding: 4px;
  border-radius: 8px;
  border: 1px solid var(--color-border);
}

.graph-tab-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 6px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 0.88rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
}

.graph-tab-btn:hover {
  color: var(--color-text-primary);
}

.graph-tab-btn--active,
.graph-tab-btn--active:hover {
  background-color: var(--color-surface);
  color: var(--color-brand-primary);
  font-weight: 600;
  box-shadow: var(--graph-shadow-tab-active);
}

.graph-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.graph-status-banner {
  margin-bottom: 16px;
}

.graph-empty-container {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 64px 20px;
}

.graph-empty-card {
  text-align: center;
  max-width: 440px;
  padding: 40px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 16px;
  box-shadow: var(--graph-shadow-card);
}

.graph-empty-icon {
  font-size: 3rem;
  margin-bottom: 16px;
  display: inline-block;
}

.graph-empty-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin-bottom: 8px;
}

.graph-empty-desc {
  font-size: 0.92rem;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.graph-panel-wrapper {
  flex: 1;
  display: flex;
  flex-direction: column;
}

.graph-panel-pane {
  flex: 1;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 20px;
  box-shadow: var(--graph-shadow-bar);
}
</style>
