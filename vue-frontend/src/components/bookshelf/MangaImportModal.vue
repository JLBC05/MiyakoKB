<script setup lang="ts">
import { ref, onMounted } from 'vue'
import BaseModal from '@/components/common/BaseModal.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import UiCheckbox from '@/components/ui/UiCheckbox.vue'
import UiInput from '@/components/ui/UiInput.vue'
import UiTextarea from '@/components/ui/UiTextarea.vue'
import UiNumberField from '@/components/ui/UiNumberField.vue'
import ProductActionRow from '@/components/product/ProductActionRow.vue'
import {
  getVeneraLibrary,
  getVeneraCoverUrl,
  importVeneraComics,
  previewLocalManga,
  confirmLocalImport,
  checkImportIntegrity,
  type VeneraComic,
  type LocalBookPreview,
  type LocalBookConfirmPayload,
  type ImportIssue,
  type AggregateIntegrityReport,
  type IntegrityReport,
} from '@/api/v2/mangaImport'
import { getV2Settings, saveV2SettingsTransaction } from '@/api/v2/settings'
import { showToast } from '@/utils/toast'

/** 导入结果里任何一条带有 issues 的条目（Venera 与本地两种形状共用）。 */
type ImportedOutcome = { issues?: ImportIssue[]; title?: string; comic_id?: string; book_id?: string | null }

function issueOwner(item: ImportedOutcome): string {
  return item.title || item.comic_id || item.book_id || '这本书'
}

/** 把单条 issue 渲染成一句可读的话，带上章 / 页定位。 */
function describeIssue(issue: ImportIssue, owner: string): string {
  const chapter = issue.chapter
  const where = chapter
    ? `第${chapter.ordinal}话（${chapter.title}）${issue.scope === 'page' ? ` 第${issue.page}页` : ''}`
    : issue.scope === 'cover'
      ? '封面'
      : '整本书'
  return `${owner} · ${where}：${issue.reason}［${issue.code}］`
}

function issueLines(items: readonly ImportedOutcome[] | undefined | null): string[] {
  const lines: string[] = []
  for (const item of items ?? []) {
    for (const issue of item.issues ?? []) {
      lines.push(describeIssue(issue, issueOwner(item)))
    }
  }
  return lines
}

/**
 * 汇总文案：成功 / 部分成功 / 失败的计数，失败时附上首条具体原因，
 * 避免过去「导入成功」但书残缺的情况。
 */
function summarizeIssues(
  items: readonly ImportedOutcome[] | undefined | null,
  partial: number,
  failed: number
): string {
  const total = items?.length ?? 0
  const lines = issueLines(items)
  if (failed > 0 && total > 0 && failed === total) {
    return lines[0] ?? `全部 ${total} 部漫画导入失败`
  }
  const ok = total - partial - failed
  const head = partial > 0
    ? `成功 ${ok} 部，部分失败 ${partial} 部${failed > 0 ? `，失败 ${failed} 部` : ''}`
    : `成功导入 ${ok} 部漫画`
  return lines.length > 0 ? `${head}：${lines[0]}` : head
}

/**
 * 导入响应里自带核对结果时的提示文案。丢件为 0 时返回 null（交给原汇总文案），
 * 有丢件才替换，因为这正是用户最需要立刻知道的事。
 */
function describeIntegrity(report: IntegrityReport | null | undefined): string | null {
  if (!report || report.ok === null) return null
  if (report.lost.length === 0 && report.problems.length === 0) return null
  const lost = report.lost.length
  const unreadable = report.problems.length
  const first = report.lost[0]?.label ?? report.problems[0]?.label ?? ''
  const parts: string[] = ['导入完成，但核对发现问题']
  if (lost > 0) parts.push(`丢件 ${lost} 章`)
  if (unreadable > 0) parts.push(`读取失败 ${unreadable} 章`)
  return `${parts.join('、')}：${first}`
}

const emit = defineEmits<{
  close: []
  imported: []
}>()

const activeTab = ref<'venera' | 'local'>('venera')
const isLoading = ref(false)
const isImporting = ref(false)
const importPercent = ref(0)
const importProgressText = ref<string | null>(null)
const importSubText = ref<string | null>(null)
let progressIntervalId: ReturnType<typeof setInterval> | null = null
const veneraFound = ref(true)
const comics = ref<VeneraComic[]>([])
const selectedComicIds = ref<Set<string>>(new Set())
const errorMessage = ref<string | null>(null)

function startSmoothProgress(start = 10, max = 92, speedMs = 120) {
  importPercent.value = start
  if (progressIntervalId) clearInterval(progressIntervalId)
  progressIntervalId = setInterval(() => {
    if (importPercent.value < max) {
      const remaining = max - importPercent.value
      const step = Math.max(0.2, remaining * 0.04)
      importPercent.value = Math.min(max, importPercent.value + step)
    }
  }, speedMs)
}

function finishProgress() {
  if (progressIntervalId) {
    clearInterval(progressIntervalId)
    progressIntervalId = null
  }
  importPercent.value = 100
}

