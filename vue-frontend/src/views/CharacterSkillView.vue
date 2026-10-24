<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppShell from '@/components/ui/AppShell.vue'
import MiyakoNavbar from '@/components/common/MiyakoNavbar.vue'
import BookSelector from '@/components/insight/BookSelector.vue'
import ProductStatusBanner from '@/components/product/ProductStatusBanner.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiNumberField from '@/components/ui/UiNumberField.vue'
import { getBookDetail } from '@/api/bookshelf'
import {
  buildInsightCharacterSkill,
  buildInsightLorebook,
  characterSkillArchiveUrl,
  getInsightCharacterSkill,
  getInsightCharacterSkillStatus,
  getInsightLorebook,
  getInsightLorebookStatus,
  listInsightCharacterSkills,
  lorebookArchiveUrl,
  type V2InsightCharacterSkill,
  type V2InsightCharacterSkillStatus,
  type V2InsightLorebook,
} from '@/api/v2/insight'
import { downloadBlob } from '@/api/download'
import { triggerBlobDownload } from '@/utils/browserDownload'
import type { BookData } from '@/types'

interface CharacterRow {
  entityId: string
  name: string
  pageCount: number
  chapterCount: number
  hasSkill: boolean
  updatedAt?: string | null
}

type SkillEntry = Record<string, unknown>

type SectionKey =
  | 'appearance'
  | 'personality'
  | 'speech_style'
  | 'relationships'
  | 'topics'
  | 'timeline'

const route = useRoute()
const router = useRouter()

const bookId = ref('')
const bookTitle = ref('')
const bookMeta = ref('')
const characters = ref<CharacterRow[]>([])
const selectedEntityId = ref('')
const skill = ref<V2InsightCharacterSkill | null>(null)
const task = ref<V2InsightCharacterSkillStatus | null>(null)
const isLoading = ref(false)
const isSkillLoading = ref(false)
const error = ref<string | null>(null)
const actionError = ref<string | null>(null)
const copied = ref('')
const minPages = ref<number | null>(3)
const isExporting = ref(false)
const activeKind = ref<'skill' | 'card'>('skill')
const charTrigger = '{{char}}'
const userTrigger = '{{user}}'
const card = ref<V2InsightCharacterSkill | null>(null)
const lorebook = ref<V2InsightLorebook | null>(null)
const lorebookTask = ref<V2InsightCharacterSkillStatus | null>(null)
const isLorebookLoading = ref(false)
const isLorebookExporting = ref(false)

let loadSequence = 0
let skillSequence = 0
let lorebookSequence = 0
let pollTimer: ReturnType<typeof setInterval> | null = null
let lorebookTimer: ReturnType<typeof setInterval> | null = null
let isMounted = false

const selectedCharacter = computed(
  () => characters.value.find(item => item.entityId === selectedEntityId.value) ?? null,
)

const isBuilding = computed(() => task.value?.status === 'running')

const progressPercent = computed(() => {
  const value = task.value
  if (!value || !value.total) return 0
  return Math.min(100, Math.round((value.done / value.total) * 100))
})

const activeDoc = computed(() => (activeKind.value === 'card' ? card.value : skill.value))

const kindLabel = computed(() => (activeKind.value === 'card' ? '角色卡' : '角色 Skill'))

const hasActiveDoc = computed(() => Boolean(activeDoc.value))

const summary = computed(() => activeDoc.value?.summary ?? null)

const isLorebookBuilding = computed(() => lorebookTask.value?.status === 'running')

const lorebookEntryCount = computed(() => {
  const stats = lorebook.value?.stats
  const value = stats && typeof stats === 'object' ? (stats as Record<string, unknown>).entries : null
  if (typeof value === 'number') return value
  const entries = lorebook.value?.summary
  const list = entries && typeof entries === 'object' ? (entries as Record<string, unknown>).entries : null
  return Array.isArray(list) ? list.length : 0
})

