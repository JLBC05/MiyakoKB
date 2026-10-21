import { describe, expect, it } from 'vitest'

import { SETTINGS_TABS, filterSettingsTabs } from '@/views/settingsTabs'

describe('settings tab search', () => {
  it('shows every tab when the query is blank', () => {
    expect(filterSettingsTabs(SETTINGS_TABS, '')).toHaveLength(SETTINGS_TABS.length)
    expect(filterSettingsTabs(SETTINGS_TABS, '   ')).toHaveLength(SETTINGS_TABS.length)
  })

  it('finds tabs by the words users actually type', () => {
    expect(filterSettingsTabs(SETTINGS_TABS, '并发').map(tab => tab.id)).toEqual(['vlm'])
    expect(filterSettingsTabs(SETTINGS_TABS, '密钥')).toEqual(
      expect.arrayContaining([expect.objectContaining({ id: 'channels' })]),
    )
    expect(filterSettingsTabs(SETTINGS_TABS, 'slug').map(tab => tab.id)).toEqual(['analysis'])
    expect(filterSettingsTabs(SETTINGS_TABS, '提示词').map(tab => tab.id)).toEqual(['prompts'])
  })

  it('matches labels, descriptions and is case-insensitive', () => {
    expect(filterSettingsTabs(SETTINGS_TABS, 'embedding').map(tab => tab.id)).toEqual(['embedding'])
    expect(filterSettingsTabs(SETTINGS_TABS, 'RAG').map(tab => tab.id)).toEqual(['embedding'])
    expect(filterSettingsTabs(SETTINGS_TABS, '重排').map(tab => tab.id)).toEqual(['reranker'])
  })

  it('returns an empty list when nothing matches so the view can show the empty state', () => {
    expect(filterSettingsTabs(SETTINGS_TABS, '不存在的设置项')).toEqual([])
  })
})
