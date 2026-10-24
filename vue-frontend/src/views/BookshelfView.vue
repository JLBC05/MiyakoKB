<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBookshelfStore } from '@/stores/bookshelfStore'
import { getV2ServerInfo } from '@/api/v2/system'
import { createBooksExportJob } from '@/api/bookshelf'
import { startAnalysis } from '@/api/insight'
import { getBooksKnowledgeStatus } from '@/api/v2/insight'
import type { BookKnowledgeStatus } from '@/api/v2/insight'
import BookCard from '@/components/bookshelf/BookCard.vue'
import BookSearch from '@/components/bookshelf/BookSearch.vue'
import BookModal from '@/components/bookshelf/BookModal.vue'
import MangaImportModal from '@/components/bookshelf/MangaImportModal.vue'
import MiyakoNavbar from '@/components/common/MiyakoNavbar.vue'
import BookDetailModal from '@/components/bookshelf/BookDetailModal.vue'
import PublicTrialNotice from '@/components/common/PublicTrialNotice.vue'
import ProductActionRow from '@/components/product/ProductActionRow.vue'
import ProductCardGrid from '@/components/product/ProductCardGrid.vue'
import ProductEmptyState from '@/components/product/ProductEmptyState.vue'
import ProductStatusBanner from '@/components/product/ProductStatusBanner.vue'
import AppShell from '@/components/ui/AppShell.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import UiSelect from '@/components/ui/UiSelect.vue'
import UiSpinner from '@/components/ui/UiSpinner.vue'
import type { UiSelectValue } from '@/components/ui/selectTypes'
import UiCheckbox from '@/components/ui/UiCheckbox.vue'
import BaseModal from '@/components/common/BaseModal.vue'
import { showToast, useToast } from '@/utils/toast'
import { useTaskCenterStore } from '@/stores/taskCenterStore'
import { useRuntimeStore } from '@/stores/runtimeStore'
import { useSettingsStore } from '@/stores/settings'
import { confirmProductAction } from '@/composables/useProductConfirm'
import { usePublicUserAccess } from '@/composables/usePublicUserAccess'
import { triggerUrlDownload, withDownloadFileName } from '@/utils/browserDownload'

const router = useRouter()
const route = useRoute()
const bookshelfStore = useBookshelfStore()
const taskCenterStore = useTaskCenterStore()
const runtimeStore = useRuntimeStore()
const settingsStore = useSettingsStore()
const publicAccess = usePublicUserAccess()
const toast = useToast()

const PUBLIC_TRIAL_NOTICE_DISMISSED_KEY = 'saber_public_trial_notice_dismissed'

const lanUrl = ref<string | null>(null)
const isLocalProfile = computed(() => runtimeStore.capabilities?.profile === 'local')
const isPublicProfile = computed(() => runtimeStore.capabilities?.profile === 'public')
const publicTrialNoticeDismissed = ref(
  window.localStorage.getItem(PUBLIC_TRIAL_NOTICE_DISMISSED_KEY) === 'true',
)
const showPublicTrialNotice = computed(
  () => isPublicProfile.value && !publicTrialNoticeDismissed.value,
)
const canUseInsight = computed(() => publicAccess.featureAllowed('insight'))
const disabledFeatureMessage = computed(() => {
  const key = route.query.disabledFeature
  if (key === 'insight') return '管理员已关闭漫画分析功能。'
  return ''
})

const showBookModal = ref(false)
const showImportModal = ref(false)
const showDetailModal = ref(false)
const editingBookId = ref<string | null>(null)
const showBatchTagsModal = ref(false)
const batchTagAction = ref<'add' | 'remove'>('add')
const selectedBatchTagNames = ref(new Set<string>())
const batchBusy = ref(false)

const knowledgeStatusMap = ref<Record<string, BookKnowledgeStatus>>({})

async function refreshKnowledgeStatus() {
  try {
    const res = await getBooksKnowledgeStatus()
    knowledgeStatusMap.value = res.items || {}
  } catch (err) {
    console.error('获取知识库构建状态失败:', err)
  }
}