// Local import state
const localPathInput = ref('')
const isScanningLocal = ref(false)
const localBooks = ref<LocalBookPreview[]>([])
const expandedBookIds = ref<Set<string>>(new Set())
const aiVisionNeededCount = ref(0)
const localScanError = ref<string | null>(null)

// Venera base dir setting (empty means "use the system default")
const veneraBaseDir = ref('')
const veneraBaseDirSource = ref<'setting' | 'default'>('default')
const isSavingVeneraBaseDir = ref(false)
const veneraBaseDirHint = ref<string | null>(null)

async function loadVeneraComics() {
  isLoading.value = true
  errorMessage.value = null
  try {
    const res = await getVeneraLibrary()
    veneraFound.value = res.veneraFound
    comics.value = res.comics || []
    veneraBaseDir.value = res.baseDir ?? ''
    veneraBaseDirSource.value = res.baseDirSource === 'setting' ? 'setting' : 'default'
    if (!res.veneraFound) {
      errorMessage.value = '未检测到 Venera，请确认已安装并下载漫画'
    }
  } catch (err: unknown) {
    veneraFound.value = false
    errorMessage.value = err instanceof Error ? err.message : '获取 Venera 漫画列表失败'
  } finally {
    isLoading.value = false
  }
}

function veneraCoverUrl(comic: VeneraComic): string | undefined {
  return comic.hasCover ? getVeneraCoverUrl(comic.id) : undefined
}

