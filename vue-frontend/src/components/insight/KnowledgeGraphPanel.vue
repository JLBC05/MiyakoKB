<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import type { Graph as G6Graph } from '@antv/g6'
import { useInsightStore } from '@/stores/insightStore'
import { useBookshelfStore } from '@/stores/bookshelfStore'
import {
  getInsightGraph,
  getInsightGraphForBooks,
  type V2InsightGraphNode,
  type V2InsightGraphEdge,
} from '@/api/v2/insight'
import UiButton from '@/components/ui/UiButton.vue'
import UiInput from '@/components/ui/UiInput.vue'
import UiNumberField from '@/components/ui/UiNumberField.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import type { UiIconName } from '@/components/ui/iconRegistry'

const route = useRoute()
const insightStore = useInsightStore()
const bookshelfStore = useBookshelfStore()

const MAX_MULTI_BOOKS = 10

type GraphNodeWithBook = V2InsightGraphNode & { bookId?: string }
type GraphEdgeWithBook = V2InsightGraphEdge & { bookId?: string }

interface BookGroupMeta {
  bookId: string
  bookTitle: string
  color: string
}

// 多书对比时用于区分不同书的边框颜色（与类别填充色叠加，不冲突）。
const BOOK_BORDER_COLORS = ['#9977CC', '#2F6F8F', '#B26A00', '#2E7D32', '#8A4F87', '#C0392B', '#16A085', '#7F8C8D', '#D35400', '#2C3E50']

const isLoading = ref(false)
const errorMessage = ref('')
const rawNodes = ref<GraphNodeWithBook[]>([])
const rawEdges = ref<GraphEdgeWithBook[]>([])
const bookGroups = ref<BookGroupMeta[]>([])

const multiBookMode = ref(false)
const selectedBookIds = ref<string[]>([])

const selectedCategory = ref<string>('all')
const minWeight = ref<number | null>(2)
const searchQuery = ref<string>('')
const showLabels = ref<boolean>(true)
const isSimplifiedMode = ref<boolean>(true)
const maxSimplifiedNodes = 60

const selectedNodeId = ref<string | null>(null)
const selectedEdgeId = ref<string | null>(null)

const graphContainerRef = ref<HTMLDivElement | null>(null)
let graph: G6Graph | null = null
let isMounted = false
let renderSequence = 0

const categories: Array<{ key: string; label: string; icon: UiIconName; color: string }> = [
  { key: 'all', label: '全部', icon: 'globe', color: '#9A9AA2' },
  { key: 'characters', label: '角色', icon: 'users', color: '#9977CC' },
  { key: 'abilities', label: '能力', icon: 'target', color: '#B26A00' },
  { key: 'items', label: '物品', icon: 'folder-open', color: '#2E7D32' },
  { key: 'locations', label: '地点', icon: 'map-pin', color: '#2F6F8F' },
  { key: 'concepts', label: '概念', icon: 'sparkles', color: '#8A4F87' },
]

function getCategoryColor(category: string): string {
  const match = categories.find(item => item.key === category)
  return match ? match.color : '#94a3b8'
}

function getCategoryLabel(category: string): string {
  const match = categories.find(item => item.key === category)
  return match ? match.label : category
}

function getBookBorderColor(bookId: string | undefined): string {
  if (!bookId) return '#ffffff'
  const index = bookGroups.value.findIndex(group => group.bookId === bookId)
  if (index < 0) return '#ffffff'
  return BOOK_BORDER_COLORS[index % BOOK_BORDER_COLORS.length]
}

function toggleBookSelection(bookId: string): void {
  const current = selectedBookIds.value
  if (current.includes(bookId)) {
    selectedBookIds.value = current.filter(id => id !== bookId)
    return
  }
  if (current.length >= MAX_MULTI_BOOKS) return
  selectedBookIds.value = [...current, bookId]
}

const categoryCounts = computed(() => {
  const map: Record<string, number> = { all: rawNodes.value.length }
  for (const item of categories) {
    if (item.key !== 'all') map[item.key] = 0
  }
  for (const node of rawNodes.value) {
    if (map[node.category] !== undefined) map[node.category]++
  }
  return map
})

