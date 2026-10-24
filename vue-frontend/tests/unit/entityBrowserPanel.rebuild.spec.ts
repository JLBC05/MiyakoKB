import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const { listEntitiesMock, rebuildAlignmentMock, showToastMock, confirmProductActionMock } =
  vi.hoisted(() => ({
    listEntitiesMock: vi.fn(),
    rebuildAlignmentMock: vi.fn(),
    showToastMock: vi.fn(),
    confirmProductActionMock: vi.fn(),
  }))

vi.mock('@/api/v2/insight', () => ({
  listInsightEntities: listEntitiesMock,
  rebuildInsightEntityAlignment: rebuildAlignmentMock,
}))

vi.mock('@/utils/toast', () => ({
  showToast: showToastMock,
}))

vi.mock('@/composables/useProductConfirm', () => ({
  confirmProductAction: confirmProductActionMock,
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
}))

import EntityBrowserPanel from '@/components/insight/EntityBrowserPanel.vue'
import { useInsightStore } from '@/stores/insightStore'

enableAutoUnmount(afterEach)

function mountPanel() {
  return mount(EntityBrowserPanel, {
    global: {
      stubs: {
        UiButton: {
          template: '<button v-bind="$attrs"><slot /></button>',
        },
        UiInput: {
          props: ['modelValue'],
          emits: ['update:modelValue'],
          template: '<div class="stub-input" />',
        },
      },
    },
  })
}

function findRebuildButton(wrapper: ReturnType<typeof mountPanel>) {
  return wrapper
    .findAll('button')
    .find(button => button.text().includes('重建实体对齐'))
}

describe('EntityBrowserPanel entity alignment rebuild', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    listEntitiesMock.mockResolvedValue({ entities: [] })
    rebuildAlignmentMock.mockReset()
    showToastMock.mockReset()
    confirmProductActionMock.mockReset()
  })

  it('starts a rebuild job only after the user confirms', async () => {
    useInsightStore().currentBookId = 'book-1'
    confirmProductActionMock.mockResolvedValue(true)
    rebuildAlignmentMock.mockResolvedValueOnce({ batchId: 'batch-1', jobIds: ['align-job-1'] })

    const wrapper = mountPanel()
    await flushPromises()

    const button = findRebuildButton(wrapper)
    expect(button).toBeTruthy()
    await button!.trigger('click')
    await flushPromises()

    expect(confirmProductActionMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: '重建实体对齐', confirmText: '重建' })
    )
    expect(rebuildAlignmentMock).toHaveBeenCalledWith('book-1')
    expect(wrapper.text()).toContain('任务已启动')
    expect(wrapper.text()).toContain('align-job-1')
    expect(showToastMock).toHaveBeenCalledWith('实体对齐重建任务已启动', 'success')
  })

  it('does not call the backend when the user cancels the confirmation', async () => {
    useInsightStore().currentBookId = 'book-1'
    confirmProductActionMock.mockResolvedValue(false)

    const wrapper = mountPanel()
    await flushPromises()
    await findRebuildButton(wrapper)!.trigger('click')
    await flushPromises()

    expect(rebuildAlignmentMock).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('任务已启动')
  })

  it('surfaces a backend failure as an error toast and clears the rebuild state', async () => {
    useInsightStore().currentBookId = 'book-1'
    confirmProductActionMock.mockResolvedValue(true)
    rebuildAlignmentMock.mockRejectedValueOnce(new Error('书不存在'))

    const wrapper = mountPanel()
    await flushPromises()
    await findRebuildButton(wrapper)!.trigger('click')
    await flushPromises()

    expect(showToastMock).toHaveBeenCalledWith('书不存在', 'error')
    expect(wrapper.text()).not.toContain('任务已启动')
  })
})
