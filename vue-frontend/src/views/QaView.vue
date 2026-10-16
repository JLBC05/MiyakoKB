<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useInsightStore } from '@/stores/insightStore'
import AppShell from '@/components/ui/AppShell.vue'
import MiyakoNavbar from '@/components/common/MiyakoNavbar.vue'
import BookSelector from '@/components/insight/BookSelector.vue'
import QAPanel from '@/components/insight/QAPanel.vue'
import ProductStatusBanner from '@/components/product/ProductStatusBanner.vue'
import UiButton from '@/components/ui/UiButton.vue'
import * as insightApi from '@/api/insight'
import { getBookDetail } from '@/api/bookshelf'
import { resolveAnalysisStatus } from '@/utils/insightStatus'
import type { BookData } from '@/types'

const route = useRoute()
const router = useRouter()
const insightStore = useInsightStore()

let bookLoadSequence = 0
let isQaViewMounted = false

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
  return isQaViewMounted && loadId === bookLoadSequence && insightStore.currentBookId === bookId
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

onMounted(() => {
  isQaViewMounted = true
  const bookId = routeBookId(route.query.book)
  if (bookId) {
    void loadBook(bookId)
  } else {
    resetBookSelection()
  }
})

onUnmounted(() => {
  isQaViewMounted = false
  bookLoadSequence += 1
  insightStore.setLoading(false)
})

watch(
  () => routeBookId(route.query.book),
  bookId => {
    if (!isQaViewMounted) return
    if (!bookId) {
      resetBookSelection()
      return
    }
    if (bookId !== insightStore.currentBookId) void loadBook(bookId)
  },
)
</script>

<template>
  <AppShell class="qa-page" viewport-mode="page">
    <MiyakoNavbar :book-title="currentBook?.title" />

    <main class="qa-main">
      <!-- 顶部次级栏：漫画选择与上下文概览 -->
      <section class="qa-header-bar">
        <div class="qa-header-bar__left">
          <BookSelector />
          <span v-if="currentBook" class="qa-header-bar__meta">
            共 {{ currentBook.totalPages }} 页 · 已分析 {{ insightStore.analyzedPageCount }} 页
          </span>
        </div>
      </section>

      <!-- 内容区状态与问答面板 -->
      <section class="qa-content">
        <ProductStatusBanner
          v-if="insightStore.isLoading"
          class="qa-status-banner"
          tone="neutral"
          title="正在加载问答上下文"
          aria-live="polite"
        >
          正在连接书籍索引与向量检索服务…
        </ProductStatusBanner>

        <ProductStatusBanner
          v-else-if="insightStore.error"
          class="qa-status-banner"
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
        <div v-else-if="!hasLoadedBook" class="qa-empty-container">
          <div class="qa-empty-card">
            <span class="qa-empty-icon" aria-hidden="true">💬</span>
            <h2 class="qa-empty-title">请选择一本漫画</h2>
            <p class="qa-empty-desc">
              在上方选择书籍即可开启剧情考证与智能问答。系统将基于向量索引和实体图谱进行精确溯源检索。
            </p>
          </div>
        </div>

        <!-- 问答面板 -->
        <div v-else class="qa-panel-wrapper">
          <QAPanel />
        </div>
      </section>
    </main>
  </AppShell>
</template>

<style scoped>
.qa-page {
  min-height: 100vh;
  background-color: var(--color-bg-base);
  color: var(--color-text-primary);
  display: flex;
  flex-direction: column;
}

.qa-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  max-width: 1440px;
  width: 100%;
  margin: 0 auto;
  padding: 16px 24px 32px;
  gap: 16px;
}

.qa-header-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 12px 20px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
}

.qa-header-bar__left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.qa-header-bar__meta {
  font-size: 0.88rem;
  color: var(--color-text-secondary);
}

.qa-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.qa-status-banner {
  margin-bottom: 16px;
}

.qa-empty-container {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 64px 20px;
}

.qa-empty-card {
  text-align: center;
  max-width: 440px;
  padding: 40px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 16px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.qa-empty-icon {
  font-size: 3rem;
  margin-bottom: 16px;
  display: inline-block;
}

.qa-empty-title {
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--color-text-primary);
  margin-bottom: 8px;
}

.qa-empty-desc {
  font-size: 0.92rem;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.qa-panel-wrapper {
  flex: 1;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 24px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
  display: flex;
  flex-direction: column;
  min-height: 700px;
}
</style>
