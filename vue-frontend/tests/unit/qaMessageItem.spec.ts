import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import QAMessageItem from '@/components/insight/qa/QAMessageItem.vue'
import ProductChipList from '@/components/product/ProductChipList.vue'
import ProductMessageBubble from '@/components/product/ProductMessageBubble.vue'
import type { QAMessage } from '@/stores/insightStore'

const assistantMessage: QAMessage = {
  id: 'qa-message-1',
  role: 'assistant',
  content: '第 5 页揭示了关键线索。',
  mode: 'precise',
  citations: [{ page: 5 }],
}

describe('QAMessageItem', () => {
  it('uses explicit button semantics for citation navigation', async () => {
    const wrapper = mount(QAMessageItem, {
      props: {
        message: assistantMessage,
        renderMarkdown: content => `<p>${content}</p>`,
      },
    })

    const bubble = wrapper.getComponent(ProductMessageBubble)
    expect(bubble.props('role')).toBe('assistant')
    expect(bubble.props('avatarIconName')).toBe('message')
    expect(bubble.props('avatarLabel')).toBe('智能助手')
    expect(wrapper.find('.qa-message-item__mode-badge').exists()).toBe(false)

    const citations = wrapper.getComponent(ProductChipList)
    expect(citations.props('ariaLabel')).toBe('引用页码')
    expect(citations.props('items')).toEqual([
      {
        id: 0,
        label: '第5页',
        ariaLabel: '查看 第5页',
        interactive: true,
        tone: 'primary',
      },
    ])

    citations.vm.$emit('select', 0)

    expect(wrapper.emitted('selectPage')?.[0]?.[0]).toBe(5)
    expect(wrapper.emitted('previewCitation')?.[0]?.[0]).toEqual({ page: 5 })
  })

  it('formats citations with chapter when chapterOrdinal is provided', () => {
    const wrapper = mount(QAMessageItem, {
      props: {
        message: {
          id: 'qa-msg-2',
          role: 'assistant',
          content: '线索在第5章第16页',
          citations: [{ page: 16, chapterOrdinal: 5 }],
        },
        renderMarkdown: content => `<p>${content}</p>`,
      },
    })

    const citations = wrapper.getComponent(ProductChipList)
    expect(citations.props('items')).toEqual([
      {
        id: 0,
        label: '第5章：第16页',
        ariaLabel: '查看 第5章：第16页',
        interactive: true,
        tone: 'primary',
      },
    ])
  })

  it('does not display save-note button', () => {
    const wrapper = mount(QAMessageItem, {
      props: {
        message: assistantMessage,
        renderMarkdown: content => `<p>${content}</p>`,
      },
    })

    expect(wrapper.find('button[aria-label="保存为笔记"]').exists()).toBe(false)
  })

  it('keeps local QA message hooks scoped to the message item owner', () => {
    const source = readFileSync(
      resolve(process.cwd(), 'src/components/insight/qa/QAMessageItem.vue'),
      'utf8'
    )

    expect(source).toContain('qa-message-item__answer-text')
    expect(source).toContain('qa-message-item__citations')
    expect(source).not.toMatch(
      /\.(?:answer-text|answer-mode-badge|loading-dots|message-citations)\b/
    )
  })
})
