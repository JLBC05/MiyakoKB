<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTaskCenterStore } from '@/stores/taskCenterStore'
import ProductThemeToggle from '@/components/product/ProductThemeToggle.vue'
import UiButton from '@/components/ui/UiButton.vue'

defineProps<{
  bookTitle?: string
}>()

const route = useRoute()
const router = useRouter()
const taskCenter = useTaskCenterStore()

const currentPath = computed(() => route.path)

interface NavItem {
  name: string
  path: string
  label: string
  icon: string
}

const navItems: NavItem[] = [
  { name: 'bookshelf', path: '/', label: '漫画库', icon: '📚' },
  { name: 'graph', path: '/graph', label: '知识图谱', icon: '🏷️' },
  { name: 'qa', path: '/qa', label: '智能问答', icon: '💬' },
  { name: 'characters', path: '/characters', label: '角色 Skill', icon: '🎭' },
  { name: 'settings', path: '/settings', label: '设置', icon: '⚙️' },
]

function isItemActive(item: NavItem): boolean {
  if (item.path === '/') {
    return currentPath.value === '/' || currentPath.value.startsWith('/reader')
  }
  return currentPath.value.startsWith(item.path)
}

function handleNavClick(item: NavItem) {
  // 保持如果带有 book 参数则继续带上
  const queryBook = route.query.book
  if (queryBook && item.path !== '/' && item.path !== '/settings') {
    void router.push({ path: item.path, query: { book: queryBook } })
  } else {
    void router.push({ path: item.path })
  }
}
</script>

<template>
  <header class="miyako-navbar" role="banner">
    <div class="miyako-navbar__container">
      <!-- 左侧：品牌 Logo -->
      <div class="miyako-navbar__left">
        <RouterLink to="/" class="miyako-navbar__brand">
          <span class="miyako-navbar__logo-icon">🌸</span>
          <span class="miyako-navbar__brand-name">MiyakoKB</span>
        </RouterLink>
        <div v-if="bookTitle" class="miyako-navbar__book-badge">
          <span class="miyako-navbar__book-separator">/</span>
          <span class="miyako-navbar__book-title" :title="bookTitle">{{ bookTitle }}</span>
        </div>
      </div>

      <!-- 中间：核心四大导航项 -->
      <nav class="miyako-navbar__nav" aria-label="主导航">
        <UiButton
          v-for="item in navItems"
          :key="item.path"
          variant="tab"
          class="miyako-navbar__nav-btn"
          :class="{ 'miyako-navbar__nav-btn--active': isItemActive(item) }"
          @click="handleNavClick(item)"
        >
          <span class="miyako-navbar__nav-icon" aria-hidden="true">{{ item.icon }}</span>
          <span class="miyako-navbar__nav-label">{{ item.label }}</span>
        </UiButton>
      </nav>

      <!-- 右侧：操作区（任务中心、主题切换） -->
      <div class="miyako-navbar__right">
        <UiButton
          variant="toolbar"
          class="miyako-navbar__task-btn"
          title="打开任务中心"
          :aria-label="`打开任务中心，${taskCenter.activeCount} 个当前任务`"
          @click="taskCenter.open()"
        >
          <span
            class="miyako-navbar__task-signal"
            :class="{
              'miyako-navbar__task-signal--active': taskCenter.currentJobs.some(job => job.status === 'running'),
              'miyako-navbar__task-signal--paused': !taskCenter.currentJobs.some(job => job.status === 'running') && taskCenter.currentJobs.some(job => job.status === 'paused'),
            }"
          />
          <span class="miyako-navbar__task-icon" aria-hidden="true">📋</span>
          <span class="miyako-navbar__task-label">任务中心</span>
          <span
            v-if="taskCenter.activeCount + taskCenter.queuedCount > 0"
            class="miyako-navbar__task-badge"
          >
            {{ taskCenter.activeCount + taskCenter.queuedCount }}
          </span>
        </UiButton>

        <ProductThemeToggle class="miyako-navbar__theme-toggle" />
      </div>
    </div>
  </header>