const nodeIndex = computed(() => {
  const map = new Map<string, V2InsightGraphNode>()
  for (const node of rawNodes.value) map.set(node.id, node)
  return map
})

const activeNodes = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  let list = rawNodes.value.filter(node => {
    if (selectedCategory.value !== 'all' && node.category !== selectedCategory.value) return false
    if (!query) return true
    const name = String(node.name ?? '').toLowerCase()
    const aliases = (node.aliases ?? []).map(alias => String(alias).toLowerCase())
    return name.includes(query) || aliases.some(alias => alias.includes(query))
  })
  if (isSimplifiedMode.value && list.length > maxSimplifiedNodes) {
    list = [...list].sort((a, b) => (b.frequency ?? 0) - (a.frequency ?? 0)).slice(0, maxSimplifiedNodes)
  }
  return list
})

const activeNodeIds = computed(() => new Set(activeNodes.value.map(node => node.id)))

const activeEdges = computed(() => {
  const threshold = minWeight.value ?? 0
  return rawEdges.value.filter(edge => {
    if (!activeNodeIds.value.has(edge.source) || !activeNodeIds.value.has(edge.target)) return false
    return (edge.weight ?? 0) >= threshold
  })
})

const averageDegree = computed(() => {
  if (!activeNodes.value.length) return '0.0'
  const degree = (activeEdges.value.length * 2) / activeNodes.value.length
  return degree.toFixed(1)
})

const selectedNodeDetail = computed(() => {
  const id = selectedNodeId.value
  if (!id) return null
  const node = nodeIndex.value.get(id)
  if (!node) return null
  const neighbors: Array<{ node: V2InsightGraphNode; weight: number }> = []
  for (const edge of rawEdges.value) {
    if (edge.source === id) {
      const other = nodeIndex.value.get(edge.target)
      if (other) neighbors.push({ node: other, weight: edge.weight ?? 0 })
    } else if (edge.target === id) {
      const other = nodeIndex.value.get(edge.source)
      if (other) neighbors.push({ node: other, weight: edge.weight ?? 0 })
    }
  }
  neighbors.sort((a, b) => b.weight - a.weight)
  return { node, topNeighbors: neighbors.slice(0, 10), degree: neighbors.length }
})

const selectedEdgeDetail = computed(() => {
  const id = selectedEdgeId.value
  if (!id) return null
  const edge = rawEdges.value.find(item => item.id === id)
  if (!edge) return null
  return {
    edge,
    source: nodeIndex.value.get(edge.source) ?? null,
    target: nodeIndex.value.get(edge.target) ?? null,
  }
})

const hasGraph = computed(() => rawNodes.value.length > 0)

function nodeDiameter(frequency: number | undefined, maxFrequency: number): number {
  const normalized = Math.min(1, Math.max(0, Math.log2((frequency ?? 1) + 1) / Math.log2(maxFrequency + 1)))
  return Math.round(18 + normalized * 34)
}

function buildGraphData(): { nodes: unknown[]; edges: unknown[] } {
  const maxFrequency = Math.max(...rawNodes.value.map(node => node.frequency ?? 1), 1)
  const nodes = activeNodes.value.map(node => {
    const size = nodeDiameter(node.frequency, maxFrequency)
    return {
      id: node.id,
      // 直径同时写进 data.size（碰撞检测用）与 style.size（渲染用）。
      data: { ...node, size },
      style: {
        size,
        fill: getCategoryColor(node.category),
        labelText: showLabels.value ? node.name : '',
        // 多书对比时，用边框颜色区分不同书（单书模式保持白色边框）。
        stroke: multiBookMode.value ? getBookBorderColor((node as GraphNodeWithBook).bookId) : '#ffffff',
        lineWidth: multiBookMode.value ? 3 : 1.5,
      },
    }
  })
  const edges = activeEdges.value.map(edge => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    data: edge,
    style: { lineWidth: Math.min(3, 0.5 + (edge.weight ?? 0) * 0.25) },
  }))
  return { nodes, edges }
}