async function saveVeneraBaseDir() {
  if (isSavingVeneraBaseDir.value) return
  isSavingVeneraBaseDir.value = true
  veneraBaseDirHint.value = null
  try {
    const document = await getV2Settings(['manga_import'])
    const entry = (document.settings ?? []).find(item => item.domain === 'manga_import')
    const revision = typeof entry?.revision === 'number' ? entry.revision : 0
    await saveV2SettingsTransaction({
      settings: [
        {
          domain: 'manga_import',
          payload: { veneraBaseDir: veneraBaseDir.value.trim() },
          baseRevision: revision,
        },
      ],
    })
    await loadVeneraComics()
    veneraBaseDirHint.value = veneraBaseDir.value.trim() ? '已保存并使用该目录' : '已恢复默认目录'
  } catch (err: unknown) {
    veneraBaseDirHint.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    isSavingVeneraBaseDir.value = false
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

const integrityReport = ref<IntegrityReport | null>(null)
const integrityTarget = ref<string>('')
const isCheckingIntegrity = ref(false)

async function handleIntegrityCheck() {
  const comicId = Array.from(selectedComicIds.value)[0]
  if (!comicId) {
    showToast('请先选择一部漫画', 'info')
    return
  }
  const comic = comics.value.find(c => c.id === comicId)
  integrityTarget.value = comic?.title ?? comicId
  isCheckingIntegrity.value = true
  integrityReport.value = null
  try {
    const report = await checkImportIntegrity({ source: 'venera', comicId, bookId: '' })
    integrityReport.value = report
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    showToast(`核对失败：${detail}`, 'error')
  } finally {
    isCheckingIntegrity.value = false
  }
}

async function handleImport() {
  if (selectedComicIds.value.size === 0) {
    showToast('请先选择要导入的漫画', 'info')
    return
  }

  isImporting.value = true
  const total = selectedComicIds.value.size
  importProgressText.value = `正在准备导入 ${total} 部漫画...`
  importSubText.value = '正在读取 Venera 数据库与章节目录'
  startSmoothProgress(10, 92, 100)

  const timer1 = setTimeout(() => {
    if (isImporting.value) {
      importProgressText.value = `正在发布封面与图片切片资产...`
      importSubText.value = '解压并写入本地媒体库，请耐心等待'
    }
  }, 1200)

  const timer2 = setTimeout(() => {
    if (isImporting.value) {
      importProgressText.value = `正在写入章节元数据与页码映射...`
      importSubText.value = '图书数据库持久化中，即将完成'
    }
  }, 3500)

  try {
    const ids = Array.from(selectedComicIds.value)
    const res = await importVeneraComics(ids)
    finishProgress()
    clearTimeout(timer1)
    clearTimeout(timer2)
    importProgressText.value = `导入完成！`
    importSubText.value = summarizeIssues(res.details, res.partial, res.failed)

    await new Promise(r => setTimeout(r, 600))
    // 导入响应里已经带了核对结果：导入完成后直接把第一本的报告摊开，
    // 用户不用再手动点「核对导入完整性」。
    const firstIntegrity = res.details.find(d => d.integrity && d.book_id)?.integrity ?? null
    if (firstIntegrity && firstIntegrity.ok !== null) {
      integrityTarget.value = res.details.find(d => d.integrity && d.book_id)?.title ?? ''
      integrityReport.value = firstIntegrity as IntegrityReport
    }
    if (res.imported > 0 || res.partial > 0) {
      showToast(describeIntegrity(firstIntegrity) ?? summarizeIssues(res.details, res.partial, res.failed), 'success')
      emit('imported')
      emit('close')
    } else {
      showToast(issueLines(res.details)[0] ?? '导入失败，请查看控制台日志', 'error')
    }
  } catch (err: unknown) {
    clearTimeout(timer1)
    clearTimeout(timer2)
    finishProgress()
    showToast(err instanceof Error ? err.message : '导入失败', 'error')
  } finally {
    isImporting.value = false
    importProgressText.value = null
    importSubText.value = null
    if (progressIntervalId) {
      clearInterval(progressIntervalId)
      progressIntervalId = null
    }
  }
}

// Local scan & confirm
async function handleScanLocal() {
  const lines = localPathInput.value
    .split('\n')
    .map(l => l.trim())
    .filter(Boolean)

  if (lines.length === 0) {
    showToast('请输入至少一个文件夹或压缩包路径', 'info')
    return
  }

  isScanningLocal.value = true
  localScanError.value = null
  try {
    const res = await previewLocalManga(lines, false)
    localBooks.value = res.books || []
    aiVisionNeededCount.value = res.ai_vision_needed_count || 0
    expandedBookIds.value = new Set(
      localBooks.value.filter(b => b.needs_manual_review).map(b => b.temp_id)
    )
    if (localBooks.value.length === 0) {
      localScanError.value = '未扫描到可导入的漫画或章节，请检查路径中是否包含图片或 ZIP/CBZ'
    }
  } catch (err: unknown) {
    localScanError.value = err instanceof Error ? err.message : '扫描失败'
  } finally {
    isScanningLocal.value = false
  }
}

function toggleBookExpand(bookId: string) {
  if (expandedBookIds.value.has(bookId)) {
    expandedBookIds.value.delete(bookId)
  } else {
    expandedBookIds.value.add(bookId)
  }
}

async function handleConfirmLocal() {
  if (localBooks.value.length === 0) return

  // Validate ordinals for each book
  for (const book of localBooks.value) {
    const seen = new Set<number>()
    for (const ch of book.chapters) {
      if (ch.ordinal === null || isNaN(Number(ch.ordinal)) || Number(ch.ordinal) < 1) {
        showToast(`漫画《${book.title}》中章节“${ch.title}”缺少有效序号，请填写`, 'error')
        expandedBookIds.value.add(book.temp_id)
        return
      }
      const ord = Math.round(Number(ch.ordinal))
      if (seen.has(ord)) {
        showToast(`漫画《${book.title}》中存在重复序号 ${ord}，请先修正`, 'error')
        expandedBookIds.value.add(book.temp_id)
        return
      }
      seen.add(ord)
    }
  }

  isImporting.value = true
  const totalBooks = localBooks.value.length
  importProgressText.value = `正在导入 ${totalBooks} 部本地漫画...`
  importSubText.value = '正在读取文件流与扫描图片序列'
  startSmoothProgress(10, 92, 110)

  const timer1 = setTimeout(() => {
    if (isImporting.value) {
      importProgressText.value = `正在发布漫画封面与高清图片切片...`
      importSubText.value = '解压并生成媒体资产切片，请稍候'
    }
  }, 1200)

  const timer2 = setTimeout(() => {
    if (isImporting.value) {
      importProgressText.value = `正在建立章节目录并持久化存储...`
      importSubText.value = '图书数据库正在写入'
    }
  }, 3200)

  try {
    const payload: LocalBookConfirmPayload[] = localBooks.value.map(b => ({
      title: b.title.trim() || '未命名漫画',
      cover_source_path: b.cover_preview_path,
      chapters: b.chapters.map(c => ({
        title: c.title.trim(),
        ordinal: Math.round(Number(c.ordinal)),
        source_type: c.source_type,
        source_path: c.source_path,
      })),
    }))

    const res = await confirmLocalImport(payload)
    finishProgress()
    clearTimeout(timer1)
    clearTimeout(timer2)
    importProgressText.value = `导入完成！`
    importSubText.value = summarizeIssues(res.imported_books, res.partial, res.failed)

    await new Promise(r => setTimeout(r, 600))
    // 本地导入同样自带核对结果：直接摊开第一本的报告。
    const firstLocal = res.imported_books.find(b => b.integrity && b.book_id)
    const localReport = (firstLocal?.integrity ?? null) as AggregateIntegrityReport | null
    if (localReport && localReport.ok !== null) {
      integrityTarget.value = firstLocal?.title ?? ''
      integrityReport.value = localReport as IntegrityReport
    }
    if ((res.imported ?? 0) > 0 || (res.partial ?? 0) > 0) {
      showToast(
        describeIntegrity(localReport as IntegrityReport | null) ??
          summarizeIssues(res.imported_books, res.partial, res.failed),
        'success'
      )
      emit('imported')
      emit('close')
    } else {
      showToast(issueLines(res.imported_books)[0] ?? '导入失败，无有效漫画写入', 'error')
    }
  } catch (err: unknown) {
    clearTimeout(timer1)
    clearTimeout(timer2)
    finishProgress()
    showToast(err instanceof Error ? err.message : '导入失败', 'error')
  } finally {
    isImporting.value = false
    importProgressText.value = null
    importSubText.value = null
    if (progressIntervalId) {
      clearInterval(progressIntervalId)
      progressIntervalId = null
    }
  }
}

function getLayerBadge(layer: string): { label: string; class: string } {
  switch (layer) {
    case 'filename':
      return { label: '文件名规则', class: 'badge-high' }
    case 'metadata':
      return { label: '元数据(ComicInfo)', class: 'badge-high' }
    case 'ai_vision':
      return { label: 'AI识别', class: 'badge-medium' }
    default:
      return { label: '待指定序号', class: 'badge-low' }
  }
}

onMounted(() => {
  loadVeneraComics()
})
</script>

<template>
  <BaseModal title="导入漫画" width="820px" @close="emit('close')">
    <div class="manga-import-modal">
      <!-- Tabs Header -->
      <div class="manga-import-tabs">
        <UiButton
          variant="tab"
          class="manga-import-tab"
          :class="{ active: activeTab === 'venera' }"
          @click="activeTab = 'venera'"
        >
          <UiIcon name="download" size="16" />
          <span>从 Venera 导入</span>
        </UiButton>
        <UiButton
          variant="tab"
          class="manga-import-tab"
          :class="{ active: activeTab === 'local' }"
          @click="activeTab = 'local'"
        >
          <UiIcon name="folder-open" size="16" />
          <span>从本地文件导入</span>
        </UiButton>
      </div>

      <!-- Tab 1: Venera Import -->
      <div v-if="activeTab === 'venera'" class="manga-import-content">
        <div class="venera-path-row">
          <span class="venera-path-label">Venera 数据目录</span>
          <UiInput
            v-model="veneraBaseDir"
            class="venera-path-input"
            size="sm"
            placeholder="留空则使用默认位置"
            aria-label="Venera 数据目录"
            :disabled="isSavingVeneraBaseDir"
          />
          <UiButton
            variant="secondary"
            size="sm"
            :loading="isSavingVeneraBaseDir"
            :disabled="isSavingVeneraBaseDir"
            @click="saveVeneraBaseDir"
          >
            保存并使用
          </UiButton>
          <span class="venera-path-source">
            {{ veneraBaseDirSource === 'setting' ? '自定义目录' : '默认目录' }}
          </span>
        </div>
        <p v-if="veneraBaseDirHint" class="venera-path-hint">{{ veneraBaseDirHint }}</p>
        <div v-if="isLoading" class="manga-import-status">
          <UiIcon name="loading" size="24" class="spinning" />
          <span>正在读取 Venera 本地漫画库...</span>
        </div>

        <div v-else-if="!veneraFound || errorMessage" class="manga-import-status">
          <UiIcon name="alert-triangle" size="28" class="error-icon" />
          <p>{{ errorMessage || '未检测到 Venera 漫画库' }}</p>
          <UiButton variant="secondary" size="sm" @click="loadVeneraComics">
            重试检测
          </UiButton>
        </div>

        <div v-else-if="comics.length === 0" class="manga-import-empty">
          <UiIcon name="book-open" size="32" />
          <p>Venera 库中暂无已下载的漫画</p>
          <UiButton variant="secondary" size="sm" @click="loadVeneraComics">
            刷新列表
          </UiButton>
        </div>

        <div v-else class="venera-list-view">
          <div class="venera-toolbar">
            <div class="select-all-label">
              <UiCheckbox
                :model-value="selectedComicIds.size === comics.length && comics.length > 0"
                :indeterminate="selectedComicIds.size > 0 && selectedComicIds.size < comics.length"
                :disabled="isImporting"
                aria-label="全选本地漫画"
                @change="toggleSelectAll"
              />
              <span>全选 ({{ selectedComicIds.size }}/{{ comics.length }})</span>
            </div>
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
                <UiCheckbox
                  :model-value="selectedComicIds.has(comic.id)"
                  :disabled="isImporting"
                  @click.stop
                  @change="toggleSelect(comic.id)"
                />
              </div>
              <div class="card-cover-wrapper">
                <img
                  v-if="comic.hasCover"
                  class="card-cover-image"
                  :src="veneraCoverUrl(comic)"
                  :alt="comic.title"
                  loading="lazy"
                />
                <div v-else class="card-cover-placeholder">
                  <UiIcon name="book-open" size="32" />
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
        <div v-if="isImporting" class="import-progress-card">
          <div class="progress-info-row">
            <div class="progress-stage-wrap">
              <UiIcon name="loading" size="16" class="spinning" />
              <span class="progress-stage-text">{{ importProgressText || '正在导入漫画...' }}</span>
            </div>
            <span class="progress-percent-text">{{ Math.round(importPercent) }}%</span>
          </div>
          <div class="progress-bar-track">
            <div
              class="progress-bar-fill"
              :style="{ width: `${Math.min(100, Math.max(0, importPercent))}%` }"
            >
              <div class="progress-bar-glare"></div>
            </div>
          </div>
          <div v-if="importSubText" class="progress-sub-text">
            {{ importSubText }}
          </div>
        </div>

        <!-- Integrity check: did the import drop anything? -->
        <div v-if="integrityReport || isCheckingIntegrity" class="integrity-card">
          <div class="integrity-head">
            <span class="integrity-title">
              导入完整性核对<span v-if="integrityTarget">：{{ integrityTarget }}</span>
            </span>
            <span v-if="integrityReport" class="integrity-badge" :class="integrityReport.ok ? 'is-ok' : 'is-bad'">
              {{ integrityReport.ok ? '通过' : '发现问题' }}
            </span>
          </div>
          <div v-if="isCheckingIntegrity" class="integrity-line">正在比对磁盘章节与数据库记录…</div>
          <template v-else-if="integrityReport">
            <div class="integrity-line">
              文件系统章节 <strong>{{ integrityReport.filesystemChapters }}</strong>
              · 已导入章节 <strong>{{ integrityReport.importedChapters }}</strong>
              · 丢件 <strong :class="{ 'is-bad-text': integrityReport.lost.length > 0 }">{{ integrityReport.lost.length }}</strong>
            </div>
            <div v-if="integrityReport.notDownloaded.length" class="integrity-line integrity-muted">
              另有 {{ integrityReport.notDownloaded.length }} 章在 Venera 登记为已下载但磁盘没有，按「未下载」处理，不计入丢件。
            </div>
            <ul v-if="integrityReport.lost.length" class="integrity-list">
              <li v-for="item in integrityReport.lost.slice(0, 20)" :key="`lost-${item.ordinal}`">
                {{ item.label }}：{{ item.detail }}
              </li>
            </ul>
            <ul v-if="integrityReport.incomplete.length" class="integrity-list integrity-muted">
              <li v-for="item in integrityReport.incomplete.slice(0, 10)" :key="`inc-${item.ordinal}`">
                {{ item.label }}：{{ item.detail }}
              </li>
            </ul>
          </template>
        </div>
      </div>

      <!-- Tab 2: Local File Import -->
      <div v-else-if="activeTab === 'local'" class="manga-import-content local-content">
        <!-- Input section -->
        <div class="local-input-section">
          <label class="input-label">
            <span>输入本地文件夹路径或 ZIP/CBZ 文件路径（支持多行输入）：</span>
            <span class="input-hint">支持每部漫画为一个总目录、或单个话次文件夹 / 压缩包</span>
          </label>
          <UiTextarea
            v-model="localPathInput"
            class="local-path-textarea"
            :rows="3"
            placeholder="例如：&#10;D:\Manga\咒术回战&#10;D:\Downloads\电锯人_Vol1.zip"
            :disabled="isScanningLocal || isImporting"
          />
          <div class="input-actions">
            <UiButton
              variant="secondary"
              size="sm"
              :disabled="isScanningLocal || isImporting || !localPathInput.trim()"
              @click="handleScanLocal"
            >
              <UiIcon v-if="isScanningLocal" name="loading" size="14" class="spinning" />
              <span>{{ isScanningLocal ? '正在扫描并分析章节...' : '扫描识别' }}</span>
            </UiButton>
          </div>
        </div>

        <!-- Scan error -->
        <div v-if="localScanError" class="scan-error-alert">
          <UiIcon name="alert-triangle" size="16" />
          <span>{{ localScanError }}</span>
        </div>

        <!-- Banner for ordinal or review needed -->
        <div v-if="aiVisionNeededCount > 0" class="review-warning-banner">
          <UiIcon name="alert-triangle" size="16" />
          <span>检测到 {{ aiVisionNeededCount }} 个章节未能直接匹配序号，请在下方手动指定序号。</span>
        </div>

        <!-- Preview Result List -->
        <div v-if="localBooks.length > 0" class="local-preview-list">
          <div
            v-for="book in localBooks"
            :key="book.temp_id"
            class="local-book-item"
            :class="{ 'needs-review': book.needs_manual_review }"
          >
            <div class="book-item-header" @click="toggleBookExpand(book.temp_id)">
              <div class="book-header-left">
                <UiIcon :name="expandedBookIds.has(book.temp_id) ? 'chevron-down' : 'chevron-right'" size="16" />
                <UiInput
                  v-model="book.title"
                  class="book-title-input"
                  placeholder="漫画标题"
                  @click.stop
                />
              </div>
              <div class="book-header-right">
                <span class="badge chapter-total-badge">{{ book.chapters.length }} 话</span>
                <span v-if="book.needs_manual_review" class="badge warning-badge">需核对顺序</span>
              </div>
            </div>

            <!-- Chapters Tree -->
            <div v-if="expandedBookIds.has(book.temp_id)" class="book-chapters-table">
              <div class="chapter-row table-header">
                <span class="col-ord">序号</span>
                <span class="col-title">章节标题</span>
                <span class="col-type">类型</span>
                <span class="col-layer">识别来源</span>
                <span class="col-pages">页数</span>
              </div>
              <div
                v-for="ch in book.chapters"
                :key="ch.temp_id"
                class="chapter-row"
                :class="{ 'row-warning': ch.needs_ordinal || ch.ordinal === null }"
              >
                <div class="col-ord">
                  <UiNumberField
                    v-model="ch.ordinal"
                    :min="1"
                    size="sm"
                    nullable
                    class="ordinal-input"
                    :class="{ 'input-empty': ch.ordinal === null || ch.ordinal === undefined }"
                    aria-label="章节序号"
                  />
                </div>
                <div class="col-title">
                  <UiInput
                    v-model="ch.title"
                    class="ch-title-input"
                    placeholder="章节标题"
                  />
                </div>
                <div class="col-type">
                  <span class="type-tag">{{ ch.source_type === 'archive' ? 'ZIP' : '文件夹' }}</span>
                </div>
                <div class="col-layer">
                  <span :class="['layer-badge', getLayerBadge(ch.recognition_layer).class]">
                    {{ getLayerBadge(ch.recognition_layer).label }}
                  </span>
                </div>
                <div class="col-pages">
                  <span>{{ ch.page_count }} 页</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Import Progress / Message -->
        <div v-if="isImporting" class="import-progress-card">
          <div class="progress-info-row">
            <div class="progress-stage-wrap">
              <UiIcon name="loading" size="16" class="spinning" />
              <span class="progress-stage-text">{{ importProgressText || '正在导入本地漫画...' }}</span>
            </div>
            <span class="progress-percent-text">{{ Math.round(importPercent) }}%</span>
          </div>
          <div class="progress-bar-track">
            <div
              class="progress-bar-fill"
              :style="{ width: `${Math.min(100, Math.max(0, importPercent))}%` }"
            >
              <div class="progress-bar-glare"></div>
            </div>
          </div>
          <div v-if="importSubText" class="progress-sub-text">
            {{ importSubText }}
          </div>
        </div>
      </div>
    </div>

    <template #footer>
      <ProductActionRow variant="dialog">
        <div class="footer-left">
          <span v-if="activeTab === 'venera' && selectedComicIds.size > 0">
            已选择 <strong>{{ selectedComicIds.size }}</strong> 部漫画
          </span>
          <span v-else-if="activeTab === 'local' && localBooks.length > 0">
            共扫描到 <strong>{{ localBooks.length }}</strong> 部漫画，待导入
          </span>
        </div>
        <UiButton variant="secondary" :disabled="isImporting || isScanningLocal" @click="emit('close')">
          取消
        </UiButton>
        <UiButton
          v-if="activeTab === 'venera' && selectedComicIds.size === 1"
          variant="secondary"
          :disabled="isImporting || isCheckingIntegrity"
          @click="handleIntegrityCheck"
        >
          <UiIcon v-if="isCheckingIntegrity" name="loading" size="16" class="spinning" />
          <span>核对导入完整性</span>
        </UiButton>
        <UiButton
          v-if="activeTab === 'venera'"
          variant="primary"
          :disabled="selectedComicIds.size === 0 || isImporting"
          @click="handleImport"
        >
          <UiIcon v-if="isImporting" name="loading" size="16" class="spinning" />
          <span>{{ isImporting ? '导入中...' : '开始导入' }}</span>
        </UiButton>
        <UiButton
          v-else-if="activeTab === 'local'"
          variant="primary"
          :disabled="localBooks.length === 0 || isImporting || isScanningLocal"
          @click="handleConfirmLocal"
        >
          <UiIcon v-if="isImporting" name="loading" size="16" class="spinning" />
          <span>{{ isImporting ? '导入中...' : `确认导入 (${localBooks.length} 部)` }}</span>
        </UiButton>
      </ProductActionRow>
    </template>
  </BaseModal>
</template>

<style scoped>
.manga-import-modal {
  --manga-import-text: #ffffff;
  --manga-import-text-red: #ef4444;
  --manga-import-shadow-faint: rgba(0, 0, 0, 0.05);
  --manga-import-surface-faint: rgba(99, 102, 241, 0.05);
  --manga-import-surface-whisper: rgba(139, 92, 246, 0.08);
  --manga-import-stroke-light: rgba(139, 92, 246, 0.25);
  --manga-import-surface-whisper-white: rgba(255, 255, 255, 0.1);
  --manga-import-surface: #8b5cf6;
  --manga-import-surface-violet: #a855f7;
  --manga-import-surface-violet-mid: #c084fc;
  --manga-import-surface-medium: rgba(255, 255, 255, 0.4);
  --manga-import-text-blue: #94a3b8;
  --manga-import-surface-whisper-red: rgba(239, 68, 68, 0.1);
  --manga-import-surface-whisper-amber: rgba(245, 158, 11, 0.1);
  --manga-import-text-amber: #d97706;
  --manga-import-text-medium: rgba(245, 158, 11, 0.4);
  --manga-import-surface-soft: rgba(245, 158, 11, 0.15);
  --manga-import-text-amber-mid: #b45309;
  --manga-import-surface-faint-amber: rgba(245, 158, 11, 0.05);
  --manga-import-surface-faint-red: rgba(239, 68, 68, 0.05);
  --manga-import-surface-whisper-teal: rgba(34, 197, 94, 0.1);
  --manga-import-text-teal: #16a34a;
  --manga-import-text-red-mid: #dc2626;

  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 440px;
  max-height: 75vh;
}

.manga-import-tabs {
  display: flex;
  gap: 8px;
  border-bottom: 1px solid var(--color-border-muted);
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
  color: var(--color-text-muted);
  background: transparent;
  border: none;
  cursor: pointer;
  transition: all 0.2s ease;
}

.manga-import-tab:hover {
  background: var(--color-surface-hover);
  color: var(--color-text-default);
}

.manga-import-tab.active {
  background: var(--color-action-brand);
  color: var(--manga-import-text);
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
  color: var(--color-text-muted);
  text-align: center;
}

.error-icon {
  color: var(--manga-import-text-red);
}

.venera-list-view {
  display: flex;
  flex-direction: column;
  flex: 1;
  overflow: hidden;
  gap: 12px;
}

.venera-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 8px;
}

.select-all-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  user-select: none;
}

