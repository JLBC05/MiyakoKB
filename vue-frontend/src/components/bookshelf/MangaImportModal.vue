<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import BaseModal from '@/components/common/BaseModal.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import ProductActionRow from '@/components/product/ProductActionRow.vue'
import {
  getVeneraLibrary,
  importVeneraComics,
  type VeneraComic,
} from '@/api/v2/mangaImport'
import { showToast } from '@/utils/toast'

const emit = defineEmits<{
  close: []
  imported: []
}>()

const activeTab = ref<'venera' | 'local'>('venera')
const isLoading = ref(false)
const isImporting = ref(false)
const veneraFound = ref(true)
const comics = ref<VeneraComic[]>([])
const selectedComicIds = ref<Set<string>>(new Set())
const errorMessage = ref<string | null>(null)
const importProgressText = ref<string | null>(null)

async function loadVeneraComics() {
  isLoading.value = true
  errorMessage.value = null
  try {
    const res = await getVeneraLibrary()
    veneraFound.value = res.veneraFound
    comics.value = res.comics || []
    if (!res.veneraFound) {
      errorMessage.value = res.error || '未检测到 Venera，请确认已安装并下载漫画'
    }
  } catch (err: unknown) {
    veneraFound.value = false
    errorMessage.value = err instanceof Error ? err.message : '获取 Venera 漫画列表失败'
  } finally {
    isLoading.value = false
  }
}

function toggleSelect(comicId: string) {
  if (isImporting.value) return
  if (selectedComicIds.value.has(comicId)) {
    selectedComicIds.value.delete(comicId)
  } else {
    selectedComicIds.value.add(comicId)
  }
}

function toggleSelectAll() {
  if (isImporting.value) return
  if (selectedComicIds.value.size === comics.value.length) {
    selectedComicIds.value.clear()
  } else {
    selectedComicIds.value = new Set(comics.value.map(c => c.id))
  }
}

async function handleImport() {
  if (selectedComicIds.value.size === 0) {
    showToast('请先选择要导入的漫画', 'info')
    return
  }

  isImporting.value = true
  importProgressText.value = `正在导入 ${selectedComicIds.value.size} 部漫画，请稍候...`

  try {
    const ids = Array.from(selectedComicIds.value)
    const res = await importVeneraComics(ids)
    if (res.imported > 0) {
      showToast(`成功导入 ${res.imported} 部漫画！`, 'success')
      emit('imported')
      emit('close')
    } else {
      const err = res.details.map(d => d.error).filter(Boolean).join('; ')
      showToast(`导入失败: ${err || '未知错误'}`, 'error')
    }
  } catch (err: unknown) {
    showToast(err instanceof Error ? err.message : '导入请求失败', 'error')
  } finally {
    isImporting.value = false
    importProgressText.value = null
  }
}

onMounted(() => {
  loadVeneraComics()
})
</script>

<template>
  <BaseModal
    title="导入漫画"
    size="lg"
    @close="emit('close')"
  >
    <div class="manga-import-modal">
      <!-- Tabs Header -->
      <div class="manga-import-tabs">
        <button
          type="button"
          class="manga-import-tab"
          :class="{ active: activeTab === 'venera' }"
          @click="activeTab = 'venera'"
        >
          <UiIcon name="download" size="16" />
          <span>从 Venera 导入</span>
        </button>
        <button
          type="button"
          class="manga-import-tab"
          :class="{ active: activeTab === 'local' }"
          @click="activeTab = 'local'"
        >
          <UiIcon name="folder" size="16" />
          <span>从本地文件导入</span>
        </button>
      </div>

      <!-- Tab 1: Venera Import -->
      <div v-if="activeTab === 'venera'" class="manga-import-content">
        <div v-if="isLoading" class="manga-import-status">
          <UiIcon name="loader" size="24" class="spinning" />
          <span>正在扫描 Venera 漫画库...</span>
        </div>

        <div v-else-if="!veneraFound || errorMessage" class="manga-import-empty">
          <UiIcon name="alert-circle" size="32" class="error-icon" />
          <p>{{ errorMessage || '未检测到 Venera，请确认已安装并下载漫画' }}</p>
          <UiButton size="sm" variant="secondary" @click="loadVeneraComics">
            重新扫描
          </UiButton>
        </div>

        <div v-else-if="comics.length === 0" class="manga-import-empty">
          <UiIcon name="inbox" size="32" />
          <p>Venera 本地库暂无漫画</p>
        </div>

        <div v-else class="venera-comic-list-wrapper">
          <div class="venera-list-toolbar">
            <label class="select-all-label">
              <input
                type="checkbox"
                :checked="selectedComicIds.size === comics.length && comics.length > 0"
                :indeterminate="selectedComicIds.size > 0 && selectedComicIds.size < comics.length"
                :disabled="isImporting"
                @change="toggleSelectAll"
              >
              <span>全选 ({{ selectedComicIds.size }}/{{ comics.length }})</span>
            </label>
            <span class="venera-hint">仅扫描本地已下载的章节与图片</span>
          </div>

          <div class="venera-comic-grid">
            <div
              v-for="comic in comics"
              :key="comic.id"
              class="venera-comic-card"
              :class="{ selected: selectedComicIds.has(comic.id), disabled: isImporting }"
              @click="toggleSelect(comic.id)"
            >
              <div class="card-checkbox">
                <input
                  type="checkbox"
                  :checked="selectedComicIds.has(comic.id)"
                  :disabled="isImporting"
                  @click.stop="toggleSelect(comic.id)"
                >
              </div>
              <div class="card-cover-wrapper">
                <div class="card-cover-placeholder">
                  <UiIcon name="book" size="32" />
                </div>
              </div>
              <div class="card-info">
                <div class="card-title" :title="comic.title">{{ comic.title }}</div>
                <div class="card-meta">
                  <span class="chapter-count">已下载 {{ comic.chapterCount }} 话</span>
                </div>
                <div v-if="comic.tags && comic.tags.length" class="card-tags">
                  <span v-for="tag in comic.tags.slice(0, 3)" :key="tag" class="tag-badge">
                    {{ tag }}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Import Progress / Message -->
        <div v-if="importProgressText" class="import-progress-banner">
          <UiIcon name="loader" size="18" class="spinning" />
          <span>{{ importProgressText }}</span>
        </div>
      </div>

      <!-- Tab 2: Local File Import (Placeholder) -->
      <div v-else-if="activeTab === 'local'" class="manga-import-content local-placeholder">
        <UiIcon name="archive" size="48" class="placeholder-icon" />
        <h3>从本地文件导入</h3>
        <p>支持 ZIP/CBZ/PDF/EPUB 或文件夹格式批量导入漫画。</p>
        <div class="coming-soon-badge">即将支持</div>
      </div>
    </div>

    <template #footer>
      <ProductActionRow variant="dialog">
        <div class="footer-left">
          <span v-if="activeTab === 'venera' && selectedComicIds.size > 0">
            已选择 <strong>{{ selectedComicIds.size }}</strong> 部漫画
          </span>
        </div>
        <UiButton variant="secondary" :disabled="isImporting" @click="emit('close')">
          取消
        </UiButton>
        <UiButton
          v-if="activeTab === 'venera'"
          variant="primary"
          :disabled="selectedComicIds.size === 0 || isImporting"
          @click="handleImport"
        >
          <UiIcon v-if="isImporting" name="loader" size="16" class="spinning" />
          <span>{{ isImporting ? '导入中...' : '开始导入' }}</span>
        </UiButton>
      </ProductActionRow>
    </template>
  </BaseModal>