const cardFields = computed<Array<{ label: string; value: string }>>(() => {
  const data = activeDoc.value?.['card']?.data
  if (!data) return []
  const rows: Array<{ label: string; value: string }> = []
  const push = (label: string, value: unknown) => {
    const text =
      typeof value === 'string'
        ? value.trim()
        : Array.isArray(value)
          ? value.map(item => String(item ?? '')).filter(Boolean).join('、')
          : ''
    if (text) rows.push({ label, value: text })
  }
  push('角色名（name）', data.name)
  push('简介（description）', data.description)
  push('性格（personality）', data.personality)
  push('场景（scenario）', data.scenario)
  push('开场白（first_mes）', data.first_mes)
  push('对话示例（mes_example）', data.mes_example)
  push('系统提示（system_prompt）', data.system_prompt)
  push('历史后指令（post_history_instructions）', data.post_history_instructions)
  push('备用开场（alternate_greetings）', data.alternate_greetings)
  push('标签（tags）', data.tags)
  push('创建者备注（creator_notes）', data.creator_notes)
  return rows
})

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function routeBookId(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function entryHeadline(entry: SkillEntry): string {
  const candidate = entry.trait ?? entry.pattern ?? entry.event ?? entry.name ?? entry.note
  return typeof candidate === 'string' ? candidate : ''
}

function entryDetail(entry: SkillEntry): string {
  const parts: string[] = []
  for (const key of ['evidence', 'dynamic', 'relation', 'example', 'note']) {
    const value = entry[key]
    if (typeof value === 'string' && value.trim()) parts.push(value.trim())
  }
  return parts.join(' · ')
}

function entryCitation(entry: SkillEntry): string {
  const value = entry.citation
  return typeof value === 'string' ? value : ''
}

function entryConfidence(entry: SkillEntry): string {
  const value = entry.confidence
  return typeof value === 'string' ? value : ''
}

function entriesOf(key: SectionKey): SkillEntry[] {
  const value = summary.value?.[key]
  return Array.isArray(value) ? (value as SkillEntry[]) : []
}

const skillSections: Array<{ key: SectionKey; title: string }> = [
  { key: 'appearance', title: '外貌与服装' },
  { key: 'personality', title: '性格' },
  { key: 'speech_style', title: '说话方式' },
  { key: 'relationships', title: '人物关系' },
  { key: 'topics', title: '关联概念' },
  { key: 'timeline', title: '关键事件' },
]

function stopLorebookPolling(): void {
  if (lorebookTimer) {
    clearInterval(lorebookTimer)
    lorebookTimer = null
  }
}

async function loadLorebook(options: { silent?: boolean } = {}): Promise<void> {
  if (!bookId.value) return
  const sequence = ++lorebookSequence
  if (!options.silent) isLorebookLoading.value = true
  try {
    const document = await getInsightLorebook(bookId.value)
    if (!isMounted || sequence !== lorebookSequence) return
    lorebook.value = document
  } catch {
    if (!isMounted || sequence !== lorebookSequence) return
    lorebook.value = null
  } finally {
    if (isMounted && sequence === lorebookSequence) isLorebookLoading.value = false
  }
}

async function pollLorebookStatus(): Promise<void> {
  try {
    const current = await getInsightLorebookStatus(bookId.value)
    if (!isMounted) return
    lorebookTask.value = current
    if (current.status === 'ready') {
      stopLorebookPolling()
      await loadLorebook({ silent: true })
      return
    }
    if (current.status === 'failed') {
      stopLorebookPolling()
      actionError.value = current.error ?? '世界书生成失败'
    }
  } catch (pollError) {
    stopLorebookPolling()
    actionError.value = pollError instanceof Error ? pollError.message : '世界书状态读取失败'
  }
}

async function buildLorebook(): Promise<void> {
  if (!bookId.value || isLorebookBuilding.value) return
  actionError.value = null
  try {
    const started = await buildInsightLorebook(bookId.value, {})
    lorebookTask.value = started
    stopLorebookPolling()
    lorebookTimer = setInterval(() => {
      void pollLorebookStatus()
    }, 2000)
  } catch (buildError) {
    actionError.value = buildError instanceof Error ? buildError.message : '世界书生成失败'
  }
}

async function exportLorebook(): Promise<void> {
  if (!bookId.value || isLorebookExporting.value) return
  isLorebookExporting.value = true
  actionError.value = null
  try {
    const result = await downloadBlob({
      url: lorebookArchiveUrl(bookId.value),
      fallbackFilename: `${bookTitle.value || 'book'}-lorebook.zip`,
      fallbackErrorMessage: '导出世界书失败',
    })
    triggerBlobDownload(result.blob, result.filename)
  } catch (exportError) {
    actionError.value = exportError instanceof Error ? exportError.message : '导出世界书失败'
  } finally {
    if (isMounted) isLorebookExporting.value = false
  }
}

async function copyText(kind: string, text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = kind
    setTimeout(() => {
      if (isMounted && copied.value === kind) copied.value = ''
    }, 1600)
  } catch {
    actionError.value = '复制失败，请手动选择文本'
  }
}

