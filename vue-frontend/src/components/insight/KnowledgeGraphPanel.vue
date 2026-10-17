<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useInsightStore } from '@/stores/insightStore'
import { getInsightGraph, type V2InsightGraphNode, type V2InsightGraphEdge } from '@/api/v2/insight'
import UiButton from '@/components/ui/UiButton.vue'

const insightStore = useInsightStore()

const isLoading = ref(false)
const errorMessage = ref('')
const rawNodes = ref<V2InsightGraphNode[]>([])
const rawEdges = ref<V2InsightGraphEdge[]>([])

// 筛选控制
const selectedCategory = ref<string>('all')
const minWeight = ref<number>(1)
const searchQuery = ref<string>('')
const showLabels = ref<boolean>(true)

// 画布视图状态 (缩放与平移)
const transform = ref({ x: 0, y: 0, scale: 1 })
const isPanning = ref(false)
const panStart = ref({ x: 0, y: 0 })
const svgContainerRef = ref<HTMLDivElement | null>(null)
const viewportSize = ref({ width: 800, height: 600 })

// 交互选中状态
const hoveredNodeId = ref<string | null>(null)
const selectedNodeId = ref<string | null>(null)
const selectedEdgeId = ref<string | null>(null)
const isDraggingNode = ref(false)
const draggedNodeId = ref<string | null>(null)

// 力导向模拟节点结构
interface SimNode extends V2InsightGraphNode {
  x: number
  y: number
  vx: number
  vy: number
  radius: number
  fx?: number | null // 拖拽固定坐标
  fy?: number | null
}

interface SimEdge extends V2InsightGraphEdge {
  sourceNode: SimNode
  targetNode: SimNode
}

const simNodes = ref<SimNode[]>([])
const simEdges = ref<SimEdge[]>([])
let animFrameId: number | null = null
let simulationAlpha = 1.0

const categories = [
  { key: 'all', label: '全部', icon: '🌐', color: '#8b5cf6' },
  { key: 'characters', label: '角色', icon: '👤', color: '#a855f7' },
  { key: 'abilities', label: '能力', icon: '⚡', color: '#eab308' },
  { key: 'items', label: '物品', icon: '🎒', color: '#06b6d4' },
  { key: 'locations', label: '地点', icon: '📍', color: '#10b981' },
  { key: 'concepts', label: '概念', icon: '💡', color: '#f97316' },
]

function getCategoryColor(category: string): string {
  const match = categories.find(c => c.key === category)
  return match ? match.color : '#94a3b8'
}

function getCategoryLabel(category: string): string {
  const match = categories.find(c => c.key === category)
  return match ? match.label : category
}

// 统计各分类实体数量
const categoryCounts = computed(() => {
  const map: Record<string, number> = { all: rawNodes.value.length }
  for (const c of categories) {
    if (c.key !== 'all') map[c.key] = 0
  }
  for (const n of rawNodes.value) {
    if (map[n.category] !== undefined) map[n.category]++
  }
  return map
})

// 筛选后活跃的节点与边
const activeNodes = computed(() => {
  return simNodes.value.filter(n => {
    if (selectedCategory.value !== 'all' && n.category !== selectedCategory.value) {
      return false
    }
    if (searchQuery.value.trim()) {
      const q = searchQuery.value.trim().toLowerCase()
      const matchName = n.name.toLowerCase().includes(q)
      const matchAlias = n.aliases?.some(a => a.toLowerCase().includes(q))
      if (!matchName && !matchAlias) return false
    }
    return true
  })
})

const activeNodeIdSet = computed(() => new Set(activeNodes.value.map(n => n.id)))

const activeEdges = computed(() => {
  return simEdges.value.filter(e => {
    if (!activeNodeIdSet.value.has(e.sourceNode.id) || !activeNodeIdSet.value.has(e.targetNode.id)) {
      return false
    }
    return e.weight >= minWeight.value
  })
})

// 当前聚焦的高亮集合 (hover 或 click 产生的一度邻居)
const focusTargetId = computed(() => hoveredNodeId.value || selectedNodeId.value)

const connectedNodeIdSet = computed(() => {
  const id = focusTargetId.value
  if (!id) return null
  const set = new Set<string>([id])
  for (const edge of activeEdges.value) {
    if (edge.sourceNode.id === id) set.add(edge.targetNode.id)
    if (edge.targetNode.id === id) set.add(edge.sourceNode.id)
  }
  return set
})

const connectedEdgeIdSet = computed(() => {
  const id = focusTargetId.value
  if (!id) return null
  const set = new Set<string>()
  for (const edge of activeEdges.value) {
    if (edge.sourceNode.id === id || edge.targetNode.id === id) {
      set.add(edge.id)
    }
  }
  return set
})

