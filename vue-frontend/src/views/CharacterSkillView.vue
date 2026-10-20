<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppShell from '@/components/ui/AppShell.vue'
import MiyakoNavbar from '@/components/common/MiyakoNavbar.vue'
import BookSelector from '@/components/insight/BookSelector.vue'
import ProductStatusBanner from '@/components/product/ProductStatusBanner.vue'
import UiButton from '@/components/ui/UiButton.vue'
import { getBookDetail } from '@/api/bookshelf'
import {
  buildInsightCharacterSkill,
  getInsightCharacterSkill,
  getInsightCharacterSkillStatus,
  listInsightCharacterSkills,
  type V2InsightCharacterSkill,
  type V2InsightCharacterSkillStatus,
} from '@/api/v2/insight'
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

let loadSequence = 0
let pollTimer: ReturnType<typeof setInterval> | null = null
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

const summary = computed(() => skill.value?.summary ?? null)

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
  const sequence = ++loadSequence
  if (!options.silent) isSkillLoading.value = true
  try {
    const document = await getInsightCharacterSkill(bookId.value, entityId)
    if (!isMounted || sequence !== loadSequence) return
    skill.value = document
    actionError.value = null
  } catch (loadError) {
    if (!isMounted || sequence !== loadSequence) return
    skill.value = null
    if (!options.silent) {
      actionError.value = loadError instanceof Error ? loadError.message : '角色 Skill 读取失败'
    }
  } finally {
    if (isMounted && sequence === loadSequence) isSkillLoading.value = false
  }
}

async function refreshCharacters(): Promise<void> {
  if (!bookId.value) return
  const rows = await listInsightCharacterSkills(bookId.value)
  characters.value = rows.characters ?? []
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
      listInsightCharacterSkills(targetBookId),
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
      if (preferred.hasSkill) await loadSkill(preferred.entityId)
    }
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
  if (row.hasSkill) {
    await loadSkill(row.entityId)
  } else {
    skill.value = null
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
    const current = await getInsightCharacterSkillStatus(bookId.value, entityId)
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
    const started = await buildInsightCharacterSkill(bookId.value, { entityId: row.entityId })
    task.value = started
    startPolling(row.entityId)
  } catch (buildError) {
    actionError.value = buildError instanceof Error ? buildError.message : '生成失败'
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
  stopPolling()
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
            {{ bookMeta }} · 可蒸馏角色 {{ characters.length }} 个
          </span>
        </div>
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

      <section v-else class="character-skill-body">
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
                  <span v-if="skill?.stats">
                    · 事件 {{ skill.stats.events }} 条 · 原画观察 {{ skill.stats.visionPages }} 页
                  </span>
                </p>
              </div>
              <UiButton
                variant="primary"
                :disabled="isBuilding"
                @click="buildSkill"
              >
                {{ selectedCharacter.hasSkill ? '重新生成' : '生成角色 Skill' }}
              </UiButton>
            </header>

            <ProductStatusBanner
              v-if="isSkillLoading"
              class="character-skill-banner"
              tone="neutral"
              title="正在加载 Skill 档案"
              aria-live="polite"
            />

            <div v-else-if="!skill" class="character-skill-empty__card character-skill-detail__placeholder">
              <p class="character-skill-empty__desc">
                还没有为「{{ selectedCharacter.name }}」生成 Skill。点击右上角按钮开始蒸馏，
                系统会读取该角色的页面分析证据与原画页面，产出带页码引用的人格档案。
              </p>
            </div>

            <template v-else>
              <section v-if="summary" class="character-skill-section">
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

              <section v-if="skill.files && Object.keys(skill.files).length > 0" class="character-skill-section">
                <h2 class="character-skill-section__title">可导出的 Skill 文件</h2>
                <ul class="character-skill-files">
                  <li v-for="(content, path) in skill.files" :key="path" class="character-skill-files__item">
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
</style>