async function loadSkill(entityId: string, options: { silent?: boolean } = {}): Promise<void> {
  if (!bookId.value || !entityId) return
  // 单独的序号：Skill 详情的加载不能推进书籍加载的序号，否则书籍加载的
  // finally 会认定自己已被取代，isLoading 永远停在 true。
  const kind = activeKind.value
  const sequence = ++skillSequence
  if (!options.silent) isSkillLoading.value = true
  try {
    const document = await getInsightCharacterSkill(bookId.value, entityId, { kind })
    if (!isMounted || sequence !== skillSequence) return
    if (kind === 'card') card.value = document
    else skill.value = document
    actionError.value = null
  } catch (loadError) {
    if (!isMounted || sequence !== skillSequence) return
    if (kind === 'card') {
      // 角色卡未生成是常态，用占位说明而不是错误横幅。
      card.value = null
    } else {
      skill.value = null
      if (!options.silent) {
        actionError.value = loadError instanceof Error ? loadError.message : '角色 Skill 读取失败'
      }
    }
  } finally {
    if (isMounted && sequence === skillSequence) isSkillLoading.value = false
  }
}

async function refreshCharacters(): Promise<void> {
  if (!bookId.value) return
  const rows = await listInsightCharacterSkills(bookId.value, { minPages: minPages.value ?? 0 })
  characters.value = rows.characters ?? []
  const stillListed = characters.value.some(item => item.entityId === selectedEntityId.value)
  if (!stillListed) {
    const fallback =
      characters.value.find(item => item.hasSkill) ?? characters.value[0] ?? null
    if (fallback) {
      selectedEntityId.value = fallback.entityId
      skill.value = null
      card.value = null
      if (fallback.hasSkill || activeKind.value === 'card') await loadSkill(fallback.entityId)
    }
  }
}

async function loadBook(targetBookId: string): Promise<void> {
  if (!targetBookId) return
  const sequence = ++loadSequence
  isLoading.value = true
  error.value = null
  stopPolling()
  task.value = null
  skill.value = null
  try {
    const [book, rows] = await Promise.all([
      getBookDetail(targetBookId),
      listInsightCharacterSkills(targetBookId, { minPages: minPages.value ?? 0 }),
    ])
    if (!isMounted || sequence !== loadSequence) return
    bookId.value = targetBookId
    applyBook(book)
    characters.value = rows.characters ?? []
    const preferred =
      characters.value.find(item => item.entityId === selectedEntityId.value && item.hasSkill) ??
      characters.value.find(item => item.hasSkill) ??
      characters.value[0]
    if (preferred) {
      selectedEntityId.value = preferred.entityId
      if (preferred.hasSkill || activeKind.value === 'card') await loadSkill(preferred.entityId)
    }
    await loadLorebook({ silent: true })
  } catch (loadError) {
    if (!isMounted || sequence !== loadSequence) return
    error.value = loadError instanceof Error ? loadError.message : '角色列表加载失败'
  } finally {
    if (isMounted && sequence === loadSequence) isLoading.value = false
  }
}