// 当前选中的实体详情
const selectedNodeDetail = computed(() => {
  if (!selectedNodeId.value) return null
  const node = simNodes.value.find(n => n.id === selectedNodeId.value)
  if (!node) return null

  // 找出与其共现权重最高的前 8 个关联实体
  const neighbors: { node: SimNode; weight: number; commonPages: number[] }[] = []
  for (const edge of simEdges.value) {
    if (edge.sourceNode.id === node.id) {
      neighbors.push({ node: edge.targetNode, weight: edge.weight, commonPages: edge.common_pages })
    } else if (edge.targetNode.id === node.id) {
      neighbors.push({ node: edge.sourceNode, weight: edge.weight, commonPages: edge.common_pages })
    }
  }
  neighbors.sort((a, b) => b.weight - a.weight)

  return {
    ...node,
    topNeighbors: neighbors.slice(0, 10),
  }
})

// 当前选中的连线详情
const selectedEdgeDetail = computed(() => {
  if (!selectedEdgeId.value) return null
  return simEdges.value.find(e => e.id === selectedEdgeId.value) || null
})

// 初始化与构建力导向图数据
async function loadGraphData() {
  const bookId = insightStore.activeBookId
  if (!bookId) {
    rawNodes.value = []
    rawEdges.value = []
    simNodes.value = []
    simEdges.value = []
    return
  }

  isLoading.value = true
  errorMessage.value = ''
  try {
    const data = await getInsightGraph(bookId, { minWeight: 1 })
    rawNodes.value = data.nodes || []
    rawEdges.value = data.edges || []
    initSimulation()
  } catch (err: any) {
    console.error('加载关系图谱失败:', err)
    errorMessage.value = err?.message || '加载关系图谱数据失败'
  } finally {
    isLoading.value = false
  }
}

function initSimulation() {
  const width = viewportSize.value.width || 800
  const height = viewportSize.value.height || 600
  const centerX = width / 2
  const centerY = height / 2

  // 1. 建立模拟节点
  const maxFreq = Math.max(...rawNodes.value.map(n => n.frequency || 1), 1)
  const nodeMap = new Map<string, SimNode>()

  simNodes.value = rawNodes.value.map((n, i) => {
    // 节点半径根据词频对数缩放：14px ~ 38px
    const normalized = Math.min(1, Math.max(0, Math.log2((n.frequency || 1) + 1) / Math.log2(maxFreq + 1)))
    const radius = Math.round(14 + normalized * 24)

    // 初始环形分布 + 随机抖动
    const angle = (i / (rawNodes.value.length || 1)) * 2 * Math.PI
    const dist = 100 + Math.random() * (Math.min(width, height) * 0.35)
    const node: SimNode = {
      ...n,
      x: centerX + Math.cos(angle) * dist,
      y: centerY + Math.sin(angle) * dist,
      vx: (Math.random() - 0.5) * 2,
      vy: (Math.random() - 0.5) * 2,
      radius,
    }
    nodeMap.set(n.id, node)
    return node
  })

  // 2. 建立模拟连线
  simEdges.value = rawEdges.value
    .map(e => {
      const sourceNode = nodeMap.get(e.source)
      const targetNode = nodeMap.get(e.target)
      if (!sourceNode || !targetNode) return null
      return {
        ...e,
        sourceNode,
        targetNode,
      } as SimEdge
    })
    .filter((e): e is SimEdge => e !== null)

  reheatSimulation()
}

