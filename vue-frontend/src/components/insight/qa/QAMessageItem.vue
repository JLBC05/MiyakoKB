<script setup lang="ts">
import { computed } from 'vue'
import ProductChipList from '@/components/product/ProductChipList.vue'
import type { ProductChipItem } from '@/components/product/ProductChipList.vue'
import ProductMessageBubble from '@/components/product/ProductMessageBubble.vue'
import UiIcon from '@/components/ui/UiIcon.vue'
import type { QACitation, QAMessage } from '@/types/insight'

const props = defineProps<{
  message: QAMessage
  renderMarkdown: (content: string) => string
}>()

const emit = defineEmits<{
  (event: 'previewCitation', citation: QACitation): void
  (event: 'selectPage', pageNum: number): void
}>()

const citationChips = computed<ProductChipItem[]>(() => {
  return (
    props.message.citations?.map((citation, index) => {
      // 审计 P2-4：页码有两种语义。章内页码可以直接点开阅读器；全书页码只是
      // 模型看到的编号，不能当阅读器位置用——标签里明确写出来，避免两者混为一张卡。
      const pagePart = citation.pageNumberSource === 'run_global'
        ? `全书第${citation.page}页`
        : `第${citation.page}页`
      let label = pagePart
      if (citation.chapterTitle) {
        label = `${citation.chapterTitle}：${pagePart}`
      } else if (citation.chapterOrdinal !== undefined) {
        label = `第${citation.chapterOrdinal}话：${pagePart}`
      }
      if (citation.source === 'entity') {
        label = `${label}（实体依据）`
      }
      return {
        id: index,
        label,
        ariaLabel: `查看 ${label}`,
        interactive: true,
        tone: 'primary',
      }
    }) ?? []
  )
})

const retrievalWarnings = computed(() => props.message.warnings ?? [])

const avatarLabel = computed(() => (props.message.role === 'user' ? '用户' : '智能助手'))
const messageLabel = computed(() => (props.message.role === 'user' ? '用户问题' : '智能回答'))

function selectCitation(id: string | number): void {
  const index = typeof id === 'number' ? id : parseInt(String(id), 10)
  const citation = props.message.citations?.[index]
  if (citation) {
    emit('previewCitation', citation)
    emit('selectPage', citation.page)
  }
}
</script>

<template>
  <ProductMessageBubble
    :role="message.role"
    :aria-label="messageLabel"
    :avatar-label="avatarLabel"
    :avatar-image-src="message.role === 'user' ? '/pic/logo.png' : undefined"
    :avatar-icon-name="message.role === 'assistant' ? 'message' : undefined"
  >
    <template v-if="message.role === 'user'">
      {{ message.content }}
    </template>

    <template v-else>
      <div v-if="message.isLoading" class="qa-message-item__loading">
        {{ message.content }}
      </div>
      <template v-else>
        <div class="qa-message-item__answer-text" v-html="renderMarkdown(message.content)"></div>
      </template>
    </template>

    <template
      v-if="message.role === 'assistant' && (citationChips.length > 0 || message.resolvedEntities?.matched_entities?.length || retrievalWarnings.length > 0) && !message.isLoading"
      #footer
    >
      <div class="qa-message-item__footer-stack">
        <div
          v-if="retrievalWarnings.length > 0"
          class="qa-retrieval-warnings"
          role="status"
          aria-label="检索状态提示"
        >
          <UiIcon class="qa-retrieval-warnings__icon" name="alert-triangle" :size="14" />
          <span class="qa-retrieval-warnings__text">{{ retrievalWarnings.join(' ') }}</span>
        </div>

        <ProductChipList
          v-if="citationChips.length > 0"
          class="qa-message-item__citations"
          aria-label="引用页码"
          label="引用:"
          label-icon-name="book-open"
          :items="citationChips"
          @select="selectCitation"
        />

        <div
          v-if="message.resolvedEntities?.matched_entities?.length"
          class="qa-resolved-entities"
        >
          <UiIcon class="qa-entities-icon" name="tags" :size="14" />
          <span class="qa-entities-label">基于实体知识库：</span>
          <span class="qa-entities-list">
            {{ message.resolvedEntities.matched_entities.map(e => e.canonical_name).join('、') }}
          </span>
        </div>
      </div>
    </template>
  </ProductMessageBubble>
</template>

<style scoped>
.qa-message-item__answer-text {
  line-height: 1.7;
}

.qa-message-item__citations {
  margin: 0;
}

.qa-message-item__footer-stack {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.qa-retrieval-warnings {
  display: inline-flex;
  align-items: flex-start;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--color-status-warning-surface);
  border: 1px solid color-mix(in srgb, var(--color-status-warning) 45%, transparent);
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
  align-self: flex-start;
}

.qa-retrieval-warnings__icon {
  font-size: 13px;
  line-height: 1.4;
}

.qa-retrieval-warnings__text {
  color: var(--color-text-primary);
}

.qa-resolved-entities {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--color-brand-primary) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-brand-primary) 25%, transparent);
  font-size: 12px;
  line-height: 1.4;
  color: var(--color-text-secondary);
  align-self: flex-start;
}

.qa-entities-icon {
  font-size: 13px;
  line-height: 1;
}

.qa-entities-label {
  font-weight: 500;
  color: var(--color-brand-primary);
}

.qa-entities-list {
  color: var(--color-text-primary);
  font-weight: 500;
}

.qa-message-item__loading {
  display: inline-block;
  color: var(--color-text-supporting);
}

.qa-message-item__loading::after {
  animation: dots 1.5s steps(4, end) infinite;
  content: '';
}

@keyframes dots {
  0%,
  20% {
    content: '';
  }

  40% {
    content: '.';
  }

  60% {
    content: '..';
  }

  80%,
  100% {
    content: '...';
  }
}
</style>
