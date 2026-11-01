import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const { getGraphMock, getGraphForBooksMock } = vi.hoisted(() => ({
  getGraphMock: vi.fn(),
  getGraphForBooksMock: vi.fn(),
}))

vi.mock('@/api/v2/insight', () => ({
  getInsightGraph: getGraphMock,
  getInsightGraphForBooks: getGraphForBooksMock,
}))

// G6 是画布渲染库，jsdom 里跑不动，用假类替换掉真实实现。
vi.mock('@antv/g6', () => {
  class FakeGraph {
    setData() {}
    render = vi.fn().mockResolvedValue(undefined)
    fitView = vi.fn().mockResolvedValue(undefined)
    on() {}
    destroy() {}
  }
  return { Graph: FakeGraph }
})

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
}))

import KnowledgeGraphPanel from '@/components/insight/KnowledgeGraphPanel.vue'
import { useInsightStore } from '@/stores/insightStore'
import { useBookshelfStore } from '@/stores/bookshelfStore'

enableAutoUnmount(afterEach)

function mountPanel() {
  return mount(KnowledgeGraphPanel, {
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
        UiNumberField: {
          props: ['modelValue'],
          emits: ['update:modelValue'],
          template: '<div class="stub-number" />',
        },
      },
    },
  })
}

function findToggleButton(wrapper: ReturnType<typeof mountPanel>) {
  return wrapper.findAll('button').find(button => button.text().includes('多书对比'))
}

describe('KnowledgeGraphPanel multi-book mode', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    getGraphMock.mockReset()
    getGraphForBooksMock.mockReset()
    getGraphMock.mockResolvedValue({ nodes: [], edges: [] })
    getGraphForBooksMock.mockResolvedValue({ groups: [] })

    const bookshelf = useBookshelfStore()
    bookshelf.books = [
      { id: 'book-a', title: '漫画 A' },
      { id: 'book-b', title: '漫画 B' },
    ]
    bookshelf.loadBooks = vi.fn().mockResolvedValue(undefined)
  })

  it('loads single book graph in normal mode', async () => {
    useInsightStore().currentBookId = 'book-a'

    mountPanel()
    await flushPromises()

    expect(getGraphMock).toHaveBeenCalledWith('book-a', { minWeight: 1 })
    expect(getGraphForBooksMock).not.toHaveBeenCalled()
  })

  it('enabling multi-book mode and selecting two books calls the grouped endpoint', async () => {
    useInsightStore().currentBookId = 'book-a'
    getGraphForBooksMock.mockResolvedValue({
      groups: [
        {
          bookId: 'book-a',
          bookTitle: '漫画 A',
          nodes: [{ id: 'n1', name: '角色 1', category: 'characters', frequency: 3 }],
          edges: [],
        },
        {
          bookId: 'book-b',
          bookTitle: '漫画 B',
          nodes: [{ id: 'n2', name: '角色 2', category: 'characters', frequency: 2 }],
          edges: [],
        },
      ],
    })

    const wrapper = mountPanel()
    await flushPromises()

    // 打开多书对比：默认带入当前书。
    await findToggleButton(wrapper)!.trigger('click')
    await flushPromises()

    // 点击第二本书的 chip 加入选择。
    const bookChip = wrapper
      .findAll('button')
      .find(button => button.text().includes('漫画 B'))
    expect(bookChip).toBeTruthy()
    await bookChip!.trigger('click')
    await flushPromises()

    expect(getGraphForBooksMock).toHaveBeenCalledWith(['book-a', 'book-b'], { minWeight: 1 })
    // 两本书的标题都应出现在图例中。
    expect(wrapper.text()).toContain('漫画 A')
    expect(wrapper.text()).toContain('漫画 B')
  })
})
