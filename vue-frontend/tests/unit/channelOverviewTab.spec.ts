import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ChannelOverviewTab from '@/components/insight/settings/ChannelOverviewTab.vue'
import InsightSettingsPanel from '@/components/insight/settings/InsightSettingsPanel.vue'
import UiButton from '@/components/ui/UiButton.vue'
import { useInsightStore } from '@/stores/insightStore'
import { useSettingsStore } from '@/stores/settings'
import type { V2CredentialSummary } from '@/api/v2/settings'

const testVlmConnection = vi.fn()
const testLlmConnection = vi.fn()
const testEmbeddingConnection = vi.fn()
const testRerankerConnection = vi.fn()

vi.mock('@/api/insight', () => ({
  testVlmConnection: (...args: unknown[]) => testVlmConnection(...args),
  testLlmConnection: (...args: unknown[]) => testLlmConnection(...args),
  testEmbeddingConnection: (...args: unknown[]) => testEmbeddingConnection(...args),
  testRerankerConnection: (...args: unknown[]) => testRerankerConnection(...args),
}))

function credential(domain: string, provider: string, hasKey: boolean): V2CredentialSummary {
  return {
    credentialId: '11111111-1111-4111-8111-111111111111',
    credentialVersionId: '22222222-2222-4222-8222-222222222222',
    domain,
    provider,
    secret: { api_key: '' },
    hasKey,
    currentVersion: 1,
    revision: 1,
  }
}

describe('ChannelOverviewTab', () => {
  let pinia: Pinia

  beforeEach(() => {
    testVlmConnection.mockReset()
    testLlmConnection.mockReset()
    testEmbeddingConnection.mockReset()
    testRerankerConnection.mockReset()
    pinia = createPinia()
    setActivePinia(pinia)
  })

  it('lists every model channel with its provider, model and key state', () => {
    const insightStore = useInsightStore()
    const settingsStore = useSettingsStore()
    insightStore.config.vlm.provider = 'custom'
    insightStore.config.vlm.model = 'gemini-3.8-flash-high'
    insightStore.config.vlm.baseUrl = 'http://gateway.example/v1'
    insightStore.config.vlm.apiKey = ''
    insightStore.config.llm.provider = 'deepseek'
    insightStore.config.llm.model = 'deepseek-flash'
    settingsStore.credentialSummaries = [credential('insight_vlm', 'custom', true)] as never

    const wrapper = mount(ChannelOverviewTab, { global: { plugins: [pinia] } })

    const rows = wrapper.findAll('.channel-overview__row').filter(row => !row.classes().includes('channel-overview__row--head'))
    expect(rows).toHaveLength(4)
    expect(rows[0]?.text()).toContain('VLM 视觉模型')
    expect(rows[0]?.text()).toContain('gemini-3.8-flash-high')
    expect(rows[0]?.text()).toContain('已保存')
    // 未保存过的通道按「未填写」提示，并触发缺失提醒
    expect(rows[1]?.text()).toContain('未填写')
    expect(wrapper.get('.channel-overview__hint').text()).toContain('3 个通道')
    expect(wrapper.getComponent(InsightSettingsPanel).props('description')).toContain('四个模型通道')
  })

  it('treats a session-only key as unsaved and keeps the blank secret out of the request', async () => {
    const insightStore = useInsightStore()
    insightStore.config.vlm.apiKey = ''
    testVlmConnection.mockResolvedValue({ success: true, message: 'ok' })

    const wrapper = mount(ChannelOverviewTab, { global: { plugins: [pinia] } })
    const testButton = wrapper.findAllComponents(UiButton).find(button => button.text() === '测试')
    await testButton?.trigger('click')
    await flushPromises()

    expect(testVlmConnection).toHaveBeenCalledWith({
      provider: insightStore.config.vlm.provider,
      api_key: '',
      model: insightStore.config.vlm.model,
      base_url: insightStore.config.vlm.baseUrl,
    })
    expect(wrapper.emitted('showMessage')?.at(-1)).toEqual(['VLM 视觉模型 连接成功', 'success'])
  })

  it('asks the parent view to jump to the matching editor', async () => {
    const wrapper = mount(ChannelOverviewTab, { global: { plugins: [pinia] } })
    const gotoButton = wrapper.findAllComponents(UiButton).filter(button => button.text() === '去配置')[2]
    await gotoButton?.trigger('click')

    expect(wrapper.emitted('goto')?.at(-1)).toEqual(['embedding'])
  })

  it('reports a failing probe without leaving the row stuck in testing state', async () => {
    const insightStore = useInsightStore()
    insightStore.config.reranker.provider = 'jina'
    testRerankerConnection.mockRejectedValue(new Error('网络错误'))

    const wrapper = mount(ChannelOverviewTab, { global: { plugins: [pinia] } })
    const rerankerRow = wrapper.findAll('.channel-overview__row').at(-1)
    const testButton = rerankerRow?.findAllComponents(UiButton).find(button => button.text() === '测试')
    await testButton?.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('showMessage')?.at(-1)).toEqual(['Reranker 重排序 连接失败：网络错误', 'error'])
    expect(rerankerRow?.findComponent(UiButton).props('disabled')).toBe(false)
  })
})
