<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/authStore'
import ProductThemeToggle from '@/components/product/ProductThemeToggle.vue'
import UiButton from '@/components/ui/UiButton.vue'

defineProps<{
  bookTitle?: string
}>()

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

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
        <button
          v-for="item in navItems"
          :key="item.path"
          type="button"
          class="miyako-navbar__nav-btn"
          :class="{ 'miyako-navbar__nav-btn--active': isItemActive(item) }"
          @click="handleNavClick(item)"
        >
          <span class="miyako-navbar__nav-icon" aria-hidden="true">{{ item.icon }}</span>
          <span class="miyako-navbar__nav-label">{{ item.label }}</span>
        </button>
      </nav>

      <!-- 右侧：操作区（主题切换、账户入口） -->
      <div class="miyako-navbar__right">
        <ProductThemeToggle class="miyako-navbar__theme-toggle" />
        <div v-if="auth.user" class="miyako-navbar__user">
          <RouterLink to="/account" class="miyako-navbar__user-link" title="用户中心">
            <span class="miyako-navbar__user-avatar">{{ auth.user.username.slice(0, 1).toUpperCase() }}</span>
            <span class="miyako-navbar__user-name">{{ auth.user.username }}</span>
          </RouterLink>
        </div>
        <div v-else-if="auth.authenticated === false" class="miyako-navbar__auth-links">
          <RouterLink to="/login" class="miyako-navbar__login-link">登录</RouterLink>
        </div>
      </div>
    </div>
  </header>
</template>

<style scoped>
.miyako-navbar {
  position: sticky;
  top: 0;
  z-index: 100;
  width: 100%;
  height: 58px;
  background-color: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
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
  color: var(--color-primary);
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
  background-color: rgba(153, 119, 204, 0.08);
}

.miyako-navbar__nav-btn--active {
  background-color: var(--color-surface) !important;
  color: var(--color-primary) !important;
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
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

.miyako-navbar__user-link {
  display: flex;
  align-items: center;
  gap: 8px;
  text-decoration: none;
  color: var(--color-text-primary);
  font-size: 0.88rem;
  font-weight: 500;
  padding: 4px 8px;
  border-radius: 6px;
  transition: background-color 0.15s;
}

.miyako-navbar__user-link:hover {
  background-color: var(--color-surface-secondary);
}

.miyako-navbar__user-avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background-color: var(--color-primary);
  color: #FFFFFF;
  font-size: 0.8rem;
  font-weight: 700;
}

.miyako-navbar__user-name {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.miyako-navbar__login-link {
  font-size: 0.88rem;
  font-weight: 600;
  color: var(--color-primary);
  text-decoration: none;
  padding: 6px 12px;
  border-radius: 6px;
  background-color: var(--color-surface-secondary);
}

.miyako-navbar__login-link:hover {
  background-color: var(--color-border);
}
</style>