.venera-hint {
  font-size: 12px;
  color: var(--color-text-muted);
}

.venera-comic-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  overflow-y: auto;
  padding: 4px;
  max-height: 480px;
}

.venera-comic-card {
  position: relative;
  border: 1px solid var(--color-border-muted);
  border-radius: 8px;
  padding: 10px;
  background: var(--color-surface-base);
  cursor: pointer;
  transition: all 0.2s ease;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.venera-comic-card:hover {
  border-color: var(--color-action-brand);
  transform: translateY(-2px);
  box-shadow: 0 4px 12px var(--manga-import-shadow-faint);
}

.venera-comic-card.selected {
  border-color: var(--color-action-brand);
  background: var(--miyako-primary-bg, var(--manga-import-surface-faint));
}

.venera-comic-card.disabled {
  opacity: 0.6;
  pointer-events: none;
}

.card-checkbox {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: var(--z-local);
}

.card-cover-wrapper {
  width: 100%;
  height: 120px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--color-surface-quiet);
  display: flex;
  align-items: center;
  justify-content: center;
}

.card-cover-placeholder {
  color: var(--color-text-muted);
}

.card-cover-image {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.venera-path-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.venera-path-label {
  font-size: 12px;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.venera-path-input {
  flex: 1 1 320px;
  min-width: 220px;
}

.venera-path-source {
  font-size: 12px;
  color: var(--color-text-muted);
}

.venera-path-hint {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.card-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.card-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-default);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-meta {
  font-size: 11px;
  color: var(--color-text-muted);
}

.card-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.tag-badge {
  font-size: 10px;
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--color-surface-neutral-muted);
  color: var(--color-text-secondary);
}

.import-progress-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  border-radius: 8px;
  background: var(--manga-import-surface-whisper);
  border: 1px solid var(--manga-import-stroke-light);
  margin-top: 10px;
}