async function renderGraph(): Promise<void> {
  const container = graphContainerRef.value
  if (!container) return
  const sequence = ++renderSequence
  if (!graph) {
    // G6 占图谱 chunk 的绝大部分体积，只有真正要画图时才加载它。
    const { Graph } = await import('@antv/g6')
    if (sequence !== renderSequence) return
    graph = new Graph({
      container,
      autoResize: true,
      background: 'transparent',
      padding: 24,
      data: buildGraphData() as never,
      node: {
        style: {
          labelPlacement: 'bottom',
          labelFontSize: 11,
          labelFill: '#334155',
          labelBackground: true,
          labelBackgroundFill: 'rgba(255, 255, 255, 0.86)',
          labelBackgroundRadius: 3,
          stroke: '#ffffff',
          lineWidth: 1.5,
          cursor: 'pointer',
        },
      },
      edge: {
        style: {
          stroke: '#cbd5e1',
          opacity: 0.55,
          cursor: 'pointer',
        },
      },
      // 关掉图级动画：G6 的 graphLayout() 用 options.animation 决定是「动画迭代」
      // 还是「一次算完再落位」，只设 layout.animate 不起作用（实测节点会连动 30 秒）。
      animation: false,
      layout: {
        type: 'force',
        preventOverlap: true,
        // 碰撞半径必须与真实节点尺寸一致：布局默认按固定 nodeSize(30) 做碰撞检测，
        // 而这里的节点直径是 18~52，所以大节点会盖住邻居。
        nodeSize: (node: { style?: { size?: number }; data?: { size?: number } }) =>
          Math.max(16, Number(node?.style?.size ?? node?.data?.size ?? 24)),
        // 再加一点间距，让标签不至于贴到别人身上。
        nodeSpacing: 6,
        // 默认质量按度数放大（degree*5），中心节点会重到碰撞力推不动（碰撞位移乘 1/mass），
        // 于是高热节点互相压在一起。这里统一质量，让碰撞对所有节点同等有效。
        getMass: () => 1,
        collideStrength: 1,
        // 库仑斥力系数：默认 1 时高连接度的中心区还会剩几对贴在一起，3 倍即可完全铺开（实测重叠 5 → 1）。
        factor: 3,
        // 关掉动画时 G6 走的是「同步跑固定轮数」分支，轮数取自这里的 iterations（默认仅 300）。
        // 边密集时 300 轮不足以把碰撞解开，会留下重叠节点。
        iterations: 1500,
        animate: false,
        // 其余（linkDistance / nodeStrength / edgeStrength / gravity / damping）一律沿用
        // @antv/layout 的默认值：默认 linkDistance = 1 + r(源) + r(目标) 本身就是尺寸感知的，
        // 手工覆盖反而会把图压成一团（实测 nodeStrength 取负值把相连节点互相推开，重叠从 6 对涨到 313 对）。
      },
      behaviors: ['zoom-canvas', 'drag-canvas', 'drag-element', 'hover-activate'],
    })
    graph.on('node:click', event => {
      const id = String((event as { target?: { id?: string } }).target?.id ?? '')
      if (!id) return
      selectedEdgeId.value = null
      selectedNodeId.value = id
    })
    graph.on('edge:click', event => {
      const id = String((event as { target?: { id?: string } }).target?.id ?? '')
      if (!id) return
      selectedNodeId.value = null
      selectedEdgeId.value = id
    })
    graph.on('canvas:click', () => {
      selectedNodeId.value = null
      selectedEdgeId.value = null
    })
  }
  graph.setData(buildGraphData() as never)
  await graph.render()
  if (sequence !== renderSequence) return
  await graph.fitView()
}

function resetView(): void {
  if (!graph) return
  void graph.fitView()
}