const filteredBooks = computed(() => bookshelfStore.books)
const allTags = computed(() => bookshelfStore.tags)
const isEmpty = computed(() => (
  filteredBooks.value.length === 0
  && !bookshelfStore.isLoading
  && !bookshelfStore.error
  && !bookshelfStore.searchQuery
  && bookshelfStore.selectedTagNames.length === 0
))
const selectedBookCount = computed(() => bookshelfStore.selectedBookIds.size)
const hasSelectedPages = computed(() => bookshelfStore.books.some(book => (
  bookshelfStore.selectedBookIds.has(book.id)
  && (book.totalPages ?? book.chapters?.reduce(
    (total, chapter) => total + (chapter.imageCount ?? 0),
    0,
  ) ?? 0) > 0
)))
const sortValue = computed(() => `${bookshelfStore.sortBy}:${bookshelfStore.sortOrder}`)
const sortOptions = [
  { label: '更新时间（新→旧）', value: 'updatedAt:desc' },
  { label: '更新时间（旧→新）', value: 'updatedAt:asc' },
  { label: '创建时间（新→旧）', value: 'createdAt:desc' },
  { label: '创建时间（旧→新）', value: 'createdAt:asc' },
  { label: '标题（升序）', value: 'title:asc' },
  { label: '标题（降序）', value: 'title:desc' },
]

function handlePageShow(event: PageTransitionEvent) {
  if (event.persisted) {
    bookshelfStore.loadBooks()
    bookshelfStore.loadTags()
    refreshKnowledgeStatus()
    if (showDetailModal.value && bookshelfStore.currentBook) {
      openBookDetail(bookshelfStore.currentBook.id)
    }
  }
}

async function loadServerInfo(): Promise<void> {
  try {
    const response = await getV2ServerInfo()
    lanUrl.value = response.lanUrl
  } catch {
    lanUrl.value = null
  }
}

onMounted(async () => {
  window.addEventListener('pageshow', handlePageShow)

  const startupRequests: Promise<unknown>[] = [
    bookshelfStore.loadBooks(),
    bookshelfStore.loadTags(),
    refreshKnowledgeStatus(),
  ]
  if (isLocalProfile.value) startupRequests.push(loadServerInfo())
  await Promise.all(startupRequests)
})

onUnmounted(() => {
  window.removeEventListener('pageshow', handlePageShow)
})

function dismissPublicTrialNotice(): void {
  publicTrialNoticeDismissed.value = true
  window.localStorage.setItem(PUBLIC_TRIAL_NOTICE_DISMISSED_KEY, 'true')
}

function openEditBookModal(bookId: string) {
  editingBookId.value = bookId
  showBookModal.value = true
}

function closeBookDetail() {
  showDetailModal.value = false
}

function openBookDetail(bookId: string) {
  bookshelfStore.setCurrentBook(bookId)
  void router.push({ name: 'book-detail', params: { bookId } })
}

function setSort(value: UiSelectValue) {
  const [by, order] = String(value).split(':') as [
    'title' | 'createdAt' | 'updatedAt',
    'asc' | 'desc',
  ]
  bookshelfStore.setSort(by, order)
}

async function downloadSelectedBooks() {
  const bookIds = [...bookshelfStore.selectedBookIds]
  if (!bookIds.length || batchBusy.value) return
  batchBusy.value = true
  let queuedToastId: number | null = null
  try {
    const accepted = await createBooksExportJob(
      bookIds,
      settingsStore.exportPreferences.preserveOriginalFilenames,
    )
    const jobId = accepted.jobIds[0]
    if (!jobId) throw new Error('后端没有返回批量导出任务')
    taskCenterStore.trackJob(jobId)
    taskCenterStore.open({ batchId: accepted.batchId })
    queuedToastId = showToast('批量下载已进入后端队列，可安全离开书架页面', 'info', 0)
    const job = await taskCenterStore.waitForJob(jobId)
    const artifact = job.artifacts[0]
    if (!artifact) throw new Error('批量导出任务未生成可下载文件')
    triggerUrlDownload(
      withDownloadFileName(artifact.url, `books-${bookIds.length}-export.zip`),
    )
    showToast('批量导出完成，下载已开始', 'success')
  } catch (error) {
    showToast(error instanceof Error ? error.message : '批量下载失败', 'error')
  } finally {
    if (queuedToastId !== null) {
      toast.removeToast(queuedToastId)
    }
    batchBusy.value = false
  }
}