.progress-info-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.progress-stage-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-brand-primary);
  font-size: 13px;
  font-weight: 600;
}

.progress-percent-text {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-brand-primary);
  font-variant-numeric: tabular-nums;
}

.progress-bar-track {
  width: 100%;
  height: 7px;
  border-radius: 9999px;
  background: var(--manga-import-surface-whisper-white);
  overflow: hidden;
  position: relative;
}

.progress-bar-fill {
  height: 100%;
  border-radius: 9999px;
  background: linear-gradient(90deg, var(--manga-import-surface) 0%, var(--manga-import-surface-violet) 50%, var(--manga-import-surface-violet-mid) 100%);
  transition: width 0.15s ease-out;
  position: relative;
  overflow: hidden;
}

.progress-bar-glare {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: linear-gradient(
    90deg,
    transparent 0%,
    var(--manga-import-surface-medium) 50%,
    transparent 100%
  );
  animation: progress-glare-move 1.5s infinite;
}

@keyframes progress-glare-move {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
}

.progress-sub-text {
  font-size: 11.5px;
  color: var(--color-text-secondary, var(--manga-import-text-blue));
  padding-left: 24px;
}

.integrity-card {
  margin-top: 12px;
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--miyako-primary-bg, var(--manga-import-surface-faint));
  border: 1px solid var(--manga-import-stroke-light);
}

