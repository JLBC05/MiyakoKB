import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const {
  bookDetailMock,
  listMock,
  skillMock,
  statusMock,
  buildMock,
  downloadBlobMock,
  triggerBlobDownloadMock,
  routerReplaceMock,
} = vi.hoisted(() => ({
  bookDetailMock: vi.fn(),
  listMock: vi.fn(),
  skillMock: vi.fn(),
  statusMock: vi.fn(),
  buildMock: vi.fn(),
  downloadBlobMock: vi.fn(),
  triggerBlobDownloadMock: vi.fn(),
  routerReplaceMock: vi.fn(),
}))

const BOOK_ID = 'book-1'
const ENTITY_ID = 'character_jiu_liu_mei'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { book: 'book-1' } }),
  useRouter: () => ({ replace: routerReplaceMock }),
}))

vi.mock('@/api/bookshelf', () => ({ getBookDetail: bookDetailMock }))

vi.mock('@/api/v2/insight', () => ({
  listInsightCharacterSkills: listMock,
  getInsightCharacterSkill: skillMock,
  getInsightCharacterSkillStatus: statusMock,
  buildInsightCharacterSkill: buildMock,
  characterSkillArchiveUrl: (bookId: string, entityId: string, kind?: string) =>
    kind && kind !== 'skill' ? `/archive/${bookId}/${entityId}?kind=${kind}` : `/archive/${bookId}/${entityId}`,
  getInsightLorebook: () => Promise.reject(new Error('lorebook not found')),
  getInsightLorebookStatus: () => Promise.resolve({ status: 'idle' }),
  buildInsightLorebook: () => Promise.resolve({ status: 'running', stage: 'evidence', done: 0, total: 3 }),
  lorebookArchiveUrl: (bookId: string) => `/archive/${bookId}/lorebook`,
}))

vi.mock('@/api/download', () => ({ downloadBlob: downloadBlobMock }))

vi.mock('@/utils/browserDownload', () => ({ triggerBlobDownload: triggerBlobDownloadMock }))

import CharacterSkillView from '@/views/CharacterSkillView.vue'

const AppShellStub = defineComponent({ template: '<div><slot /></div>' })
const NavbarStub = defineComponent({ template: '<nav />' })
const BookSelectorStub = defineComponent({ template: '<div />' })
const NumberFieldStub = defineComponent({
  props: { modelValue: { type: Number, default: null } },
  template: '<div class="number-field-stub" />',
})

function mountView() {
  return mount(CharacterSkillView, {
    global: {
      stubs: {
        AppShell: AppShellStub,
        MiyakoNavbar: NavbarStub,
        BookSelector: BookSelectorStub,
        UiNumberField: NumberFieldStub,
      },
    },
  })
}

function skillDocument() {
  return {
    bookId: BOOK_ID,
    bookTitle: 'FX战士久留美',
    entityId: ENTITY_ID,
    characterName: '久留美',
    revision: 2,
    status: 'ready',
    summary: {
      one_liner: '把母亲亏掉的两千万赢回来。',
      persona_prompt: '用短句下注。',
      personality: [],
      appearance: [],
      speech_style: [],
      relationships: [],
      topics: [],
      timeline: [],
      unknowns: [],
    },
    files: { 'SKILL.md': '# 久留美\n' },
  }
}

beforeEach(() => {
  bookDetailMock.mockReset().mockResolvedValue({ id: BOOK_ID, title: 'FX战士久留美', totalPages: 1507 })
  listMock.mockReset().mockResolvedValue({
    characters: [
      {
        entityId: ENTITY_ID,
        name: '久留美',
        pageCount: 900,
        chapterCount: 40,
        hasSkill: true,
        skillRevision: 2,
        updatedAt: '2026-10-20T12:00:00Z',
      },
    ],
  })
  skillMock.mockReset().mockResolvedValue(skillDocument())
  statusMock.mockReset().mockResolvedValue({
    bookId: BOOK_ID,
    entityId: ENTITY_ID,
    status: 'ready',
    stage: 'done',
    done: 1,
    total: 1,
    revision: 2,
  })
  buildMock.mockReset()
  downloadBlobMock.mockReset().mockResolvedValue({ blob: new Blob(['zip']), filename: '久留美-skill.zip' })
  triggerBlobDownloadMock.mockReset()
  routerReplaceMock.mockReset()
})

describe('character skill view', () => {
  it('leaves the loading banner once the preferred character already has a skill', async () => {
    const wrapper = mountView()

    await flushPromises()

    expect(wrapper.text()).not.toContain('正在读取角色档案')
    expect(wrapper.text()).toContain('把母亲亏掉的两千万赢回来。')
    expect(skillMock).toHaveBeenCalledWith(BOOK_ID, ENTITY_ID, { kind: 'skill' })
    wrapper.unmount()
  })

  it('downloads the archive of the selected character', async () => {
    const wrapper = mountView()
    await flushPromises()

    const exportButton = wrapper
      .findAll('button')
      .find(button => button.text().includes('导出为 .zip'))
    expect(exportButton).toBeTruthy()
    await exportButton!.trigger('click')
    await flushPromises()

    expect(downloadBlobMock).toHaveBeenCalledWith({
      url: `/archive/${BOOK_ID}/${ENTITY_ID}`,
      fallbackFilename: '久留美-skill.zip',
      fallbackErrorMessage: '导出压缩包失败',
    })
    expect(triggerBlobDownloadMock).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