async function deleteSelectedBooks() {
  const bookIds = [...bookshelfStore.selectedBookIds]
  if (!bookIds.length || batchBusy.value) return
  const confirmed = await confirmProductAction({
    title: '批量删除书籍',
    message: `确定永久删除选中的 ${bookIds.length} 本书籍吗？存在活动任务的书籍会被保留。`,
    confirmText: '批量删除',
    tone: 'danger',
  })
  if (!confirmed) return
  batchBusy.value = true
  try {
    const result = await bookshelfStore.batchDeleteBooksApi(bookIds)
    showToast(
      result.rejected.length
        ? `已删除 ${result.deleted.length} 本，${result.rejected.length} 本因任务或导入被保留`
        : `已删除 ${result.deleted.length} 本书籍`,
      result.rejected.length ? 'warning' : 'success',
    )
    if (result.rejected.length) {
      taskCenterStore.open({ bookId: result.rejected[0]?.bookId })
    }
  } catch (error) {
    showToast(error instanceof Error ? error.message : '批量删除失败', 'error')
  } finally {
    batchBusy.value = false
  }
}

async function handleImportedSuccess() {
  await bookshelfStore.loadBooks()
  await refreshKnowledgeStatus()
}

async function handleBuildKnowledge(bookId: string, title: string) {
  try {
    const res = await startAnalysis(bookId, { mode: 'full' })
    taskCenterStore.trackJob(res.jobId)
    taskCenterStore.open()
    showToast(`已提交《${title}》知识库构建任务`, 'success')
  } catch (err) {
    showToast(err instanceof Error ? err.message : '启动构建任务失败', 'error')
  }
}

async function handleBatchBuildKnowledge() {
  const bookIds = [...bookshelfStore.selectedBookIds]
  if (!bookIds.length || batchBusy.value) return
  batchBusy.value = true
  try {
    let count = 0
    for (const id of bookIds) {
      try {
        const res = await startAnalysis(id, { mode: 'full' })
        taskCenterStore.trackJob(res.jobId)
        count++
      } catch (e) {
        console.error(`书籍 ${id} 启动构建失败:`, e)
      }
    }
    taskCenterStore.open()
    showToast(`已成功为 ${count} 本书籍提交知识库构建任务`, 'success')
  } catch (err) {
    showToast(err instanceof Error ? err.message : '批量构建任务失败', 'error')
  } finally {
    batchBusy.value = false
  }
}

function openBatchTags(action: 'add' | 'remove') {
  batchTagAction.value = action
  selectedBatchTagNames.value = new Set()
  showBatchTagsModal.value = true
}

function toggleBatchTag(tagName: string, selected: boolean) {
  const next = new Set(selectedBatchTagNames.value)
  if (selected) next.add(tagName)
  else next.delete(tagName)
  selectedBatchTagNames.value = next
}

async function applyBatchTags() {
  const bookIds = [...bookshelfStore.selectedBookIds]
  const tagNames = [...selectedBatchTagNames.value]
  if (!bookIds.length || !tagNames.length || batchBusy.value) return
  batchBusy.value = true
  try {
    const updated = await bookshelfStore.batchUpdateTagsApi(
      bookIds,
      tagNames,
      batchTagAction.value,
    )
    showToast(`已更新 ${updated} 本书籍的标签`, 'success')
    showBatchTagsModal.value = false
  } catch (error) {
    showToast(error instanceof Error ? error.message : '批量更新标签失败', 'error')
  } finally {
    batchBusy.value = false
  }
}
</script>