.integrity-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}

.integrity-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--color-text-primary, var(--manga-import-text));
}

.integrity-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
}

.integrity-badge.is-ok {
  background: var(--manga-import-surface-whisper);
  color: var(--manga-import-surface-violet);
}

.integrity-badge.is-bad {
  background: var(--manga-import-surface-whisper-red);
  color: var(--manga-import-text-red);
}

.integrity-line {
  font-size: 12px;
  line-height: 1.7;
  color: var(--color-text-secondary, var(--manga-import-text-blue));
}

.integrity-muted {
  opacity: 0.78;
}

.is-bad-text {
  color: var(--manga-import-text-red);
}

.integrity-list {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--color-text-secondary, var(--manga-import-text-blue));
  max-height: 160px;
  overflow-y: auto;
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* Local Tab Styles */
.local-content {
  gap: 12px;
  overflow-y: auto;
  max-height: 520px;
  padding-right: 4px;
}

.local-input-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--color-surface-quiet);
  padding: 12px;
  border-radius: 6px;
  border: 1px solid var(--color-border-muted);
}

.input-label {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-default);
}

.input-hint {
  font-size: 11px;
  color: var(--color-text-muted);
}

.local-path-textarea {
  width: 100%;
  border-radius: 4px;
  border: 1px solid var(--color-border-default);
  padding: 8px;
  font-size: 12px;
  font-family: monospace;
  resize: vertical;
  background: var(--color-surface-base);
  color: var(--color-text-default);
}

