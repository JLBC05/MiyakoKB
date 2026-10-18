<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import BaseModal from '@/components/common/BaseModal.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import { getPageSummary } from '@/api/v2/content'
import type { QACitation } from '@/types/insight'

interface Props {
  visible: boolean
  citation: QACitation | null
}

const props = defineProps<Props>()

const emit = defineEmits<{
  (event: 'close'): void
}>()

const isLoading = ref(false)
const errorMessage = ref('')
const resolvedImageUrl = ref('')

const modalTitle = computed(() => {
  if (!props.citation) return '引用页面预览'
  const c = props.citation
  if (c.chapterTitle) {
    return `${c.chapterTitle}：第${c.page}页`
  }
  if (c.chapterOrdinal !== undefined) {
    return `第${c.chapterOrdinal}话：第${c.page}页`
  }
  return `第${c.page}页`
})

watch(
  () => [props.visible, props.citation] as const,
  async ([visible, citation]) => {
    if (!visible || !citation) {
      resolvedImageUrl.value = ''
      errorMessage.value = ''
      isLoading.value = false
      return
    }

    errorMessage.value = ''
    if (citation.sourceUrl) {
      resolvedImageUrl.value = citation.sourceUrl
      isLoading.value = false
      return
    }

    if (citation.pageId) {
      isLoading.value = true
      try {
        const summary = await getPageSummary(citation.pageId)
        if (summary.sourceUrl) {
          resolvedImageUrl.value = summary.sourceUrl
        } else {
          errorMessage.value = '该页面尚未生成源图片'
        }
      } catch (err) {
        errorMessage.value = err instanceof Error ? err.message : '加载页面信息失败'
      } finally {
        isLoading.value = false
      }
      return
    }

    errorMessage.value = '缺少页面引用标识，无法加载原图'
  },
  { immediate: true }
)
</script>

<template>
  <BaseModal
    :model-value="visible"
    :title="modalTitle"
    size="large"
    body-padding="compact"
    backdrop="strong"
    backdrop-effect="blur-sm"
    @update:model-value="emit('close')"
  >
    <div class="citation-preview">
      <div v-if="isLoading" class="citation-preview__loading">
        <UiIcon name="loader" class="citation-preview__spinner" />
        <span>正在加载漫画图片...</span>
      </div>

      <div v-else-if="errorMessage" class="citation-preview__error">
        <UiIcon name="alert-circle" />
        <span>{{ errorMessage }}</span>
      </div>

      <div v-else-if="resolvedImageUrl" class="citation-preview__image-container">
        <img
          :src="resolvedImageUrl"
          :alt="modalTitle"
          class="citation-preview__image"
        />
      </div>

      <div v-if="citation?.excerpt" class="citation-preview__excerpt">
        <div class="citation-preview__excerpt-header">
          <UiIcon name="book-open" size="14" />
          <span>引用文本片段</span>
        </div>
        <div class="citation-preview__excerpt-content">
          {{ citation.excerpt }}
        </div>
      </div>
    </div>

    <template #footer>
      <div class="citation-preview__footer">
        <UiButton variant="secondary" size="sm" @click="emit('close')">
          关闭
        </UiButton>
      </div>
    </template>
  </BaseModal>
</template>

<style scoped>
.citation-preview {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.citation-preview__loading,
.citation-preview__error {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 240px;
  color: var(--insight-text-secondary);
  font-size: 14px;
}

.citation-preview__error {
  color: var(--insight-status-error, #f87171);
}

.citation-preview__spinner {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.citation-preview__image-container {
  display: flex;
  justify-content: center;
  align-items: center;
  max-height: 65vh;
  overflow: auto;
  background: var(--insight-surface-tertiary, rgba(0, 0, 0, 0.35));
  border: 1px solid var(--color-border-muted);
  border-radius: 8px;
  padding: 8px;
}

.citation-preview__image {
  max-width: 100%;
  max-height: 60vh;
  object-fit: contain;
  border-radius: 4px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
}

.citation-preview__excerpt {
  padding: 10px 14px;
  border-radius: 6px;
  background: var(--insight-surface-secondary, rgba(255, 255, 255, 0.04));
  border-left: 3px solid var(--insight-action-primary, #a855f7);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.citation-preview__excerpt-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--insight-action-primary, #a855f7);
}

.citation-preview__excerpt-content {
  font-size: 13px;
  line-height: 1.6;
  color: var(--insight-text-secondary, #cbd5e1);
  white-space: pre-wrap;
  word-break: break-word;
}

.citation-preview__footer {
  display: flex;
  justify-content: flex-end;
  width: 100%;
}
</style>