function applyBook(book: BookData): void {
  bookTitle.value = book.title
  bookMeta.value = `共 ${book.totalPages ?? 0} 页`
}

function handleBookSelect(targetBookId: string): void {
  if (!targetBookId) {
    void router.replace({ query: {} })
    return
  }
  void router.replace({ query: { ...route.query, book: targetBookId } })
}

async function selectCharacter(row: CharacterRow): Promise<void> {
  if (selectedEntityId.value === row.entityId && (skill.value || !row.hasSkill)) return
  selectedEntityId.value = row.entityId
  stopPolling()
  task.value = null
  if (row.hasSkill || activeKind.value === 'card') {
    await loadSkill(row.entityId)
  } else {
    skill.value = null
    card.value = null
    actionError.value = null
  }
}

function startPolling(entityId: string): void {
  stopPolling()
  pollTimer = setInterval(() => {
    void pollStatus(entityId)
  }, 2000)
}

async function pollStatus(entityId: string): Promise<void> {
  try {
    const current = await getInsightCharacterSkillStatus(bookId.value, entityId, {
      kind: activeKind.value,
    })
    if (!isMounted) return
    task.value = current
    if (current.status === 'ready') {
      stopPolling()
      await refreshCharacters()
      await loadSkill(entityId)
      return
    }
    if (current.status === 'failed') {
      stopPolling()
      actionError.value = current.error ?? '生成失败'
    }
  } catch (pollError) {
    stopPolling()
    actionError.value = pollError instanceof Error ? pollError.message : '生成状态读取失败'
  }
}

async function buildSkill(): Promise<void> {
  const row = selectedCharacter.value
  if (!row || isBuilding.value) return
  actionError.value = null
  try {
    const started = await buildInsightCharacterSkill(bookId.value, {
      entityId: row.entityId,
      kind: activeKind.value,
    })
    task.value = started
    startPolling(row.entityId)
  } catch (buildError) {
    actionError.value = buildError instanceof Error ? buildError.message : '生成失败'
  }
}

async function exportSkill(): Promise<void> {
  const row = selectedCharacter.value
  if (!row || !bookId.value || isExporting.value) return
  const entityId = row.entityId
  isExporting.value = true
  actionError.value = null
  try {
    const kind = activeKind.value
    const result = await downloadBlob({
      url: characterSkillArchiveUrl(bookId.value, entityId, kind),
      fallbackFilename: `${row.name}-${kind === 'card' ? 'card' : 'skill'}.zip`,
      fallbackErrorMessage: '导出压缩包失败',
    })
    triggerBlobDownload(result.blob, result.filename)
  } catch (exportError) {
    actionError.value = exportError instanceof Error ? exportError.message : '导出压缩包失败'
  } finally {
    if (isMounted) isExporting.value = false
  }
}

onMounted(() => {
  isMounted = true
  const target = routeBookId(route.query.book)
  if (target) void loadBook(target)
})

onUnmounted(() => {
  isMounted = false
  loadSequence += 1
  skillSequence += 1
  lorebookSequence += 1
  stopPolling()
  stopLorebookPolling()
})

watch(activeKind, () => {
  const entityId = selectedEntityId.value
  if (!isMounted || !bookId.value || !entityId) return
  const cached = activeKind.value === 'card' ? card.value : skill.value
  if (cached) return
  void loadSkill(entityId)
})

watch(minPages, () => {
  if (!isMounted || !bookId.value) return
  void refreshCharacters().catch(() => {
    /* 过滤失败时保留上一次列表，错误由页面级 banner 呈现 */
  })
})

watch(
  () => routeBookId(route.query.book),
  target => {
    if (!isMounted) return
    if (!target) {
      stopPolling()
      bookId.value = ''
      bookTitle.value = ''
      bookMeta.value = ''
      characters.value = []
      skill.value = null
      task.value = null
      return
    }
    if (target !== bookId.value) void loadBook(target)
  },
)
</script>