<template>
  <AppShell class="bookshelf-page">
    <MiyakoNavbar />

    <main class="bookshelf-main">
      <ProductStatusBanner
        v-if="disabledFeatureMessage"
        tone="neutral"
        role="status"
      >
        {{ disabledFeatureMessage }}
      </ProductStatusBanner>
      <PublicTrialNotice
        v-if="showPublicTrialNotice"
        class="bookshelf-trial-notice"
        dismissible
        @dismiss="dismissPublicTrialNotice"
      />
      <div class="bookshelf-toolbar">
        <h1 class="bookshelf-toolbar__title">我的书架</h1>
        <ProductActionRow
          class="bookshelf-toolbar__actions"
          aria-label="书架主要操作"
          justify="end"
        >
          <UiButton variant="primary" @click="showImportModal = true">
            <UiIcon name="download" size="16" />
            <span>导入漫画</span>
          </UiButton>
          <UiButton
            :variant="bookshelfStore.batchMode ? 'primary' : 'secondary'"
            @click="bookshelfStore.batchMode ? bookshelfStore.exitBatchMode() : bookshelfStore.enterBatchMode()"
          >
            <UiIcon name="check" size="16" />
            <span>{{ bookshelfStore.batchMode ? '退出批量管理' : '批量管理' }}</span>
          </UiButton>
        </ProductActionRow>
      </div>

      <div class="bookshelf-query-bar">
        <BookSearch
          :tags="allTags"
          :query="bookshelfStore.searchQuery"
          :selected-tag-names="bookshelfStore.selectedTagNames"
          @search="bookshelfStore.setSearchQuery"
          @filter-tag="bookshelfStore.toggleTagFilter"
        />
        <UiSelect
          class="bookshelf-query-bar__sort"
          aria-label="书架排序"
          :model-value="sortValue"
          :options="sortOptions"
          @change="setSort"
        />
      </div>

      <ProductStatusBanner
        v-if="bookshelfStore.error"
        class="bookshelf-load-status"
        tone="danger"
        title="书架加载失败"
        role="alert"
      >
        {{ bookshelfStore.error }}
        <template #actions>
          <UiButton size="sm" variant="secondary" @click="bookshelfStore.loadBooks()">
            重试
          </UiButton>
        </template>
      </ProductStatusBanner>

      <ProductStatusBanner
        v-if="bookshelfStore.tagsError"
        class="bookshelf-load-status"
        tone="warning"
        title="标签加载失败"
        role="alert"
      >
        {{ bookshelfStore.tagsError }}
        <template #actions>
          <UiButton size="sm" variant="secondary" @click="bookshelfStore.loadTags()">
            重试
          </UiButton>
        </template>
      </ProductStatusBanner>

      <ProductActionRow
        v-if="bookshelfStore.batchMode"
        class="bookshelf-batch-bar"
        aria-label="书架批量操作"
        justify="start"
      >
        <UiButton variant="secondary" @click="bookshelfStore.toggleSelectAll">
          {{ bookshelfStore.isAllSelected ? '取消全选' : '全选当前书籍' }}
        </UiButton>
        <span class="bookshelf-batch-bar__count">已选择 {{ selectedBookCount }} 本</span>
        <UiButton
          variant="secondary"
          :disabled="!hasSelectedPages || batchBusy"
          @click="downloadSelectedBooks"
        >
          下载选中书籍
        </UiButton>
        <UiButton
          variant="primary"
          :disabled="selectedBookCount === 0 || batchBusy"
          @click="handleBatchBuildKnowledge"
        >
          ⚡ 批量生成知识库
        </UiButton>
        <UiButton
          variant="secondary"
          :disabled="selectedBookCount === 0 || batchBusy"
          @click="openBatchTags('add')"
        >
          添加标签
        </UiButton>
        <UiButton
          variant="secondary"
          :disabled="selectedBookCount === 0 || batchBusy"
          @click="openBatchTags('remove')"
        >
          移除标签
        </UiButton>
        <UiButton
          variant="danger"
          :disabled="selectedBookCount === 0 || batchBusy"
          @click="deleteSelectedBooks"
        >
          删除选中书籍
        </UiButton>
      </ProductActionRow>

      <div class="bookshelf-main__books">
        <ProductStatusBanner
          v-if="bookshelfStore.isLoading && filteredBooks.length === 0"
          class="bookshelf-loading-state"
          tone="neutral"
          title="正在加载书架"
          role="status"
          aria-live="polite"
        >
          <template #icon>
            <UiSpinner size="18" />
          </template>
          请稍候…
        </ProductStatusBanner>

        <ProductCardGrid
          v-else-if="filteredBooks.length > 0"
          aria-label="书籍列表"
          gap="24px"
          min-item-width="160px"
        >
          <BookCard
            v-for="book in filteredBooks"
            :key="book.id"
            :book="book"
            :tags="allTags"
            :selectable="bookshelfStore.batchMode"
            :selected="bookshelfStore.selectedBookIds.has(book.id)"
            :knowledge-status="knowledgeStatusMap[book.id]"
            @click="openBookDetail(book.id)"
            @select="bookshelfStore.toggleBookSelection(book.id)"
            @build-knowledge="handleBuildKnowledge(book.id, book.title)"
          />
        </ProductCardGrid>

        <ProductEmptyState
          v-else-if="isEmpty"
          class="bookshelf-main__empty-state"
          title="书架空空如也"
          description="点击&quot;导入漫画&quot;开始构建你的漫画知识库"
        >
          <template #icon>📚</template>
          <template #actions>
            <UiButton variant="primary" @click="showImportModal = true">
              <UiIcon name="download" size="16" />
              <span>导入第一本漫画</span>
            </UiButton>
          </template>
        </ProductEmptyState>

        <ProductEmptyState
          v-else-if="!bookshelfStore.error"
          class="bookshelf-main__empty-state"
          title="未找到匹配的书籍"
          description="尝试调整搜索条件或标签筛选"
        >
          <template #icon>🔍</template>
        </ProductEmptyState>
      </div>
    </main>

    <BookModal
      v-if="showBookModal"
      :book-id="editingBookId"
      @close="showBookModal = false"
      @saved="showBookModal = false"
    />

    <MangaImportModal
      v-if="showImportModal"
      @close="showImportModal = false"
      @imported="handleImportedSuccess"
    />

    <BookDetailModal
      v-if="showDetailModal"
      :insight-allowed="canUseInsight"
      @close="closeBookDetail"
      @edit="openEditBookModal"
    />

    <BaseModal
      v-model="showBatchTagsModal"
      :title="batchTagAction === 'add' ? '批量添加标签' : '批量移除标签'"
      size="small"
      :close-on-overlay="true"
      :close-on-esc="true"
    >
      <div class="bookshelf-batch-tags">
        <UiCheckbox
          v-for="tag in allTags"
          :key="tag.id"
          :model-value="selectedBatchTagNames.has(tag.name)"
          :label="tag.name"
          @change="toggleBatchTag(tag.name, $event)"
        />
        <p v-if="!allTags.length">还没有可用标签，请先在“管理标签”中创建。</p>
      </div>
      <template #footer>
        <ProductActionRow aria-label="批量标签操作" variant="dialog">
          <UiButton variant="secondary" @click="showBatchTagsModal = false">取消</UiButton>
          <UiButton
            variant="primary"
            :disabled="selectedBatchTagNames.size === 0 || batchBusy"
            @click="applyBatchTags"
          >
            应用
          </UiButton>
        </ProductActionRow>
      </template>
    </BaseModal>
  </AppShell>
