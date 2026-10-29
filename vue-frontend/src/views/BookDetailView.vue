<script setup lang="ts">
import { computed, ref, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppShell from '@/components/ui/AppShell.vue'
import MiyakoNavbar from '@/components/common/MiyakoNavbar.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import ProductActionRow from '@/components/product/ProductActionRow.vue'
import ProductStatusBanner from '@/components/product/ProductStatusBanner.vue'
import ProductChipList from '@/components/product/ProductChipList.vue'
import BaseModal from '@/components/common/BaseModal.vue'

import ChapterList from '@/components/bookshelf/book-detail/ChapterList.vue'
import ChapterFormContent from '@/components/bookshelf/book-detail/ChapterFormContent.vue'
import BookDeleteConfirmContent from '@/components/bookshelf/book-detail/BookDeleteConfirmContent.vue'

import { useBookshelfStore } from '@/stores/bookshelfStore'
import { useTaskCenterStore } from '@/stores/taskCenterStore'
import { getBookDetail } from '@/api/bookshelf'
import { getInsightChapters, rebuildKnowledgeBase, startAnalysis } from '@/api/insight'
import { showToast } from '@/utils/toast'
import type { BookData, ChapterData } from '@/types'

const route = useRoute()
const router = useRouter()
const bookshelfStore = useBookshelfStore()
const taskCenterStore = useTaskCenterStore()

const bookId = computed(() => (route.params.bookId as string) || (route.query.book as string) || '')

const loadedBook = ref<BookData | null>(null)
const chapters = ref<ChapterData[]>([])
const isLoading = ref(true)
const loadError = ref<string | null>(null)

const selectedChapterIds = ref<Set<string>>(new Set())
const analyzedChapterIds = ref<Set<string>>(new Set())
const isBuildingSelected = ref(false)
const showRebuildConfirm = ref(false)
const isRebuilding = ref(false)
const analyzedChaptersCount = computed(() => analyzedChapterIds.value.size)

let isMounted = false
let bookLoadSeq = 0

// 章节管理状态
const showChapterModal = ref(false)
const editingChapterId = ref<string | null>(null)
const chapterTitle = ref('')
const isChapterSaving = ref(false)
const showDeleteConfirm = ref(false)
const deleteTarget = ref<'book' | 'chapter'>('chapter')
const deleteChapterId = ref<string | null>(null)
const isDeleting = ref(false)
const draggedChapterIndex = ref<number | null>(null)
const dragOverChapterIndex = ref<number | null>(null)

const tagItems = computed(() => loadedBook.value?.tags?.map(tag => ({
  id: tag,
  label: tag,
  tone: 'primary' as const,
})) ?? [])

async function loadBookData(targetId: string): Promise<void> {
  if (!targetId) return
  const currentSeq = ++bookLoadSeq
  isLoading.value = true
  loadError.value = null

  try {
    const book = await getBookDetail(targetId)
    if (!isMounted || currentSeq !== bookLoadSeq) return

    loadedBook.value = book
    chapters.value = (book.chapters as ChapterData[]) || []
    bookshelfStore.updateBook(targetId, book)

    try {
      const insightChapters = await getInsightChapters(targetId)
      if (isMounted && currentSeq === bookLoadSeq) {
        analyzedChapterIds.value = new Set(
          insightChapters.filter(ch => ch.analyzed).map(ch => ch.id)
        )
      }
    } catch {
      if (isMounted && currentSeq === bookLoadSeq) {
        analyzedChapterIds.value = new Set()
      }
    }
  } catch (err) {
    if (isMounted && currentSeq === bookLoadSeq) {
      loadError.value = err instanceof Error ? err.message : '加载书籍详情失败'
    }
  } finally {
    if (isMounted && currentSeq === bookLoadSeq) {
      isLoading.value = false
    }
  }
}

// 章节选择与知识库构建
function handleSelectChapter(chapterId: string, selected: boolean): void {
  const next = new Set(selectedChapterIds.value)
  if (selected) {
    next.add(chapterId)
  } else {
    next.delete(chapterId)
  }
  selectedChapterIds.value = next
}

function handleSelectAllChapters(chapterIds: string[]): void {
  selectedChapterIds.value = new Set(chapterIds)
}

async function handleBuildSelectedChapters(): Promise<void> {
  if (!bookId.value || selectedChapterIds.value.size === 0) return
  isBuildingSelected.value = true
  try {
    const res = await startAnalysis(bookId.value, {
      mode: 'chapters',
      chapters: Array.from(selectedChapterIds.value),
    })
    taskCenterStore.trackJob(res.jobId)
    taskCenterStore.open()
    showToast('已提交章节知识库构建任务', 'success')
  } catch (err) {
    showToast(err instanceof Error ? err.message : '提交构建任务失败', 'error')
  } finally {
    isBuildingSelected.value = false
  }
}

async function handleRebuildKnowledge(): Promise<void> {
  if (!bookId.value) return
  isRebuilding.value = true
  try {
    const res = await rebuildKnowledgeBase(bookId.value)
    taskCenterStore.trackJob(res.jobId)
    taskCenterStore.open()
    showRebuildConfirm.value = false
    showToast('已提交知识库重建任务（不重跑页分析）', 'success')
  } catch (err) {
    showToast(err instanceof Error ? err.message : '提交重建任务失败', 'error')
  } finally {
    isRebuilding.value = false
  }
}

// 导航快捷入口
function goToGraph(): void {
  if (bookId.value) {
    void router.push({ path: '/graph', query: { book: bookId.value } })
  }
}

function goToQa(): void {
  if (bookId.value) {
    void router.push({ path: '/qa', query: { book: bookId.value } })
  }
}

function goToReader(chapterId?: string): void {
  const targetChapter = chapterId || chapters.value[0]?.id
  if (bookId.value && targetChapter) {
    void router.push({
      path: '/reader',
      query: { book: bookId.value, chapter: targetChapter },
    })
  } else {
    showToast('暂无可用章节进行阅读', 'warning')
  }
}

// 章节管理操作
function openCreateChapterModal(): void {
  editingChapterId.value = null
  chapterTitle.value = ''
  showChapterModal.value = true
}

function openEditChapterModal(id: string): void {
  const chapter = chapters.value.find(c => c.id === id)
  if (chapter) {
    editingChapterId.value = id
    chapterTitle.value = chapter.title
    showChapterModal.value = true
  }
}

async function saveChapter(): Promise<void> {
  if (!chapterTitle.value.trim() || !bookId.value) return
  isChapterSaving.value = true
  try {
    if (editingChapterId.value) {
      await bookshelfStore.updateChapterApi(bookId.value, editingChapterId.value, chapterTitle.value.trim())
      showToast('章节已更新', 'success')
    } else {
      await bookshelfStore.createChapterApi(bookId.value, chapterTitle.value.trim())
      showToast('章节创建成功', 'success')
    }
    showChapterModal.value = false
    await loadBookData(bookId.value)
  } catch (err) {
    showToast(err instanceof Error ? err.message : '保存章节失败', 'error')
  } finally {
    isChapterSaving.value = false
  }
}

function deleteChapter(id: string): void {
  deleteTarget.value = 'chapter'
  deleteChapterId.value = id
  showDeleteConfirm.value = true
}

async function handleConfirmDelete(): Promise<void> {
  if (!bookId.value) return
  isDeleting.value = true
  try {
    if (deleteTarget.value === 'chapter' && deleteChapterId.value) {
      await bookshelfStore.deleteChapterApi(bookId.value, deleteChapterId.value)
      showToast('章节已删除', 'success')
      showDeleteConfirm.value = false
      await loadBookData(bookId.value)
    } else if (deleteTarget.value === 'book') {
      await bookshelfStore.deleteBookApi(bookId.value)
      showToast('书籍已删除', 'success')
      showDeleteConfirm.value = false
      void router.push('/')
    }
  } catch (err) {
    showToast(err instanceof Error ? err.message : '删除失败', 'error')
  } finally {
    isDeleting.value = false
  }
}

onMounted(() => {
  isMounted = true
  if (bookId.value) {
    void loadBookData(bookId.value)
  }
})

watch(bookId, newId => {
  selectedChapterIds.value = new Set()
  if (newId && isMounted) {
    void loadBookData(newId)
  }
})
</script>

<template>
  <AppShell class="book-detail-page" viewport-mode="page">
    <MiyakoNavbar :book-title="loadedBook?.title" />

    <main class="book-detail-main">
      <ProductStatusBanner
        v-if="isLoading"
        class="book-detail-banner"
        tone="neutral"
        title="正在加载书籍"
      >
        正在读取书籍数据…
      </ProductStatusBanner>

      <ProductStatusBanner
        v-else-if="loadError"
        class="book-detail-banner"
        tone="danger"
        title="加载失败"
      >
        {{ loadError }}
        <template #actions>
          <UiButton size="sm" variant="secondary" @click="loadBookData(bookId)">重试</UiButton>
          <UiButton size="sm" variant="ghost" @click="router.push('/')">返回书架</UiButton>
        </template>
      </ProductStatusBanner>

      <div v-else-if="loadedBook" class="book-detail-content">
        <!-- 顶部书籍信息卡片与快捷入口 -->
        <header class="book-header-card">
          <div class="book-header-card__cover-box">
            <img
              v-if="loadedBook.cover"
              :src="loadedBook.cover"
              :alt="loadedBook.title"
              class="book-header-card__cover"
            />
            <div v-else class="book-header-card__cover-placeholder">📖</div>
          </div>

          <div class="book-header-card__info">
            <div class="book-header-card__headline">
              <h1 class="book-header-card__title">{{ loadedBook.title }}</h1>
              <div class="book-header-card__meta-badges">
                <span class="meta-badge">总页数: <strong>{{ loadedBook.totalPages ?? 0 }}</strong></span>
                <span class="meta-badge">章节数: <strong>{{ chapters.length }}</strong></span>
                <span class="meta-badge meta-badge--success">已构建知识库: <strong>{{ analyzedChaptersCount }} / {{ chapters.length }} 章节</strong></span>
              </div>
            </div>

            <div v-if="tagItems.length > 0" class="book-header-card__tags">
              <ProductChipList :items="tagItems" />
            </div>

            <!-- 核心快捷入口按钮组 -->
            <div class="book-header-card__actions">
              <UiButton
                variant="primary"
                size="md"
                class="action-btn--primary"
                @click="goToGraph"
              >
                <UiIcon name="tags" size="18" />
                <span>进入知识图谱</span>
              </UiButton>

              <UiButton
                variant="primary"
                size="md"
                tone="success"
                class="action-btn--success"
                @click="goToQa"
              >
                <UiIcon name="message" size="18" />
                <span>知识库问答</span>
              </UiButton>

              <UiButton
                variant="secondary"
                size="md"
                @click="goToReader()"
              >
                <UiIcon name="book-open" size="18" />
                <span>立即阅读</span>
              </UiButton>

              <UiButton
                variant="secondary"
                size="md"
                @click="showRebuildConfirm = true"
              >
                <UiIcon name="refresh" size="18" />
                <span>重建知识库（不重跑页分析）</span>
              </UiButton>

              <UiButton
                variant="ghost"
                size="md"
                @click="deleteTarget = 'book'; showDeleteConfirm = true"
              >
                <UiIcon name="trash" size="18" />
                <span>删除书籍</span>
              </UiButton>
            </div>
          </div>
        </header>

        <!-- 章节列表模块 -->
        <section class="book-chapters-section">
          <div class="chapters-card">
            <div class="chapters-card__header">
              <h2 class="chapters-card__title">章节目录 ({{ chapters.length }})</h2>
            </div>

            <ChapterList
              :chapters="chapters"
              :drag-over-chapter-index="dragOverChapterIndex"
              :dragged-chapter-index="draggedChapterIndex"
              :selected-chapter-ids="selectedChapterIds"
              :analyzed-chapter-ids="analyzedChapterIds"
              :building-pending="isBuildingSelected"
              @select="handleSelectChapter"
              @select-all="handleSelectAllChapters"
              @build-knowledge-selected="handleBuildSelectedChapters"
              @create="openCreateChapterModal"
              @delete="deleteChapter"
              @edit="openEditChapterModal"
              @read="goToReader"
            />
          </div>
        </section>
      </div>
    </main>

    <!-- 章节编辑/新建弹窗 -->
    <BaseModal
      v-model="showChapterModal"
      :title="editingChapterId ? '编辑章节' : '新建章节'"
      size="small"
      :close-on-overlay="true"
      :close-on-esc="true"
    >
      <ChapterFormContent v-model="chapterTitle" @save="saveChapter" />
      <template #footer>
        <ProductActionRow aria-label="章节表单操作" variant="dialog">
          <UiButton type="button" variant="secondary" @click="showChapterModal = false">取消</UiButton>
          <UiButton type="button" variant="primary" :loading="isChapterSaving" @click="saveChapter">保存</UiButton>
        </ProductActionRow>
      </template>
    </BaseModal>

    <!-- 重建知识库确认弹窗 -->
    <BaseModal
      v-model="showRebuildConfirm"
      title="重建知识库"
      size="small"
      :close-on-overlay="true"
      :close-on-esc="true"
    >
      <div class="rebuild-confirm-body">
        <p>将复用已发布的页面分析结果，只重跑汇总层、故事概览、时间线、实体对齐与向量索引，不会重新调用视觉模型逐页看图。</p>
        <p>适用于修改了分析架构或预设后刷新产物；耗时通常是全书分析的十分之一以内，但页面覆盖率取决于已有分析结果，覆盖不足的产物会显示为「部分可用」。</p>
      </div>
      <template #footer>
        <ProductActionRow aria-label="重建知识库操作" variant="dialog">
          <UiButton type="button" variant="secondary" @click="showRebuildConfirm = false">取消</UiButton>
          <UiButton type="button" variant="primary" :loading="isRebuilding" @click="handleRebuildKnowledge">开始重建</UiButton>
        </ProductActionRow>
      </template>
    </BaseModal>

    <!-- 删除确认弹窗 -->
    <BaseModal
      v-model="showDeleteConfirm"
      title="删除确认"
      size="small"
      :close-on-overlay="true"
      :close-on-esc="true"
    >
      <BookDeleteConfirmContent :target="deleteTarget" />
      <template #footer>
        <ProductActionRow
          aria-label="删除操作"
          variant="dialog"
        >
          <UiButton type="button" variant="secondary" @click="showDeleteConfirm = false">取消</UiButton>
          <UiButton type="button" variant="danger" :loading="isDeleting" @click="handleConfirmDelete">删除</UiButton>
        </ProductActionRow>
      </template>
    </BaseModal>
  </AppShell>
</template>

<style scoped>
.rebuild-confirm-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.book-detail-page {
  --book-detail-shadow-card: 0 1px 3px rgba(0, 0, 0, 0.03);
  --book-detail-shadow-cover: 0 4px 12px rgba(0, 0, 0, 0.08);
  --book-detail-brand-deep: #7e5bb4;
  --book-detail-brand-gradient: linear-gradient(135deg, var(--color-brand-primary) 0%, var(--book-detail-brand-deep) 100%);
  --book-detail-success-surface: rgba(16, 185, 129, 0.15);
  --book-detail-success-border: rgba(16, 185, 129, 0.3);
  --book-detail-success-text: #10b981;

  background-color: var(--color-bg-base);
  color: var(--color-text-primary);
  display: flex;
  flex-direction: column;
}

.book-detail-main {
  flex: 1;
  max-width: 1440px;
  width: 100%;
  margin: 0 auto;
  padding: 24px;
}

.book-detail-banner {
  margin-bottom: 20px;
}

.book-detail-content {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

/* 顶部书籍信息卡片 */
.book-header-card {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 24px;
  display: flex;
  gap: 24px;
  box-shadow: var(--book-detail-shadow-card);
}

.book-header-card__cover-box {
  width: 130px;
  height: 180px;
  flex-shrink: 0;
  border-radius: 8px;
  overflow: hidden;
  background: var(--color-surface-secondary);
  box-shadow: var(--book-detail-shadow-cover);
}

.book-header-card__cover {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.book-header-card__cover-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 2.8rem;
}

.book-header-card__info {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  min-width: 0;
}

.book-header-card__headline {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.book-header-card__title {
  font-size: 1.55rem;
  font-weight: 700;
  color: var(--color-text-primary);
  margin: 0;
  line-height: 1.25;
}

.book-header-card__meta-badges {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.meta-badge {
  font-size: 0.84rem;
  padding: 3px 10px;
  border-radius: 6px;
  background-color: var(--color-surface-secondary);
  color: var(--color-text-secondary);
}

.meta-badge--success {
  background-color: var(--book-detail-success-surface);
  color: var(--book-detail-success-text);
  border: 1px solid var(--book-detail-success-border);
}

.book-header-card__tags {
  margin-top: 8px;
}

.book-header-card__actions {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--color-border);
}

.action-btn--primary {
  background: var(--book-detail-brand-gradient);
  border: none;
  font-weight: 600;
}

.action-btn--success {
  font-weight: 600;
}

/* 章节列表区 */
.book-chapters-section {
  display: flex;
  flex-direction: column;
}

.chapters-card {
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 24px;
  box-shadow: var(--book-detail-shadow-card);
}

.chapters-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--color-border);
}

.chapters-card__title {
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--color-text-primary);
  margin: 0;
}
</style>
