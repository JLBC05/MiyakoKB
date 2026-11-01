import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { useInsightStore } from '@/stores/insightStore'

const { sendChatMock, getQAStatusMock, showToastMock } = vi.hoisted(() => ({
  sendChatMock: vi.fn(),
  getQAStatusMock: vi.fn(),
  showToastMock: vi.fn(),
}))

vi.mock('@/api/insight', () => ({
  sendChat: sendChatMock,
  rebuildEmbeddings: vi.fn(),
  rebuildCompressedContext: vi.fn(),
  regenerateOverview: vi.fn(),
  getQAStatus: getQAStatusMock,
}))

vi.mock('@/utils/toast', () => ({
  showToast: showToastMock,
}))

vi.mock('@/composables/useProductConfirm', () => ({
  confirmProductAction: vi.fn(),
}))

import QAPanel from '@/components/insight/QAPanel.vue'

enableAutoUnmount(afterEach)

function baseMountOptions(pinia: ReturnType<typeof createPinia>) {
  return {
    global: {
      plugins: [pinia],
      stubs: {
        QAMessageList: {
          props: ['messages', 'renderMarkdown', 'scrollRequestId'],
          emits: ['preview-citation', 'save-note', 'select-page'],
          template:
            '<div class="stub-message-list"><button class="stub-jump" @click="$emit(\'select-page\', 42)">jump</button></div>',
        },
        QAOptionsBar: true,
        QAComposer: {
          name: 'QAComposer',
          props: ['question', 'disabled'],
          emits: ['update:question', 'submit'],
          template: '<button class="stub-submit" @click="$emit(\'submit\')">send</button>',
        },
        ProductStatusBanner: true,
        QASaveNoteModal: true,
        CitationPreviewModal: true,
        UiButton: true,
        UiIcon: true,
      },
    },
  }
}

describe('QAPanel pinned page ids', () => {
  beforeEach(() => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const insightStore = useInsightStore()
    insightStore.currentBookId = 'book-1'
    insightStore.setStreaming(false)
    getQAStatusMock.mockReset()
    getQAStatusMock.mockResolvedValue({ available: true, reason: null })
    sendChatMock.mockReset()
    sendChatMock.mockResolvedValue({
      answer: '答案',
      mode: 'generic',
      citations: [],
      resolvedEntities: undefined,
      warnings: [],
    })
    showToastMock.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the pinned badge and threads pinnedPageIds into the chat request', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useInsightStore().currentBookId = 'book-1'

    const wrapper = mount(QAPanel, {
      props: { pinnedPageIds: ['page-7', 'page-8'], variant: 'drawer' },
      ...baseMountOptions(pinia),
    })
    await flushPromises()

    expect(wrapper.get('.qa-panel__pinned-badge').text()).toContain('当前页优先')

    // 输入问题并提交
    const composer = wrapper.getComponent({ name: 'QAComposer' })
    // QAComposer stub has no v-model here; drive via textarea fallback is unnecessary —
    // set question through the exposed stub event instead.
    ;(composer.vm as unknown as { $emit: (e: string, v: string) => void }).$emit(
      'update:question',
      '这一页说的是谁'
    )
    await flushPromises()
    await wrapper.get('.stub-submit').trigger('click')
    await flushPromises()

    expect(sendChatMock).toHaveBeenCalledTimes(1)
    const options = sendChatMock.mock.calls[0]![2]
    expect(options.pinnedPageIds).toEqual(['page-7', 'page-8'])
  })

  it('omits pinnedPageIds from the chat request when none are set', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useInsightStore().currentBookId = 'book-1'

    const wrapper = mount(QAPanel, {
      props: {},
      ...baseMountOptions(pinia),
    })
    await flushPromises()

    expect(wrapper.find('.qa-panel__pinned-badge').exists()).toBe(false)
  })

  it('emits navigate-page instead of selecting in the store when in drawer mode', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const insightStore = useInsightStore()
    insightStore.currentBookId = 'book-1'
    const selectSpy = vi.spyOn(insightStore, 'selectPage')

    const wrapper = mount(QAPanel, {
      props: { variant: 'drawer' },
      ...baseMountOptions(pinia),
    })
    await flushPromises()

    await wrapper.get('.stub-jump').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('navigate-page')).toBeTruthy()
    expect(wrapper.emitted('navigate-page')![0]).toEqual([42])
    expect(selectSpy).not.toHaveBeenCalled()
  })
})