// 模拟步进计算 (Force Layout Step)
function stepSimulation() {
  if (simulationAlpha < 0.005) {
    if (animFrameId) {
      cancelAnimationFrame(animFrameId)
      animFrameId = null
    }
    return
  }

  const nodes = activeNodes.value
  const edges = activeEdges.value
  const width = viewportSize.value.width || 800
  const height = viewportSize.value.height || 600
  const centerX = width / 2
  const centerY = height / 2

  const kRepulsion = 1200 * simulationAlpha
  const kCenter = 0.03 * simulationAlpha

  // 1. 中心重力
  for (const n of nodes) {
    if (n.fx != null && n.fy != null) {
      n.x = n.fx
      n.y = n.fy
      continue
    }
    n.vx += (centerX - n.x) * kCenter
    n.vy += (centerY - n.y) * kCenter
  }

  // 2. 节点两两排斥力 (Coulomb)
  const nLen = nodes.length
  for (let i = 0; i < nLen; i++) {
    const na = nodes[i]
    for (let j = i + 1; j < nLen; j++) {
      const nb = nodes[j]
      const dx = nb.x - na.x
      const dy = nb.y - na.y
      const distSq = dx * dx + dy * dy + 1
      const dist = Math.sqrt(distSq)
      const minDist = na.radius + nb.radius + 12

      // 碰撞与排斥
      let force = kRepulsion / distSq
      if (dist < minDist) {
        force += (minDist - dist) * 0.5 * simulationAlpha
      }

      const fx = (dx / dist) * force
      const fy = (dy / dist) * force

      if (na.fx == null) {
        na.vx -= fx
        na.vy -= fy
      }
      if (nb.fx == null) {
        nb.vx += fx
        nb.vy += fy
      }
    }
  }

  // 3. 弹簧引力 (Hooke)
  for (const e of edges) {
    const na = e.sourceNode
    const nb = e.targetNode
    const dx = nb.x - na.x
    const dy = nb.y - na.y
    const dist = Math.sqrt(dx * dx + dy * dy) || 1

    // 理想边长根据共现权重调整：权重越高，拉得越近
    const idealDist = Math.max(50, 160 - Math.min(e.weight, 20) * 4)
    const springForce = (dist - idealDist) * 0.05 * simulationAlpha

    const fx = (dx / dist) * springForce
    const fy = (dy / dist) * springForce

    if (na.fx == null) {
      na.vx += fx
      na.vy += fy
    }
    if (nb.fx == null) {
      nb.vx -= fx
      nb.vy -= fy
    }
  }

  // 4. 应用速度与衰减
  const damping = 0.82
  for (const n of nodes) {
    if (n.fx != null && n.fy != null) {
      n.vx = 0
      n.vy = 0
      continue
    }
    n.vx *= damping
    n.vy *= damping
    n.x += n.vx
    n.y += n.vy
  }

  simulationAlpha *= 0.985
  animFrameId = requestAnimationFrame(stepSimulation)
}

function reheatSimulation(alpha = 1.0) {
  simulationAlpha = alpha
  if (!animFrameId) {
    animFrameId = requestAnimationFrame(stepSimulation)
  }
}

// 画布视图控制：缩放与平移
function handleWheel(e: WheelEvent) {
  e.preventDefault()
  const zoomFactor = e.deltaY < 0 ? 1.12 : 0.89
  const newScale = Math.min(3.5, Math.max(0.2, transform.value.scale * zoomFactor))

  if (!svgContainerRef.value) return
  const rect = svgContainerRef.value.getBoundingClientRect()
  const mouseX = e.clientX - rect.left
  const mouseY = e.clientY - rect.top

  // 围绕鼠标位置进行无级缩放
  transform.value.x = mouseX - (mouseX - transform.value.x) * (newScale / transform.value.scale)
  transform.value.y = mouseY - (mouseY - transform.value.y) * (newScale / transform.value.scale)
  transform.value.scale = newScale
}

function handleMouseDownBackground(e: MouseEvent) {
  if (e.target !== e.currentTarget && (e.target as HTMLElement).tagName !== 'svg') return
  isPanning.value = true
  panStart.value = { x: e.clientX - transform.value.x, y: e.clientY - transform.value.y }
  selectedNodeId.value = null
  selectedEdgeId.value = null
}

function handleMouseMove(e: MouseEvent) {
  if (isPanning.value) {
    transform.value.x = e.clientX - panStart.value.x
    transform.value.y = e.clientY - panStart.value.y
  } else if (isDraggingNode.value && draggedNodeId.value) {
    const node = simNodes.value.find(n => n.id === draggedNodeId.value)
    if (node) {
      // 鼠标屏幕坐标转 SVG 画布坐标
      const canvasX = (e.clientX - transform.value.x) / transform.value.scale
      const canvasY = (e.clientY - transform.value.y) / transform.value.scale
      node.fx = canvasX
      node.fy = canvasY
      node.x = canvasX
      node.y = canvasY
      reheatSimulation(0.3)
    }
  }
}

function handleMouseUp() {
  isPanning.value = false
  if (isDraggingNode.value && draggedNodeId.value) {
    const node = simNodes.value.find(n => n.id === draggedNodeId.value)
    if (node) {
      node.fx = null
      node.fy = null
    }
  }
  isDraggingNode.value = false
  draggedNodeId.value = null
}

function handleNodeMouseDown(node: SimNode, e: MouseEvent) {
  e.stopPropagation()
  isDraggingNode.value = true
  draggedNodeId.value = node.id
  selectedNodeId.value = node.id
  selectedEdgeId.value = null
}

function handleNodeClick(node: SimNode, e: MouseEvent) {
  e.stopPropagation()
  selectedNodeId.value = node.id
  selectedEdgeId.value = null
}

function handleEdgeClick(edge: SimEdge, e: MouseEvent) {
  e.stopPropagation()
  selectedEdgeId.value = edge.id
  selectedNodeId.value = null
}