<template>
  <AppShell class="character-skill-page" viewport-mode="page">
    <MiyakoNavbar :book-title="bookTitle" />

    <main class="character-skill-main">
      <section class="character-skill-header">
        <div class="character-skill-header__left">
          <BookSelector :model-value="bookId" @select="handleBookSelect" />
          <span v-if="bookTitle" class="character-skill-header__meta">
            {{ bookMeta }} · 可蒸馏角色 {{ characters.length }} 个（出场 ≥
            {{ minPages ?? 0 }} 页）
          </span>
        </div>
        <label class="character-skill-filter">
          <span class="character-skill-filter__label">最少出场页数</span>
          <UiNumberField
            v-model="minPages"
            class="character-skill-filter__input"
            :min="0"
            :max="2000"
            size="sm"
            aria-label="最少出场页数"
          />
        </label>
      </section>

      <ProductStatusBanner
        v-if="isLoading"
        class="character-skill-banner"
        tone="neutral"
        title="正在读取角色档案"
        aria-live="polite"
      >
        正在从实体知识网络与页面分析结果中整理角色名单…
      </ProductStatusBanner>

      <ProductStatusBanner
        v-else-if="error"
        class="character-skill-banner"
        tone="danger"
        title="角色列表加载失败"
        aria-live="assertive"
      >
        {{ error }}
        <template #actions>
          <UiButton size="sm" variant="secondary" @click="loadBook(bookId)">重试</UiButton>
        </template>
      </ProductStatusBanner>

      <div v-else-if="!bookId" class="character-skill-empty">
        <div class="character-skill-empty__card">
          <span class="character-skill-empty__icon" aria-hidden="true">🎭</span>
          <h2 class="character-skill-empty__title">请选择一本漫画</h2>
          <p class="character-skill-empty__desc">
            选好书籍后，可以从实体知识网络中挑选角色，生成带人格层与页码引用的角色 Skill 档案。
          </p>
        </div>
      </div>

      <ProductStatusBanner
        v-else-if="characters.length === 0"
        class="character-skill-banner"
        tone="neutral"
        title="这本书还没有可蒸馏的角色"
      >
        请先在任务中心完成一次知识库构建（需要实体知识网络）。
      </ProductStatusBanner>

      <section v-if="characters.length > 0" class="character-skill-lorebook">
        <div class="character-skill-section__head">
          <h2 class="character-skill-section__title">世界书 Lorebook</h2>
          <span class="character-skill-lorebook__actions">
            <UiButton
              size="sm"
              variant="secondary"
              :loading="isLorebookBuilding"
              :disabled="isLorebookBuilding"
              @click="buildLorebook"
            >
              {{ lorebook ? '重新蒸馏世界书' : '蒸馏世界书' }}
            </UiButton>
            <UiButton
              v-if="lorebook"
              size="sm"
              variant="secondary"
              :loading="isLorebookExporting"
              :disabled="isLorebookExporting"
              @click="exportLorebook"
            >
              导出世界书 .zip
            </UiButton>
          </span>
        </div>
        <p v-if="isLorebookBuilding" class="character-skill-section__text">
          正在整理全书实体与设定 · {{ lorebookTask?.stage ?? '处理中' }}
          {{ lorebookTask?.done ?? 0 }}/{{ lorebookTask?.total ?? 0 }}
        </p>
        <p v-else-if="lorebook" class="character-skill-section__text">
          已生成 {{ lorebookEntryCount }} 条词条，触发词 <code>{{ charTrigger }}</code> /
          <code>{{ userTrigger }}</code>，可直接导入酒馆类工具；导出的压缩包内含
          <code>lorebook.json</code> 与逐条 Markdown。
        </p>
        <p v-else-if="!isLorebookLoading" class="character-skill-section__text">
          还没有世界书。世界书会把这本书的实体、设定与关键事件整理成可触发的条目。
        </p>
      </section>

      <section v-if="characters.length > 0" class="character-skill-body">
        <aside class="character-skill-list" aria-label="角色列表">
          <UiButton
            v-for="row in characters"
            :key="row.entityId"
            variant="ghost"
            class="character-skill-list__item"
            :class="{ 'character-skill-list__item--active': row.entityId === selectedEntityId }"
            @click="selectCharacter(row)"
          >
            <span class="character-skill-list__name">{{ row.name }}</span>
            <span class="character-skill-list__meta">
              {{ row.pageCount }} 页 · {{ row.chapterCount }} 话
              <span v-if="row.hasSkill" class="character-skill-list__badge">已生成</span>
            </span>
          </UiButton>
        </aside>

        <article class="character-skill-detail">
          <ProductStatusBanner
            v-if="actionError"
            class="character-skill-banner"
            tone="danger"
            title="操作失败"
            aria-live="assertive"
          >
            {{ actionError }}
          </ProductStatusBanner>

          <ProductStatusBanner
            v-if="isBuilding"
            class="character-skill-banner"
            tone="neutral"
            title="正在蒸馏角色档案"
            aria-live="polite"
          >
            {{ task?.stage ?? '处理中' }} · {{ task?.done ?? 0 }}/{{ task?.total ?? 0 }}（{{ progressPercent }}%）
          </ProductStatusBanner>

          <template v-if="selectedCharacter">
            <header class="character-skill-detail__header">
              <div>
                <h1 class="character-skill-detail__title">{{ selectedCharacter.name }}</h1>
                <p class="character-skill-detail__subtitle">
                  {{ selectedCharacter.pageCount }} 页 · {{ selectedCharacter.chapterCount }} 话出场
                  <span v-if="activeDoc?.stats">
                    · 事件 {{ activeDoc.stats.events ?? 0 }} 条 · 原画观察
                    {{ activeDoc.stats.visionPages }} 页
                  </span>
                </p>
              </div>
              <UiButton variant="primary" :disabled="isBuilding" @click="buildSkill">
                {{ hasActiveDoc ? '重新生成' : `生成${kindLabel}` }}
              </UiButton>
            </header>

            <div class="character-skill-kind-tabs" role="tablist" aria-label="蒸馏产物类型">
              <UiButton
                variant="tab"
                class="character-skill-kind-tabs__btn"
                :class="{ 'character-skill-kind-tabs__btn--active': activeKind === 'skill' }"
                role="tab"
                :aria-selected="activeKind === 'skill'"
                @click="activeKind = 'skill'"
              >
                🎭 角色 Skill
              </UiButton>
              <UiButton
                variant="tab"
                class="character-skill-kind-tabs__btn"
                :class="{ 'character-skill-kind-tabs__btn--active': activeKind === 'card' }"
                role="tab"
                :aria-selected="activeKind === 'card'"
                @click="activeKind = 'card'"
              >
                🃏 角色卡
              </UiButton>
            </div>

            <ProductStatusBanner
              v-if="isSkillLoading"
              class="character-skill-banner"
              tone="neutral"
              title="正在加载 Skill 档案"
              aria-live="polite"
            />

            <div
              v-else-if="!activeDoc"
              class="character-skill-empty__card character-skill-detail__placeholder"
            >
              <p class="character-skill-empty__desc">
                还没有为「{{ selectedCharacter.name }}」生成{{ kindLabel }}。点击右上角按钮开始蒸馏，
                系统会读取该角色的页面分析证据与原画页面，产出带页码引用的档案。
              </p>
            </div>

            <template v-else>
              <template v-if="activeKind === 'skill'">
                <section v-if="summary?.one_liner" class="character-skill-section">
                  <h2 class="character-skill-section__title">一句话</h2>
                  <p class="character-skill-section__text">{{ summary.one_liner }}</p>
                </section>

                <section v-if="summary?.persona_prompt" class="character-skill-section">
                  <div class="character-skill-section__head">
                    <h2 class="character-skill-section__title">扮演提示</h2>
                    <UiButton size="sm" variant="ghost" @click="copyText('persona', String(summary.persona_prompt))">
                      {{ copied === 'persona' ? '已复制' : '复制' }}
                    </UiButton>
                  </div>
                  <p class="character-skill-section__text">{{ summary.persona_prompt }}</p>
                </section>

                <section
                  v-for="block in skillSections"
                  :key="block.key"
                  class="character-skill-section"
                >
                  <template v-if="entriesOf(block.key).length > 0">
                    <h2 class="character-skill-section__title">{{ block.title }}</h2>
                    <ul class="character-skill-entries">
                      <li
                        v-for="(entry, index) in entriesOf(block.key)"
                        :key="`${block.key}-${index}`"
                        class="character-skill-entries__item"
                      >
                        <span class="character-skill-entries__headline">{{ entryHeadline(entry) }}</span>
                        <span v-if="entryDetail(entry)" class="character-skill-entries__detail">
                          {{ entryDetail(entry) }}
                        </span>
                        <span class="character-skill-entries__foot">
                          <span v-if="entryConfidence(entry)" class="character-skill-entries__confidence">
                            {{ entryConfidence(entry) }}
                          </span>
                          <span class="character-skill-entries__citation">{{ entryCitation(entry) }}</span>
                        </span>
                      </li>
                    </ul>
                  </template>
                </section>

                <section v-if="summary?.unknowns?.length" class="character-skill-section">
                  <h2 class="character-skill-section__title">资料缺口</h2>
                  <ul class="character-skill-entries">
                    <li v-for="(item, index) in summary.unknowns" :key="`unknown-${index}`" class="character-skill-entries__item">
                      <span class="character-skill-entries__headline">{{ item }}</span>
                    </li>
                  </ul>
                </section>
              </template>

              <template v-else>
                <section class="character-skill-section">
                  <h2 class="character-skill-section__title">角色卡字段（chara_card_v3）</h2>
                  <ul v-if="cardFields.length" class="character-skill-entries">
                    <li
                      v-for="field in cardFields"
                      :key="field.label"
                      class="character-skill-entries__item"
                    >
                      <span class="character-skill-entries__headline">{{ field.label }}</span>
                      <span class="character-skill-entries__detail">{{ field.value }}</span>
                    </li>
                  </ul>
                  <p v-else class="character-skill-section__text">角色卡还没有内容。</p>
                </section>

                <section v-if="summary?.evidence?.length" class="character-skill-section">
                  <h2 class="character-skill-section__title">证据</h2>
                  <ul class="character-skill-entries">
                    <li
                      v-for="(item, index) in summary.evidence"
                      :key="`card-evidence-${index}`"
                      class="character-skill-entries__item"
                    >
                      <span class="character-skill-entries__headline">{{ item.claim }}</span>
                      <span class="character-skill-entries__citation">{{ item.citation }}</span>
                    </li>
                  </ul>
                </section>
              </template>

              <section
                v-if="activeDoc.files && Object.keys(activeDoc.files).length > 0"
                class="character-skill-section"
              >
                <div class="character-skill-section__head">
                  <h2 class="character-skill-section__title">可导出的文件</h2>
                  <UiButton
                    size="sm"
                    variant="secondary"
                    :loading="isExporting"
                    :disabled="isExporting"
                    @click="exportSkill"
                  >
                    导出为 .zip 压缩包
                  </UiButton>
                </div>
                <ul class="character-skill-files">
                  <li
                    v-for="(content, path) in activeDoc.files"
                    :key="path"
                    class="character-skill-files__item"
                  >
                    <code class="character-skill-files__path">{{ path }}</code>
                    <UiButton size="sm" variant="ghost" @click="copyText(String(path), String(content))">
                      {{ copied === String(path) ? '已复制' : '复制内容' }}
                    </UiButton>
                  </li>
                </ul>
              </section>
            </template>
          </template>
        </article>
      </section>
    </main>
  </AppShell>