</template>

<style scoped>
.manga-import-modal {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 400px;
  max-height: 70vh;
}

.manga-import-tabs {
  display: flex;
  gap: 8px;
  border-bottom: 1px solid var(--border-color, #e2e8f0);
  padding-bottom: 8px;
}

.manga-import-tab {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-muted, #64748b);
  background: transparent;
  border: none;
  cursor: pointer;
  transition: all 0.2s ease;
}

.manga-import-tab:hover {
  background: var(--bg-hover, #f1f5f9);
  color: var(--text-color, #1e293b);
}

.manga-import-tab.active {
  background: var(--miyako-primary, #6366f1);
  color: #ffffff;
}

.manga-import-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.manga-import-status,
.manga-import-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 48px 16px;
  color: var(--text-muted, #64748b);
  text-align: center;
}

.error-icon {
  color: #ef4444;
}

.venera-comic-list-wrapper {
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
  max-height: 480px;
  padding-right: 4px;
}

.venera-list-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 8px;
  background: var(--bg-surface, #f8fafc);
  border-radius: 6px;
  font-size: 13px;
}

.select-all-label {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}

.venera-hint {
  color: var(--text-muted, #94a3b8);
  font-size: 12px;
}

.venera-comic-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}

.venera-comic-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px;
  border: 1px solid var(--border-color, #e2e8f0);
  border-radius: 8px;
  background: var(--bg-card, #ffffff);
  cursor: pointer;
  position: relative;
  transition: all 0.2s ease;
}

.venera-comic-card:hover {
  border-color: var(--miyako-primary, #6366f1);
  transform: translateY(-1px);
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
}

.venera-comic-card.selected {
  border-color: var(--miyako-primary, #6366f1);
  background: rgba(99, 102, 241, 0.04);
}

.venera-comic-card.disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.card-checkbox {
  display: flex;
  align-items: center;
}

.card-cover-wrapper {
  width: 48px;
  height: 64px;
  flex-shrink: 0;
  border-radius: 4px;
  overflow: hidden;
  background: var(--bg-muted, #e2e8f0);
  display: flex;
  align-items: center;
  justify-content: center;
}

.card-cover-placeholder {
  color: var(--text-muted, #94a3b8);
}

.card-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-color, #1e293b);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-meta {
  font-size: 12px;
  color: var(--text-muted, #64748b);
}

.card-tags {
  display: flex;
  gap: 4px;
  overflow: hidden;
}

.tag-badge {
  font-size: 10px;
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--bg-muted, #f1f5f9);
  color: var(--text-muted, #64748b);
  white-space: nowrap;
}

.import-progress-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-radius: 6px;
  background: rgba(99, 102, 241, 0.1);
  color: var(--miyako-primary, #6366f1);
  font-size: 14px;
  font-weight: 500;
  margin-top: 12px;
}

.local-placeholder {
  align-items: center;
  justify-content: center;
  gap: 16px;
  padding: 48px 16px;
  text-align: center;
  color: var(--text-muted, #64748b);
}

.placeholder-icon {
  color: var(--miyako-primary, #6366f1);
  opacity: 0.8;
}

.coming-soon-badge {
  display: inline-block;
  padding: 4px 12px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
  background: var(--bg-muted, #f1f5f9);
  color: var(--text-muted, #64748b);
}

.footer-left {
  font-size: 13px;
  color: var(--text-muted, #64748b);
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