function resetView() {
  transform.value = { x: 0, y: 0, scale: 1 }
  reheatSimulation(0.6)
}

function updateContainerSize() {
  if (svgContainerRef.value) {
    viewportSize.value = {
      width: svgContainerRef.value.clientWidth || 800,
      height: svgContainerRef.value.clientHeight || 600,
    }
  }
}

watch(() => insightStore.activeBookId, () => {
  loadGraphData()
})

watch([selectedCategory, minWeight], () => {
  reheatSimulation(0.5)
})

onMounted(() => {
  updateContainerSize()
  window.addEventListener('resize', updateContainerSize)
  window.addEventListener('mouseup', handleMouseUp)
  window.addEventListener('mousemove', handleMouseMove)
  loadGraphData()
})

onUnmounted(() => {
  window.removeEventListener('resize', updateContainerSize)
  window.removeEventListener('mouseup', handleMouseUp)
  window.removeEventListener('mousemove', handleMouseMove)
  if (animFrameId) {
    cancelAnimationFrame(animFrameId)
    animFrameId = null
  }
})
</script>

<template>
  <div class="knowledge-graph-panel">
    <!-- 顶部控制工具栏 -->
    <div class="graph-toolbar">
      <!-- 类别过滤器 -->
      <div class="category-chips">
        <button
          v-for="cat in categories"
          :key="cat.key"
          type="button"
          class="category-chip"
          :class="{ active: selectedCategory === cat.key }"
          @click="selectedCategory = cat.key"
        >
          <span class="chip-icon">{{ cat.icon }}</span>
          <span class="chip-label">{{ cat.label }}</span>
          <span class="chip-count">{{ categoryCounts[cat.key] || 0 }}</span>
        </button>
      </div>

      <!-- 右侧控制区 -->
      <div class="toolbar-actions">
        <!-- 最小共现页数滑块 -->
        <div class="weight-filter" title="过滤低于此共现页数的连线">
          <span class="weight-label">共现频次 ≥ {{ minWeight }}</span>
          <input
            v-model.number="minWeight"
            type="range"
            min="1"
            max="15"
            step="1"
            class="weight-slider"
          />
        </div>

        <!-- 搜索定位 -->
        <div class="graph-search">
          <input
            v-model="searchQuery"
            type="text"
            placeholder="搜索实体..."
            class="search-input"
          />
          <button v-if="searchQuery" class="clear-btn" @click="searchQuery = ''">✕</button>
        </div>

        <!-- 开关标签 -->
        <UiButton
          variant="outline"
          size="sm"
          :class="{ 'btn-active': showLabels }"
          @click="showLabels = !showLabels"
        >
          🏷️ 标签
        </UiButton>

        <!-- 重置与重排 -->
        <UiButton variant="outline" size="sm" @click="resetView">
          🎯 居中
        </UiButton>
        <UiButton variant="outline" size="sm" @click="reheatSimulation(1.0)">
          ⚡ 刷新布局
        </UiButton>
      </div>
    </div>

    <!-- 加载中与错误 -->
    <div v-if="isLoading" class="graph-state-overlay">
      <div class="spinner"></div>
      <p>正在解析实体共现拓扑并构建知识图谱...</p>
    </div>

    <div v-else-if="errorMessage" class="graph-state-overlay error">
      <p class="error-msg">⚠️ {{ errorMessage }}</p>
      <UiButton variant="outline" size="sm" @click="loadGraphData">重试</UiButton>
    </div>

    <div v-else-if="!activeNodes.length" class="graph-state-overlay empty">
      <div class="empty-icon">🕸️</div>
      <h3>暂无关系图谱数据</h3>
      <p>请先在漫画库中对当前漫画构建知识库，系统将自动对齐实体并提取关系网络。</p>
    </div>

    <!-- 主画布与右侧抽屉容器 -->
    <div v-show="activeNodes.length && !isLoading" class="graph-content-area">
      <!-- SVG 交互力导向画布 -->
      <div
        ref="svgContainerRef"
        class="graph-viewport"
        :class="{ grabbing: isPanning || isDraggingNode }"
        @wheel="handleWheel"
        @mousedown="handleMouseDownBackground"
      >
        <svg
          class="graph-svg"
          :width="viewportSize.width"
          :height="viewportSize.height"
        >
          <defs>
            <!-- 连线发光渐变滤镜 -->
            <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
              <feGaussianBlur stdDeviation="3" result="blur" />
              <feComposite in="SourceGraphic" in2="blur" operator="over" />
            </filter>
          </defs>

          <!-- 缩放与平移变换组 -->
          <g :transform="`translate(${transform.x}, ${transform.y}) scale(${transform.scale})`">
            <!-- 关系连线层 -->
            <g class="edges-layer">
              <line
                v-for="edge in activeEdges"
                :key="edge.id"
                :x1="edge.sourceNode.x"
                :y1="edge.sourceNode.y"
                :x2="edge.targetNode.x"
                :y2="edge.targetNode.y"
                class="graph-edge"
                :class="{
                  highlighted: connectedEdgeIdSet?.has(edge.id) || selectedEdgeId === edge.id,
                  dimmed: focusTargetId && !connectedEdgeIdSet?.has(edge.id),
                  selected: selectedEdgeId === edge.id,
                }"
                :stroke-width="Math.min(8, Math.max(1.5, Math.log2(edge.weight + 1) * 1.8))"
                @click="handleEdgeClick(edge, $event)"
              />
            </g>

            <!-- 实体节点层 -->
            <g class="nodes-layer">
              <g
                v-for="node in activeNodes"
                :key="node.id"
                class="graph-node-group"
                :class="{
                  highlighted: connectedNodeIdSet?.has(node.id) || selectedNodeId === node.id,
                  dimmed: focusTargetId && !connectedNodeIdSet?.has(node.id),
                  selected: selectedNodeId === node.id,
                }"
                :transform="`translate(${node.x}, ${node.y})`"
                @mouseenter="hoveredNodeId = node.id"
                @mouseleave="hoveredNodeId = null"
                @mousedown="handleNodeMouseDown(node, $event)"
                @click="handleNodeClick(node, $event)"
              >
                <!-- 节点外层光晕光圈 (选中或高亮时) -->
                <circle
                  v-if="selectedNodeId === node.id || hoveredNodeId === node.id"
                  class="node-glow-ring"
                  :r="node.radius + 6"
                  :stroke="getCategoryColor(node.category)"
                />

                <!-- 主实体圆点 -->
                <circle
                  class="node-circle"
                  :r="node.radius"
                  :fill="getCategoryColor(node.category)"
                />

                <!-- 节点中心图标或缩写 -->
                <text
                  class="node-icon-text"
                  text-anchor="middle"
                  dominant-baseline="central"
                  :font-size="Math.max(10, node.radius * 0.75)"
                >
                  {{ node.name.charAt(0) }}
                </text>

                <!-- 节点文本标签 -->
                <text
                  v-if="showLabels || selectedNodeId === node.id || hoveredNodeId === node.id"
                  class="node-label"
                  text-anchor="middle"
                  :y="node.radius + 15"
                >
                  {{ node.name }}
                </text>
              </g>
            </g>
          </g>
        </svg>

        <!-- 画布浮动状态脚标 -->
        <div class="viewport-badge">
          <span>节点: {{ activeNodes.length }}</span>
          <span class="dot">·</span>
          <span>连线: {{ activeEdges.length }}</span>
          <span class="dot">·</span>
          <span>缩放: {{ Math.round(transform.scale * 100) }}%</span>
        </div>
      </div>

      <!-- 右侧实体/关系详情面板 -->
      <div class="graph-detail-drawer" :class="{ open: !!selectedNodeDetail || !!selectedEdgeDetail }">
        <!-- 实体详情展示 -->
        <div v-if="selectedNodeDetail" class="detail-card">
          <div class="detail-header">
            <div class="title-wrap">
              <span class="category-badge" :style="{ backgroundColor: getCategoryColor(selectedNodeDetail.category) + '22', color: getCategoryColor(selectedNodeDetail.category) }">
                {{ getCategoryLabel(selectedNodeDetail.category) }}
              </span>
              <h3 class="entity-title">{{ selectedNodeDetail.name }}</h3>
            </div>
            <button class="close-btn" @click="selectedNodeId = null">✕</button>
          </div>

          <div class="detail-stats">
            <div class="stat-item">
              <span class="stat-num">{{ selectedNodeDetail.frequency }}</span>
              <span class="stat-label">总登场频次</span>
            </div>
            <div class="stat-item">
              <span class="stat-num">{{ selectedNodeDetail.pages.length }}</span>
              <span class="stat-label">覆盖漫画页</span>
            </div>
            <div class="stat-item">
              <span class="stat-num">{{ selectedNodeDetail.topNeighbors.length }}</span>
              <span class="stat-label">关联羁绊数</span>
            </div>
          </div>

          <!-- 别名列表 -->
          <div v-if="selectedNodeDetail.aliases?.length" class="detail-section">
            <div class="section-title">已知别名 / 称号</div>
            <div class="tags-list">
              <span v-for="alias in selectedNodeDetail.aliases" :key="alias" class="entity-tag">
                {{ alias }}
              </span>
            </div>
          </div>

          <!-- 亲密共现伙伴 -->
          <div v-if="selectedNodeDetail.topNeighbors.length" class="detail-section">
            <div class="section-title">核心关联伙伴 (共现频次最高)</div>
            <div class="neighbors-list">
              <div
                v-for="neighbor in selectedNodeDetail.topNeighbors"
                :key="neighbor.node.id"
                class="neighbor-row"
                @click="selectedNodeId = neighbor.node.id"
              >
                <div class="neighbor-avatar" :style="{ backgroundColor: getCategoryColor(neighbor.node.category) }">
                  {{ neighbor.node.name.charAt(0) }}
                </div>
                <div class="neighbor-info">
                  <span class="neighbor-name">{{ neighbor.node.name }}</span>
                  <span class="neighbor-cat">{{ getCategoryLabel(neighbor.node.category) }}</span>
                </div>
                <div class="neighbor-weight">
                  同登场 <strong>{{ neighbor.weight }}</strong> 次
                </div>
              </div>
            </div>
          </div>

          <!-- 登场页码汇总 -->
          <div v-if="selectedNodeDetail.pages.length" class="detail-section">
            <div class="section-title">出场页码 (前 30 页)</div>
            <div class="pages-chips">
              <span v-for="page in selectedNodeDetail.pages.slice(0, 30)" :key="page" class="page-chip">
                第 {{ page }} 页
              </span>
              <span v-if="selectedNodeDetail.pages.length > 30" class="page-more">
                +{{ selectedNodeDetail.pages.length - 30 }} 页
              </span>
            </div>
          </div>
        </div>

        <!-- 连线详情展示 -->
        <div v-else-if="selectedEdgeDetail" class="detail-card">
          <div class="detail-header">
            <div class="title-wrap">
              <span class="category-badge link-badge">🕸️ 羁绊关系</span>
              <h3 class="entity-title">
                {{ selectedEdgeDetail.sourceNode.name }} ⟷ {{ selectedEdgeDetail.targetNode.name }}
              </h3>
            </div>
            <button class="close-btn" @click="selectedEdgeId = null">✕</button>
          </div>

          <div class="relation-summary-box">
            <div class="co-occur-stat">
              共同在漫画中登场 <strong class="highlight-count">{{ selectedEdgeDetail.weight }}</strong> 页
            </div>
            <div class="relation-partners">
              <div class="partner-card" @click="selectedNodeId = selectedEdgeDetail.sourceNode.id">
                <span class="partner-role" :style="{ color: getCategoryColor(selectedEdgeDetail.sourceNode.category) }">
                  {{ getCategoryLabel(selectedEdgeDetail.sourceNode.category) }}
                </span>
                <span class="partner-name">{{ selectedEdgeDetail.sourceNode.name }}</span>
              </div>
              <span class="partner-vs">🤝</span>
              <div class="partner-card" @click="selectedNodeId = selectedEdgeDetail.targetNode.id">
                <span class="partner-role" :style="{ color: getCategoryColor(selectedEdgeDetail.targetNode.category) }">
                  {{ getCategoryLabel(selectedEdgeDetail.targetNode.category) }}
                </span>
                <span class="partner-name">{{ selectedEdgeDetail.targetNode.name }}</span>
              </div>
            </div>
          </div>

          <!-- 共同登场页码 -->
          <div class="detail-section">
            <div class="section-title">共同出场页码 ({{ selectedEdgeDetail.common_pages.length }})</div>
            <div class="pages-chips">
              <span v-for="page in selectedEdgeDetail.common_pages" :key="page" class="page-chip common">
                第 {{ page }} 页
              </span>
            </div>
          </div>
        </div>

        <!-- 空提示 -->
        <div v-else class="detail-empty">
          <div class="hint-icon">👆</div>
          <p class="hint-title">交互指引</p>
          <ul class="hint-list">
            <li><strong>滚轮缩放</strong>：平滑放大或缩小图谱</li>
            <li><strong>拖动画布</strong>：按住背景拖拽漫游视野</li>
            <li><strong>拖动节点</strong>：按住圆圈拖拽调整局部拓扑</li>
            <li><strong>悬浮高亮</strong>：查看该实体直接一度关联</li>
            <li><strong>点击节点/连线</strong>：查看详尽登场与共现伙伴数据</li>
          </ul>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.knowledge-graph-panel {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 640px;
  background: var(--color-surface, #1e1b2e);
  border-radius: var(--radius-lg, 12px);
  border: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
  overflow: hidden;
  position: relative;
}

/* 顶部工具栏 */
.graph-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 18px;
  background: var(--color-surface-hover, #262338);
  border-bottom: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
  z-index: 10;
}

.category-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.category-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border-radius: 9999px;
  border: 1px solid var(--color-border, rgba(255, 255, 255, 0.1));
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-secondary, #94a3b8);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s ease;
}