</template>

<style scoped>
.character-skill-page {
  --character-skill-shadow-bar: 0 1px 3px rgba(0, 0, 0, 0.04);
  --character-skill-shadow-card: 0 2px 8px rgba(0, 0, 0, 0.05);

  background-color: var(--color-bg-base);
  color: var(--color-text-primary);
  display: flex;
  flex-direction: column;
}

.character-skill-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  max-width: 1440px;
  width: 100%;
  margin: 0 auto;
  padding: 16px 24px 32px;
  gap: 16px;
}

.character-skill-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 12px 20px;
  box-shadow: var(--character-skill-shadow-bar);
}

.character-skill-header__left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.character-skill-header__meta {
  font-size: 0.88rem;
  color: var(--color-text-secondary);
}

.character-skill-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.character-skill-filter__label {
  font-size: 0.85rem;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.character-skill-filter__input {
  width: 96px;
}

.character-skill-banner {
  margin-bottom: 8px;
}

.character-skill-body {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(200px, 280px) minmax(0, 1fr);
  gap: 16px;
  min-height: 0;
}

.character-skill-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  overflow-y: auto;
  max-height: 70vh;
}

.character-skill-list__item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 8px 10px;
  border-radius: 8px;
  text-align: left;
}

.character-skill-list__item--active {
  background-color: var(--color-surface-subtle);
}

