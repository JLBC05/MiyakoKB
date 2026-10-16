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
import { getBookDetail } from '@/api/bookshelf'
import { showToast } from '@/utils/toast'
import type { BookData, ChapterData } from '@/types'

const route = useRoute()
const router = useRouter()
const bookshelfStore = useBookshelfStore()

const bookId = computed(() => (route.params.bookId as string) || (route.query.book as string) || '')

const loadedBook = ref<BookData | null>(null)
const chapters = ref<ChapterData[]>([])
const isLoading = ref(true)
const loadError = ref<string | null>(null)

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
const selectedChapterIds = ref<string[]>([])
const isBatchDownloading = ref(false)
const isBatchTranslating = ref(false)
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

function goToCharacterStudio(): void {
  if (bookId.value) {
    void router.push({
      name: 'character-studio',
      query: { book: bookId.value },
    })
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
                <UiIcon name="tag" size="18" />
                <span>进入知识图谱</span>
              </UiButton>

              <UiButton
                variant="primary"
                size="md"
                tone="success"
                class="action-btn--success"
                @click="goToQa"
              >
                <UiIcon name="message-square" size="18" />
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
                @click="goToCharacterStudio"
              >
                <UiIcon name="user" size="18" />
                <span>角色工坊</span>
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
              <UiButton size="sm" variant="secondary" @click="openCreateChapterModal">
                <UiIcon name="plus" size="14" />
                <span>新建章节</span>
              </UiButton>
            </div>

            <ChapterList
              :chapters="chapters"
              :drag-over-chapter-index="dragOverChapterIndex"
              :dragged-chapter-index="draggedChapterIndex"
              :download-pending="isBatchDownloading"
              :selected-chapter-ids="selectedChapterIds"
              :translation-pending="isBatchTranslating"
              :translation-allowed="false"
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

    <!-- 删除确认弹窗 -->
    <BaseModal
      v-model="showDeleteConfirm"
      title="删除确认"
      size="small"
      :close-on-overlay="true"
      :close-on-esc="true"
    >
      <BookDeleteConfirmContent
        :is-deleting="isDeleting"
        :message="deleteTarget === 'book' ? `确定永久删除《${loadedBook?.title}》吗？` : '确定删除该章节吗？'"
        @cancel="showDeleteConfirm = false"
        @confirm="handleConfirmDelete"
      />
    </BaseModal>
  </AppShell>
</template>

<style scoped>
.book-detail-page {
  min-height: 100vh;
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
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
}

.book-header-card__cover-box {
  width: 130px;
  height: 180px;
  flex-shrink: 0;
  border-radius: 8px;
  overflow: hidden;
  background: var(--color-surface-secondary);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
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
  background: linear-gradient(135deg, var(--color-primary) 0%, #7e5bb4 100%);
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
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
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