async function loadGraphData(): Promise<void> {
  const queryBook = typeof route?.query?.book === 'string' ? route.query.book.trim() : ''
  const singleBookId = insightStore.currentBookId || queryBook

  // 多书对比模式：需要至少两本已选书。
  if (multiBookMode.value && selectedBookIds.value.length > 1) {
    isLoading.value = true
    errorMessage.value = ''
    try {
      const data = await getInsightGraphForBooks(selectedBookIds.value, { minWeight: 1 })
      if (!isMounted) return
      const groups = 'groups' in data ? data.groups : []
      const nodes: GraphNodeWithBook[] = []
      const edges: GraphEdgeWithBook[] = []
      const meta: BookGroupMeta[] = []
      groups.forEach((group, index) => {
        meta.push({
          bookId: group.bookId,
          bookTitle: group.bookTitle || group.bookId,
          color: BOOK_BORDER_COLORS[index % BOOK_BORDER_COLORS.length],
        })
        for (const node of group.nodes ?? []) nodes.push({ ...node, bookId: group.bookId })
        for (const edge of group.edges ?? []) edges.push({ ...edge, bookId: group.bookId })
      })
      rawNodes.value = nodes
      rawEdges.value = edges
      bookGroups.value = meta
      selectedNodeId.value = null
      selectedEdgeId.value = null
      await nextTick()
      await renderGraph()
    } catch (error) {
      if (!isMounted) return
      errorMessage.value = error instanceof Error ? error.message : '加载多书关系图谱数据失败'
    } finally {
      if (isMounted) isLoading.value = false
    }
    return
  }

  bookGroups.value = []
  if (!singleBookId) {
    rawNodes.value = []
    rawEdges.value = []
    selectedNodeId.value = null
    selectedEdgeId.value = null
    return
  }
  isLoading.value = true
  errorMessage.value = ''
  try {
    const data = await getInsightGraph(singleBookId, { minWeight: 1 })
    if (!isMounted) return
    rawNodes.value = data.nodes ?? []
    rawEdges.value = data.edges ?? []
    selectedNodeId.value = null
    selectedEdgeId.value = null
    await nextTick()
    await renderGraph()
  } catch (error) {
    if (!isMounted) return
    errorMessage.value = error instanceof Error ? error.message : '加载关系图谱数据失败'
  } finally {
    if (isMounted) isLoading.value = false
  }
}

watch(
  () => [selectedCategory.value, minWeight.value, searchQuery.value, isSimplifiedMode.value, showLabels.value],
  () => {
    if (!isMounted || !graph) return
    void renderGraph()
  },
)

// 切换多书对比模式或改选书目时，需要重新拉取数据（数据结构从扁平变为分组）。
watch(
  () => [multiBookMode.value, selectedBookIds.value.join(',')],
  () => {
    if (!isMounted) return
    void loadGraphData()
  },
)

async function toggleMultiBookMode(): Promise<void> {
  multiBookMode.value = !multiBookMode.value
  if (multiBookMode.value && selectedBookIds.value.length === 0) {
    // 默认带上当前书（若已在书架列表中）。
    const current = insightStore.currentBookId
    if (current) selectedBookIds.value = [current]
    if (bookshelfStore.books.length === 0) await bookshelfStore.loadBooks()
  } else if (!multiBookMode.value) {
    selectedBookIds.value = []
  }
}

onMounted(() => {
  isMounted = true
  if (bookshelfStore.books.length === 0) void bookshelfStore.loadBooks()
  void loadGraphData()
})

onUnmounted(() => {
  isMounted = false
  renderSequence += 1
  selectedNodeId.value = null
  selectedEdgeId.value = null
  if (graph) {
    graph.destroy()
    graph = null
  }
})
</script>