.local-path-textarea:focus {
  outline: none;
  border-color: var(--color-action-brand);
}

.input-actions {
  display: flex;
  justify-content: flex-end;
}

.scan-error-alert {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 4px;
  background: var(--manga-import-surface-whisper-red);
  color: var(--manga-import-text-red);
  font-size: 12px;
}

.review-warning-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 4px;
  background: var(--manga-import-surface-whisper-amber);
  color: var(--manga-import-text-amber);
  font-size: 12px;
  font-weight: 500;
}

.local-preview-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.local-book-item {
  border: 1px solid var(--color-border-muted);
  border-radius: 6px;
  background: var(--color-surface-base);
  overflow: hidden;
}

.local-book-item.needs-review {
  border-color: var(--manga-import-text-medium);
}

.book-item-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  background: var(--color-surface-quiet);
  cursor: pointer;
  user-select: none;
}

.book-item-header:hover {
  background: var(--color-surface-hover);
}

.book-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
}

.book-title-input {
  font-size: 14px;
  font-weight: 600;
  border: 1px solid transparent;
  padding: 2px 6px;
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-default);
  width: 280px;
}

.book-title-input:hover,
.book-title-input:focus {
  border-color: var(--color-border-default);
  background: var(--color-surface-base);
  outline: none;
}