.character-skill-list__name {
  font-weight: 600;
  font-size: 0.94rem;
  color: var(--color-text-primary);
}

.character-skill-list__meta {
  font-size: 0.78rem;
  color: var(--color-text-secondary);
}

.character-skill-list__badge {
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 999px;
  background-color: var(--color-surface-brand);
  color: var(--color-text-brand);
  font-size: 0.72rem;
}

.character-skill-detail {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 20px 24px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  box-shadow: var(--character-skill-shadow-card);
  overflow-y: auto;
  max-height: 70vh;
}

.character-skill-detail__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.character-skill-detail__title {
  font-size: 1.35rem;
  font-weight: 650;
  margin: 0 0 4px;
}

.character-skill-detail__subtitle {
  margin: 0;
  font-size: 0.85rem;
  color: var(--color-text-secondary);
}

.character-skill-detail__placeholder {
  margin-top: 8px;
}

.character-skill-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 10px;
  border-top: 1px solid var(--color-border);
}

.character-skill-section__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.character-skill-section__title {
  font-size: 1rem;
  font-weight: 600;
  margin: 0;
}

.character-skill-section__text {
  margin: 0;
  font-size: 0.92rem;
  line-height: 1.65;
  color: var(--color-text-primary);
  white-space: pre-wrap;
}