<template>
  <div class="knowledge-graph-panel">
    <div class="knowledge-graph-panel__toolbar">
      <div class="knowledge-graph-panel__categories">
        <UiButton
          v-for="category in categories"
          :key="category.key"
          size="sm"
          :variant="selectedCategory === category.key ? 'primary' : 'secondary'"
          @click="selectedCategory = category.key"
        >
          <UiIcon :name="category.icon" :size="14" /> {{ category.label }}
          <span class="knowledge-graph-panel__count">{{ categoryCounts[category.key] ?? 0 }}</span>
        </UiButton>
      </div>

      <div class="knowledge-graph-panel__controls">
        <UiInput
          v-model="searchQuery"
          class="knowledge-graph-panel__search"
          type="search"
          placeholder="搜索实体或别名"
          aria-label="搜索实体或别名"
          clearable
        />
        <label class="knowledge-graph-panel__field">
          <span>最小共现</span>
          <UiNumberField
            v-model="minWeight"
            class="knowledge-graph-panel__number"
            :min="0"
            :max="100"
            size="sm"
            aria-label="最小共现次数"
          />
        </label>
        <UiButton size="sm" variant="secondary" @click="isSimplifiedMode = !isSimplifiedMode">
          {{ isSimplifiedMode ? '简洁视图' : '完整视图' }}
        </UiButton>
        <UiButton size="sm" variant="secondary" @click="showLabels = !showLabels">
          {{ showLabels ? '隐藏标签' : '显示标签' }}
        </UiButton>
        <UiButton size="sm" variant="secondary" @click="resetView">重置视图</UiButton>
        <UiButton
          size="sm"
          :variant="multiBookMode ? 'primary' : 'secondary'"
          @click="toggleMultiBookMode"
        >
          <UiIcon name="layers" :size="14" /> 多书对比
        </UiButton>
      </div>
    </div>

    <div v-if="multiBookMode" class="knowledge-graph-panel__book-picker">
      <span class="knowledge-graph-panel__book-picker-label">
        选择书籍（最多 {{ MAX_MULTI_BOOKS }} 本，已选 {{ selectedBookIds.length }}）
      </span>
      <div class="knowledge-graph-panel__book-list">
        <UiButton
          v-for="book in bookshelfStore.books"
          :key="book.id"
          size="xs"
          :variant="selectedBookIds.includes(book.id) ? 'primary' : 'secondary'"
          class="knowledge-graph-panel__book-chip"
          :disabled="!selectedBookIds.includes(book.id) && selectedBookIds.length >= MAX_MULTI_BOOKS"
          @click="toggleBookSelection(book.id)"
        >
          {{ book.title || book.id }}
        </UiButton>
      </div>
    </div>

    <div class="knowledge-graph-panel__stats">
      <span>节点 {{ activeNodes.length }}</span>
      <span>关系 {{ activeEdges.length }}</span>
      <span>平均度 {{ averageDegree }}</span>
      <span v-for="group in bookGroups" :key="group.bookId" class="knowledge-graph-panel__book-legend">
        <span class="knowledge-graph-panel__book-swatch" :style="{ backgroundColor: group.color }" />
        {{ group.bookTitle }}
      </span>
      <span class="knowledge-graph-panel__hint">滚轮缩放 · 拖拽画布平移 · 拖拽节点 · 点击查看详情</span>
    </div>

    <div class="knowledge-graph-panel__body">
      <div class="knowledge-graph-panel__canvas-wrap">
        <div ref="graphContainerRef" class="knowledge-graph-panel__canvas" />

        <div v-if="isLoading" class="knowledge-graph-panel__overlay">正在加载关系图谱…</div>
        <div v-else-if="errorMessage" class="knowledge-graph-panel__overlay knowledge-graph-panel__overlay--error">
          {{ errorMessage }}
          <UiButton size="sm" variant="secondary" @click="loadGraphData">重试</UiButton>
        </div>
        <div v-else-if="!hasGraph" class="knowledge-graph-panel__overlay">
          这本书还没有共现关系图谱，请先完成知识库构建。
        </div>
        <div v-else-if="activeNodes.length === 0" class="knowledge-graph-panel__overlay">
          没有匹配当前筛选条件的实体。
        </div>
      </div>

      <aside class="knowledge-graph-panel__detail" aria-live="polite">
        <template v-if="selectedNodeDetail">
          <h3 class="knowledge-graph-panel__detail-title">
            {{ selectedNodeDetail.node.name }}
          </h3>
          <p class="knowledge-graph-panel__detail-meta">
            {{ getCategoryLabel(selectedNodeDetail.node.category) }} · 出现
            {{ selectedNodeDetail.node.frequency ?? 0 }} 页 · 关联 {{ selectedNodeDetail.degree }} 个实体
          </p>
          <p v-if="selectedNodeDetail.node.aliases?.length" class="knowledge-graph-panel__detail-text">
            别名：{{ selectedNodeDetail.node.aliases.join('、') }}
          </p>
          <h4 class="knowledge-graph-panel__detail-subtitle">最强关联</h4>
          <ul class="knowledge-graph-panel__neighbors">
            <li
              v-for="item in selectedNodeDetail.topNeighbors"
              :key="item.node.id"
              class="knowledge-graph-panel__neighbor"
            >
              <span class="knowledge-graph-panel__neighbor-name">{{ item.node.name }}</span>
              <span class="knowledge-graph-panel__neighbor-weight">共现 {{ item.weight }} 次</span>
            </li>
          </ul>
        </template>

        <template v-else-if="selectedEdgeDetail">
          <h3 class="knowledge-graph-panel__detail-title">关系详情</h3>
          <p class="knowledge-graph-panel__detail-text">
            {{ selectedEdgeDetail.source?.name ?? '未知' }} ↔
            {{ selectedEdgeDetail.target?.name ?? '未知' }}
          </p>
          <p class="knowledge-graph-panel__detail-meta">
            共现 {{ selectedEdgeDetail.edge.weight ?? 0 }} 次
            <span v-if="selectedEdgeDetail.edge.common_pages?.length">
              · 页码 {{ selectedEdgeDetail.edge.common_pages.slice(0, 20).join('、') }}
            </span>
          </p>
        </template>

        <p v-else class="knowledge-graph-panel__detail-empty">
          点击一个实体或一条关系，查看它的共现邻居与页码证据。
        </p>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.knowledge-graph-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 560px;
}