</template>

<style scoped>
.bookshelf-main {
  max-width: 1400px;
  min-height: 0;
  margin: 0 auto;
  padding: 24px;
}

.bookshelf-trial-notice {
  margin-bottom: 24px;
}

.bookshelf-page {
  padding-inline: 20px;
}

.bookshelf-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 32px;
}

.bookshelf-toolbar__title {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 0;
  color: var(--color-text-default);
  font-weight: 700;
  font-size: 1.8rem;
}

.bookshelf-toolbar__title::before {
  content: '📚';
  font-size: 1.5rem;
}

.bookshelf-toolbar__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.bookshelf-main__books {
  min-height: 400px;
}

.bookshelf-load-status {
  margin: 16px 0;
}

.bookshelf-loading-state {
  --product-status-banner-align-items: center;
  --product-status-banner-justify-content: center;
  --product-status-banner-min-height: 240px;
  --product-status-banner-border: 0;
  --product-status-banner-background: transparent;
}

.bookshelf-main__empty-state {
  --product-empty-state-min-height: 0;
  --product-empty-state-padding: 80px 20px;
  --product-empty-state-icon-width: auto;
  --product-empty-state-icon-height: auto;
  --product-empty-state-icon-margin-bottom: 16px;
  --product-empty-state-icon-border: 0;
  --product-empty-state-icon-radius: 0;
  --product-empty-state-icon-background: transparent;
  --product-empty-state-icon-color: inherit;
  --product-empty-state-icon-font-size: 4rem;
  --product-empty-state-title-font-size: 1.5rem;
  --product-empty-state-title-margin: 0 0 8px;
  --product-empty-state-description-margin: 0 0 24px;
}

.bookshelf-query-bar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(210px, 280px);
  gap: 16px;
  align-items: start;
}

.bookshelf-query-bar__sort {
  width: 100%;
}

.bookshelf-batch-bar {
  position: sticky;
  top: 12px;
  z-index: var(--z-sticky);
  flex-wrap: wrap;
  margin: 16px 0;
  padding: 12px;
  background: var(--color-surface-card);
  border: 1px solid var(--color-border-default);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-soft);
}

.bookshelf-batch-bar__count {
  color: var(--color-text-supporting);
}

.bookshelf-batch-tags {
  display: grid;
  gap: 12px;
}

@media (--breakpoint-md-down) {
  .bookshelf-query-bar {
    grid-template-columns: 1fr;
  }
}

@media (--breakpoint-sm-down) {
  .bookshelf-toolbar__actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    width: 100%;
  }
}
</style>