.character-skill-entries {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.character-skill-entries__item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  border-radius: 8px;
  background-color: var(--color-surface-subtle);
}

.character-skill-entries__headline {
  font-size: 0.9rem;
  font-weight: 550;
}

.character-skill-entries__detail {
  font-size: 0.85rem;
  color: var(--color-text-secondary);
}

.character-skill-entries__foot {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 0.76rem;
  color: var(--color-text-secondary);
}

.character-skill-entries__confidence {
  padding: 0 6px;
  border-radius: 999px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
}

.character-skill-files {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.character-skill-files__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 10px;
  border-radius: 8px;
  background-color: var(--color-surface-subtle);
}

.character-skill-files__path {
  font-size: 0.82rem;
  color: var(--color-text-secondary);
}

.character-skill-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 64px 20px;
}

.character-skill-empty__card {
  text-align: center;
  max-width: 520px;
  padding: 40px;
  background-color: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 16px;
  box-shadow: var(--character-skill-shadow-card);
}

.character-skill-empty__icon {
  font-size: 3rem;
  display: inline-block;
  margin-bottom: 16px;
}

.character-skill-empty__title {
  font-size: 1.25rem;
  font-weight: 600;
  margin-bottom: 8px;
}

.character-skill-empty__desc {
  font-size: 0.92rem;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.character-skill-kind-tabs {
  display: flex;
  gap: 4px;
  margin: 12px 0 4px;
  border-bottom: 1px solid var(--color-border);
}

.character-skill-kind-tabs__btn--active {
  color: var(--color-text-brand);
  box-shadow: inset 0 -2px 0 var(--color-brand-primary);
}

.character-skill-lorebook {
  margin: 16px 0;
  padding: 16px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background-color: var(--color-surface);
}

.character-skill-lorebook__actions {
  display: flex;
  gap: 8px;
}
</style>
