<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useTaskCenterStore } from '@/stores/taskCenterStore'
import ProductThemeToggle from '@/components/product/ProductThemeToggle.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import type { UiIconName } from '@/components/ui/iconRegistry'

defineProps<{
  bookTitle?: string
}>()

const route = useRoute()
const router = useRouter()
const taskCenter = useTaskCenterStore()

const currentPath = computed(() => route.path)

// 用绑定而非静态 src：静态写法会被 SFC 的 transformAssetUrls 当成模块导入，
// 而 /pic/* 是 public 目录下的运行时资源，不该进打包图。
const logoUrl = '/pic/miyako-logo.png'

interface NavItem {
  name: string
  path: string
  label: string
  icon: UiIconName
}

const navItems: NavItem[] = [
  { name: 'bookshelf', path: '/', label: '漫画库', icon: 'book-marked' },
  { name: 'graph', path: '/graph', label: '知识图谱', icon: 'tags' },
  { name: 'qa', path: '/qa', label: '智能问答', icon: 'message' },
  { name: 'characters', path: '/characters', label: '蒸馏角色', icon: 'users' },
  { name: 'settings', path: '/settings', label: '设置', icon: 'settings' },
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
          <img
            class="miyako-navbar__logo-icon"
            :src="logoUrl"
            alt=""
            aria-hidden="true"
            width="28"
            height="28"
          />
          <span class="miyako-navbar__brand-name">MiyakoKB</span>
        </RouterLink>
        <div v-if="bookTitle" class="miyako-navbar__book-badge">
          <span class="miyako-navbar__book-separator">/</span>
          <span class="miyako-navbar__book-title" :title="bookTitle">{{ bookTitle }}</span>
        </div>
      </div>

      <!-- 中间：核心五大导航项 -->
      <nav class="miyako-navbar__nav" aria-label="主导航">
        <UiButton
          v-for="item in navItems"
          :key="item.path"
          variant="tab"
          class="miyako-navbar__nav-btn"
          :class="{ 'miyako-navbar__nav-btn--active': isItemActive(item) }"
          :title="item.label"
          :aria-label="item.label"
          @click="handleNavClick(item)"
        >
          <UiIcon :name="item.icon" :size="18" class="miyako-navbar__nav-icon" />
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
          <UiIcon name="list" :size="18" class="miyako-navbar__task-icon" />
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
  --miyako-navbar-shadow-active: none;
  --miyako-navbar-tint-hover: var(--color-surface-hover);

  position: sticky;
  top: 0;
  z-index: var(--z-app-header);
  width: 100%;
  height: 58px;
  background-color: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
  transition: background-color var(--motion-fast, 150ms ease), border-color var(--motion-fast, 150ms ease);
}

.miyako-navbar__container {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  max-width: 1680px;
  height: 100%;
  margin: 0 auto;
  padding: 0 24px;
}

.miyako-navbar__left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex: 0 1 auto;
}

.miyako-navbar__brand {
  display: flex;
  align-items: center;
  gap: 8px;
  text-decoration: none;
  color: var(--color-text-primary);
  font-weight: 700;
  font-size: var(--font-size-lg, 20px);
  letter-spacing: -0.02em;
  white-space: nowrap;
}

.miyako-navbar__logo-icon {
  width: 28px;
  height: 28px;
  flex: none;
  display: block;
  border-radius: 6px;
  object-fit: contain;
  box-shadow: 0 0 0 1px var(--color-border);
}

.miyako-navbar__brand-name {
  color: var(--color-brand-primary);
  font-family: inherit;
}

.miyako-navbar__book-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--font-size-sm, 14px);
  color: var(--color-text-secondary);
  min-width: 0;
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
  gap: 4px;
  background-color: var(--color-surface-secondary);
  padding: 4px;
  border-radius: var(--radius-control, 8px);
  border: 1px solid var(--color-border);
  flex: 0 0 auto;
}

.miyako-navbar__nav-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 6px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm, 14px);
  font-weight: 500;
  cursor: pointer;
  transition: background-color var(--motion-fast, 150ms ease), color var(--motion-fast, 150ms ease);
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
  display: inline-flex;
  flex: 0 0 auto;
}

.miyako-navbar__right {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex: 0 0 auto;
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
  border-radius: var(--radius-control, 8px);
  border: 1px solid var(--color-border);
  background-color: var(--color-surface-secondary);
  color: var(--color-text-primary);
  font-size: var(--font-size-xs, 12px);
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition: background-color var(--motion-fast, 150ms ease), border-color var(--motion-fast, 150ms ease), color var(--motion-fast, 150ms ease);
}

.miyako-navbar__task-btn:hover {
  background-color: var(--color-surface-hover);
  border-color: var(--color-brand-primary);
  color: var(--color-brand-primary);
}

.miyako-navbar__task-icon {
  display: inline-flex;
  flex: 0 0 auto;
}

/* 任务状态点：静态色点，去掉发光与脉冲 */
.miyako-navbar__task-signal {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: var(--color-text-muted);
  flex: 0 0 auto;
}

.miyako-navbar__task-signal--active {
  background-color: var(--color-status-success);
}

.miyako-navbar__task-signal--paused {
  background-color: var(--color-status-warning);
}

.miyako-navbar__task-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: var(--radius-pill, 999px);
  background-color: var(--color-brand-primary);
  color: var(--color-text-inverse);
  font-size: var(--font-size-xs, 12px);
  font-weight: 700;
}

/* 移动端：导航仅图标横排，任务中心仅图标+角标，避免换行与竖向堆叠 */
@media (--breakpoint-md-down) {
  .miyako-navbar__container {
    padding: 0 12px;
    gap: 8px;
  }

  .miyako-navbar__nav-btn {
    padding: 8px 10px;
  }

  .miyako-navbar__nav-label {
    display: none;
  }

  .miyako-navbar__task-label {
    display: none;
  }

  .miyako-navbar__task-btn {
    padding: 0 8px;
  }
}

@media (--breakpoint-xs-down) {
  .miyako-navbar__brand-name {
    display: none;
  }

  .miyako-navbar__book-badge {
    display: none;
  }

  .miyako-navbar__nav-btn {
    padding: 8px;
  }
}
</style>