.knowledge-graph-panel__toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}

.knowledge-graph-panel__categories,
.knowledge-graph-panel__controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.knowledge-graph-panel__count {
  margin-left: 4px;
  font-size: 11px;
  opacity: 0.72;
}

.knowledge-graph-panel__search {
  width: 200px;
}

.knowledge-graph-panel__field {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.knowledge-graph-panel__number {
  width: 84px;
}

.knowledge-graph-panel__book-picker {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.knowledge-graph-panel__book-picker-label {
  flex: none;
}

.knowledge-graph-panel__book-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.knowledge-graph-panel__book-chip {
  border-radius: 999px;
}

.knowledge-graph-panel__stats {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.knowledge-graph-panel__hint {
  margin-left: auto;
}

.knowledge-graph-panel__book-legend {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.knowledge-graph-panel__book-swatch {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}

.knowledge-graph-panel__body {
  display: flex;
  gap: 12px;
  flex: 1;
  min-height: 0;
}

.knowledge-graph-panel__canvas-wrap {
  position: relative;
  flex: 1;
  min-height: 520px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background-color: var(--color-surface);
  overflow: hidden;
}

.knowledge-graph-panel__canvas {
  width: 100%;
  height: 100%;
  min-height: 520px;
}

.knowledge-graph-panel__overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: center;
  justify-content: center;
  padding: 24px;
  text-align: center;
  font-size: 13px;
  color: var(--color-text-secondary);
  background-color: var(--color-surface);
}

.knowledge-graph-panel__overlay--error {
  color: var(--color-text-primary);
}

.knowledge-graph-panel__detail {
  width: 268px;
  flex: none;
  padding: 14px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background-color: var(--color-surface);
  overflow-y: auto;
  max-height: 620px;
}

.knowledge-graph-panel__detail-title {
  margin: 0 0 6px;
  font-size: 15px;
  color: var(--color-text-primary);
}

.knowledge-graph-panel__detail-subtitle {
  margin: 14px 0 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.knowledge-graph-panel__detail-meta,
.knowledge-graph-panel__detail-text,
.knowledge-graph-panel__detail-empty {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.knowledge-graph-panel__neighbors {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.knowledge-graph-panel__neighbor {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  color: var(--color-text-primary);
}

.knowledge-graph-panel__neighbor-weight {
  color: var(--color-text-secondary);
}

@media (--breakpoint-xl-down) {
  .knowledge-graph-panel__body {
    flex-direction: column;
  }

  .knowledge-graph-panel__detail {
    width: auto;
    max-height: none;
  }
}
</style>