</template>

<style scoped>
.miyako-navbar {
  --miyako-navbar-shadow-bar: 0 1px 3px rgba(0, 0, 0, 0.04);
  --miyako-navbar-shadow-active: 0 1px 3px rgba(0, 0, 0, 0.08);
  --miyako-navbar-tint-hover: rgba(153, 119, 204, 0.08);

  position: sticky;
  top: 0;
  z-index: var(--z-app-header);
  width: 100%;
  height: 58px;
  background-color: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
  box-shadow: var(--miyako-navbar-shadow-bar);
  transition: background-color 0.2s, border-color 0.2s;
}

.miyako-navbar__container {
  display: flex;
  align-items: center;
  justify-content: space-between;
  max-width: 1680px;
  height: 100%;
  margin: 0 auto;
  padding: 0 24px;
}

.miyako-navbar__left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 180px;
}

.miyako-navbar__brand {
  display: flex;
  align-items: center;
  gap: 8px;
  text-decoration: none;
  color: var(--color-text-primary);
  font-weight: 700;
  font-size: 1.25rem;
  letter-spacing: -0.02em;
}

.miyako-navbar__logo-icon {
  font-size: 1.4rem;
  line-height: 1;
}

.miyako-navbar__brand-name {
  color: var(--color-brand-primary);
  font-family: inherit;
}

.miyako-navbar__book-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.9rem;
  color: var(--color-text-secondary);
}

.miyako-navbar__book-separator {
  opacity: 0.5;
}

.miyako-navbar__book-title {
  max-width: 220px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 500;
  color: var(--color-text-primary);
}

.miyako-navbar__nav {
  display: flex;
  align-items: center;
  gap: 8px;
  background-color: var(--color-surface-secondary);
  padding: 4px;
  border-radius: 10px;
  border: 1px solid var(--color-border);
}

.miyako-navbar__nav-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 7px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 0.9rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.18s ease;
}

.miyako-navbar__nav-btn:hover {
  color: var(--color-text-primary);
  background-color: var(--miyako-navbar-tint-hover);
}

.miyako-navbar__nav-btn--active,
.miyako-navbar__nav-btn--active:hover {
  background-color: var(--color-surface);
  color: var(--color-brand-primary);
  font-weight: 600;
  box-shadow: var(--miyako-navbar-shadow-active);
}

.miyako-navbar__nav-icon {
  font-size: 1rem;
  line-height: 1;
}

.miyako-navbar__right {
  display: flex;
  align-items: center;
  gap: 16px;
  min-width: 180px;
  justify-content: flex-end;
}

.miyako-navbar__theme-toggle {
  display: flex;
  align-items: center;
}

.miyako-navbar__task-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 12px;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background-color: var(--color-surface-secondary);
  color: var(--color-text-primary);
  font-size: 0.84rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
}

.miyako-navbar__task-btn:hover {
  background-color: var(--color-surface-hover);
  border-color: var(--color-brand-primary);
  color: var(--color-brand-primary);
}

.miyako-navbar__task-icon {
  font-size: 1rem;
}

.miyako-navbar__task-signal {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: var(--color-text-muted);
}

.miyako-navbar__task-signal--active {
  background-color: var(--color-status-success);
  box-shadow: 0 0 6px var(--color-status-success);
  animation: pulse-signal 1.5s infinite;
}

.miyako-navbar__task-signal--paused {
  background-color: var(--color-status-warning);
}

@keyframes pulse-signal {
  0% { transform: scale(0.95); opacity: 0.8; }
  50% { transform: scale(1.2); opacity: 1; }
  100% { transform: scale(0.95); opacity: 0.8; }
}

.miyako-navbar__task-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background-color: var(--color-brand-primary);
  color: var(--color-text-inverse);
  font-size: 0.72rem;
  font-weight: 700;
}

.miyako-navbar__login-link:hover {
  background-color: var(--color-border);
}
</style>