.category-chip:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
}

.category-chip.active {
  background: var(--color-primary, #8b5cf6);
  border-color: var(--color-primary, #8b5cf6);
  color: #fff;
  font-weight: 500;
  box-shadow: 0 0 12px rgba(139, 92, 246, 0.35);
}

.chip-count {
  font-size: 11px;
  padding: 1px 5px;
  border-radius: 9999px;
  background: rgba(0, 0, 0, 0.25);
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.weight-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(0, 0, 0, 0.2);
  padding: 4px 10px;
  border-radius: var(--radius-md, 8px);
  border: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
}

.weight-label {
  font-size: 12px;
  color: var(--color-text-secondary, #94a3b8);
  white-space: nowrap;
}

.weight-slider {
  width: 80px;
  accent-color: var(--color-primary, #8b5cf6);
  cursor: pointer;
}

.graph-search {
  position: relative;
  display: flex;
  align-items: center;
}

.search-input {
  width: 140px;
  padding: 4px 26px 4px 10px;
  border-radius: var(--radius-md, 8px);
  border: 1px solid var(--color-border, rgba(255, 255, 255, 0.1));
  background: rgba(0, 0, 0, 0.25);
  color: #fff;
  font-size: 12px;
  transition: all 0.2s;
}

.search-input:focus {
  outline: none;
  width: 180px;
  border-color: var(--color-primary, #8b5cf6);
}

.clear-btn {
  position: absolute;
  right: 6px;
  background: transparent;
  border: none;
  color: #94a3b8;
  font-size: 11px;
  cursor: pointer;
}

.btn-active {
  background: rgba(139, 92, 246, 0.2) !important;
  border-color: var(--color-primary, #8b5cf6) !important;
  color: #fff !important;
}

/* 主画布与内容区 */
.graph-content-area {
  display: flex;
  flex: 1;
  position: relative;
  overflow: hidden;
}

.graph-viewport {
  flex: 1;
  height: 100%;
  position: relative;
  cursor: grab;
  background: radial-gradient(circle at center, rgba(139, 92, 246, 0.05) 0%, transparent 70%),
              radial-gradient(#2d2847 1px, transparent 1px);
  background-size: 100% 100%, 24px 24px;
}

.graph-viewport.grabbing {
  cursor: grabbing;
}

.graph-svg {
  width: 100%;
  height: 100%;
  display: block;
}

/* 连线样式 */
.graph-edge {
  stroke: rgba(255, 255, 255, 0.16);
  stroke-linecap: round;
  cursor: pointer;
  transition: stroke 0.2s, stroke-width 0.2s, opacity 0.2s;
}

.graph-edge:hover {
  stroke: var(--color-primary, #8b5cf6);
  stroke-width: 4px !important;
}

.graph-edge.highlighted {
  stroke: #c084fc;
  stroke-opacity: 0.9;
  filter: url(#glow);
}

.graph-edge.selected {
  stroke: #fbbf24;
  stroke-width: 4.5px !important;
  filter: url(#glow);
}

.graph-edge.dimmed {
  opacity: 0.08;
}

/* 节点样式 */
.graph-node-group {
  cursor: pointer;
  transition: opacity 0.2s, transform 0.15s ease-out;
}

.graph-node-group.dimmed {
  opacity: 0.12;
}

.node-glow-ring {
  fill: none;
  stroke-width: 2.5px;
  stroke-dasharray: 4 2;
  animation: ring-rotate 8s linear infinite;
}

@keyframes ring-rotate {
  from { stroke-dashoffset: 0; }
  to { stroke-dashoffset: 40; }
}

.node-circle {
  stroke: rgba(255, 255, 255, 0.9);
  stroke-width: 2px;
  filter: drop-shadow(0 4px 6px rgba(0, 0, 0, 0.4));
  transition: r 0.2s, stroke-width 0.2s;
}

.graph-node-group:hover .node-circle,
.graph-node-group.selected .node-circle {
  stroke: #fbbf24;
  stroke-width: 3.5px;
  filter: drop-shadow(0 0 10px rgba(251, 191, 36, 0.6));
}

.node-icon-text {
  fill: #ffffff;
  font-weight: 700;
  pointer-events: none;
  user-select: none;
}

.node-label {
  fill: var(--color-text, #f1f5f9);
  font-size: 12px;
  font-weight: 500;
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.9), 0 0 6px rgba(0, 0, 0, 0.8);
  pointer-events: none;
  user-select: none;
}

/* 画布状态角标 */
.viewport-badge {
  position: absolute;
  bottom: 14px;
  left: 14px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 9999px;
  background: rgba(15, 12, 29, 0.75);
  backdrop-filter: blur(8px);
  border: 1px solid rgba(255, 255, 255, 0.08);
  color: #94a3b8;
  font-size: 11px;
  pointer-events: none;
}

.viewport-badge .dot {
  opacity: 0.4;
}

/* 右侧详情抽屉 */
.graph-detail-drawer {
  width: 340px;
  height: 100%;
  background: var(--color-surface-hover, #231f38);
  border-left: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  z-index: 5;
}

.detail-card {
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.detail-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.category-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 9999px;
  font-size: 11px;
  font-weight: 600;
  margin-bottom: 6px;
}

.category-badge.link-badge {
  background: rgba(251, 191, 36, 0.15);
  color: #fbbf24;
}

.entity-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: #fff;
  line-height: 1.3;
}

.close-btn {
  background: transparent;
  border: none;
  color: #94a3b8;
  font-size: 14px;
  cursor: pointer;
  padding: 4px;
  border-radius: 4px;
}

.close-btn:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
}

.detail-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  background: rgba(0, 0, 0, 0.2);
  padding: 10px;
  border-radius: var(--radius-md, 8px);
}

.stat-item {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.stat-num {
  font-size: 16px;
  font-weight: 700;
  color: #fff;
}

.stat-label {
  font-size: 11px;
  color: #94a3b8;
  margin-top: 2px;
}

.detail-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.section-title {
  font-size: 12px;
  font-weight: 600;
  color: #94a3b8;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.tags-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.entity-tag {
  padding: 3px 8px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.06);
  font-size: 12px;
  color: #cbd5e1;
}

.neighbors-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.neighbor-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.05);
  cursor: pointer;
  transition: all 0.15s;
}

.neighbor-row:hover {
  background: rgba(255, 255, 255, 0.08);
  border-color: var(--color-primary, #8b5cf6);
}

.neighbor-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: #fff;
}

.neighbor-info {
  flex: 1;
  display: flex;
  flex-direction: column;
}

.neighbor-name {
  font-size: 13px;
  color: #fff;
  font-weight: 500;
}

.neighbor-cat {
  font-size: 10px;
  color: #94a3b8;
}

.neighbor-weight {
  font-size: 11px;
  color: #cbd5e1;
}

.neighbor-weight strong {
  color: #fbbf24;
}

.pages-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  max-height: 140px;
  overflow-y: auto;
}

.page-chip {
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(139, 92, 246, 0.15);
  border: 1px solid rgba(139, 92, 246, 0.3);
  color: #d8b4fe;
  font-size: 11px;
}

.page-chip.common {
  background: rgba(251, 191, 36, 0.15);
  border-color: rgba(251, 191, 36, 0.3);
  color: #fde047;
}

.page-more {
  font-size: 11px;
  color: #94a3b8;
  align-self: center;
}

/* 关系总结框 */
.relation-summary-box {
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: var(--radius-md, 8px);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.co-occur-stat {
  font-size: 13px;
  color: #cbd5e1;
  text-align: center;
}

.highlight-count {
  font-size: 16px;
  color: #fbbf24;
}

.relation-partners {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.partner-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 8px;
  background: rgba(255, 255, 255, 0.04);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
}

.partner-card:hover {
  background: rgba(255, 255, 255, 0.08);
}

.partner-role {
  font-size: 10px;
  font-weight: 600;
  margin-bottom: 2px;
}

.partner-name {
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  text-align: center;
}

.partner-vs {
  font-size: 16px;
}

/* 右侧空白状态引导 */
.detail-empty {
  padding: 24px 18px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  color: #94a3b8;
  margin: auto 0;
}

.hint-icon {
  font-size: 32px;
  margin-bottom: 8px;
}

.hint-title {
  font-size: 14px;
  font-weight: 600;
  color: #fff;
  margin-bottom: 12px;
}

.hint-list {
  text-align: left;
  font-size: 12px;
  line-height: 1.8;
  padding-left: 18px;
  margin: 0;
  color: #94a3b8;
}

.hint-list strong {
  color: #e2e8f0;
}

/* 覆盖层状态 */
.graph-state-overlay {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 40px;
  color: #94a3b8;
}

.graph-state-overlay.empty .empty-icon {
  font-size: 48px;
}

.graph-state-overlay.empty h3 {
  font-size: 18px;
  font-weight: 600;
  color: #fff;
  margin: 0;
}

.graph-state-overlay.error .error-msg {
  color: #ef4444;
}

.spinner {
  width: 32px;
  height: 32px;
  border: 3px solid rgba(139, 92, 246, 0.2);
  border-top-color: var(--color-primary, #8b5cf6);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