.book-header-right {
  display: flex;
  align-items: center;
  gap: 6px;
}

.badge {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
}

.chapter-total-badge {
  background: var(--color-surface-hover);
  color: var(--color-text-secondary);
}

.warning-badge {
  background: var(--manga-import-surface-soft);
  color: var(--manga-import-text-amber-mid);
  font-weight: 500;
}

.book-chapters-table {
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--color-border-muted);
}

.chapter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--color-border-soft);
  font-size: 12px;
}

.chapter-row.table-header {
  background: var(--color-surface-quiet);
  font-weight: 600;
  color: var(--color-text-muted);
  font-size: 11px;
}

.chapter-row.row-warning {
  background: var(--manga-import-surface-faint-amber);
}

.col-ord {
  width: 60px;
}

.ordinal-input {
  width: 100%;
  padding: 2px 4px;
  border: 1px solid var(--color-border-default);
  border-radius: 4px;
  font-size: 12px;
  text-align: center;
}

.ordinal-input.input-empty {
  border-color: var(--manga-import-text-red);
  background: var(--manga-import-surface-faint-red);
}

.col-title {
  flex: 1;
}

.ch-title-input {
  width: 100%;
  padding: 2px 6px;
  border: 1px solid transparent;
  border-radius: 4px;
  font-size: 12px;
  background: transparent;
  color: var(--color-text-default);
}

.ch-title-input:hover,
.ch-title-input:focus {
  border-color: var(--color-border-default);
  background: var(--color-surface-base);
  outline: none;
}

.col-type {
  width: 64px;
}

.type-tag {
  font-size: 10px;
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--color-surface-muted);
  color: var(--color-text-secondary);
}

.col-layer {
  width: 120px;
}

.layer-badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  display: inline-block;
}

.badge-high {
  background: var(--manga-import-surface-whisper-teal);
  color: var(--manga-import-text-teal);
}

.badge-medium {
  background: var(--manga-import-surface-whisper-amber);
  color: var(--manga-import-text-amber);
}

.badge-low {
  background: var(--manga-import-surface-whisper-red);
  color: var(--manga-import-text-red-mid);
  font-weight: 500;
}

.col-pages {
  width: 50px;
  text-align: right;
  color: var(--color-text-muted);
  font-size: 11px;
}

.footer-left {
  display: flex;
  align-items: center;
  font-size: 13px;
  color: var(--color-text-muted);
}
</style>
