import { describe, expect, it } from 'vitest'

import { parseSettingsValidationIssue } from '@/utils/settingsValidation'

describe('settings validation error routing', () => {
  it('points a provider field error at the matching tab', () => {
    const issue = parseSettingsValidationIssue(
      'provider_settings.insight_vlm.imageMaxSize must be an integer from 64 to 4096',
    )

    expect(issue).toEqual({
      tab: 'vlm',
      fieldLabel: '最大图片边长',
      detail: 'provider_settings.insight_vlm.imageMaxSize must be an integer from 64 to 4096',
    })
  })

  it('keeps the four provider domains apart', () => {
    expect(parseSettingsValidationIssue('provider_settings.insight_chat.modelName must be a string')?.tab).toBe('llm')
    expect(parseSettingsValidationIssue('provider_settings.insight_embedding.rpm_limit must be an integer of at least 1')?.tab).toBe('embedding')
    expect(parseSettingsValidationIssue('provider_settings.insight_reranker.timeoutSeconds must be an integer')?.tab).toBe('reranker')
  })

  it('routes analysis payload problems to the analysis tab', () => {
    const issue = parseSettingsValidationIssue(
      'insight.analysis.batch has unknown keys: pagesPerBatchExtra',
    )

    expect(issue?.tab).toBe('analysis')
  })

  it('labels the field that sits at the end of the dotted path', () => {
    expect(parseSettingsValidationIssue('provider_settings.insight_vlm.analyzeConcurrency must be an integer from 1 to 8')?.fieldLabel)
      .toBe('批内并发数')
    expect(parseSettingsValidationIssue('provider_settings.insight_chat.openaiOptions.request.temperature must be from 0 to 2')?.fieldLabel)
      .toBe('温度')
    expect(parseSettingsValidationIssue('insight.analysis.batch is missing keys: contextBatchCount')?.fieldLabel)
      .toBe('上文参考批次数')
  })

  it('falls back to a generic label when the field is unknown', () => {
    const issue = parseSettingsValidationIssue('provider_settings.insight_vlm.unknownKnob must be boolean')

    expect(issue).toEqual({
      tab: 'vlm',
      fieldLabel: '设置项',
      detail: 'provider_settings.insight_vlm.unknownKnob must be boolean',
    })
  })

  it('leaves conflicts, transport failures and empty input on the generic path', () => {
    expect(parseSettingsValidationIssue('provider_settings.insight_vlm 已被其他页面修改')).toBeNull()
    expect(parseSettingsValidationIssue('保存失败: 网络错误')).toBeNull()
    expect(parseSettingsValidationIssue('')).toBeNull()
    expect(parseSettingsValidationIssue(undefined)).toBeNull()
  })
})
