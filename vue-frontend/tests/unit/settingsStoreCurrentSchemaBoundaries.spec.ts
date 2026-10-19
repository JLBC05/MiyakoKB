import { describe, expect, it } from 'vitest'

import { createDefaultSettings } from '@/stores/settings/defaults'
import { parseCurrentSettings } from '@/stores/settings/schema'
import { deepClone } from '@/utils/deepClone'

// 说明：本文件原先还覆盖 settings store 的变更/事务 API
// （updateTranslationService、addProofreadingRound、setProofreadingEnabled、
// store.settings.*、saveToBackend 权威校验、workflow_preferences 拒绝逻辑）。
// 这些 store API 随翻译/HQ/校对/OCR 设置域在 7e6e8d2 chore(cleanup) 中一并删除，
// 相关测试已随之移除；此处仅保留仍然存活的当前 schema 解析边界。
describe('settings current schema boundaries', () => {
  it('rejects extra fields instead of treating them as an older settings shape', () => {
    const topLevel = createDefaultSettings() as unknown as Record<string, unknown>
    topLevel.removedField = true

    const nested = deepClone(createDefaultSettings())
    ;(nested.translation.openaiOptions.request as Record<string, unknown>).removedField = true

    const proofreading = deepClone(createDefaultSettings())
    proofreading.proofreading.rounds = [{
      ...proofreading.hqTranslation,
      id: '11111111-1111-4111-8111-111111111111',
      name: '第1轮',
    }]
    ;(proofreading.proofreading.rounds[0] as unknown as Record<string, unknown>).sessionReset = true

    const retiredProofreadingRetry = deepClone(createDefaultSettings())
    ;(retiredProofreadingRetry.proofreading as unknown as Record<string, unknown>).maxRetries = 2

    expect(parseCurrentSettings(topLevel)).toBeNull()
    expect(parseCurrentSettings(nested)).toBeNull()
    expect(parseCurrentSettings(proofreading)).toBeNull()
    expect(parseCurrentSettings(retiredProofreadingRetry)).toBeNull()
  })

  it('rejects malformed numeric runtime options instead of coercing them', () => {
    const fractionalRetry = deepClone(createDefaultSettings())
    fractionalRetry.translation.openaiOptions.execution.transportRetries = 1.5

    const invalidTemperature = deepClone(createDefaultSettings())
    invalidTemperature.translation.openaiOptions.request.temperature = 2.1

    const fractionalBatch = deepClone(createDefaultSettings())
    fractionalBatch.hqTranslation.batchSize = 1.5

    expect(parseCurrentSettings(fractionalRetry)).toBeNull()
    expect(parseCurrentSettings(invalidTemperature)).toBeNull()
    expect(parseCurrentSettings(fractionalBatch)).toBeNull()
  })

  it('accepts positive HQ and proofreading batch sizes without fixed upper bounds', () => {
    const settings = createDefaultSettings()
    settings.hqTranslation.batchSize = 128
    settings.proofreading.rounds = [{
      ...settings.hqTranslation,
      id: '11111111-1111-4111-8111-111111111111',
      name: '第1轮',
      batchSize: 256,
    }]

    expect(parseCurrentSettings(settings)?.hqTranslation.batchSize).toBe(128)
    expect(parseCurrentSettings(settings)?.proofreading.rounds[0]?.batchSize).toBe(256)
  })

  it('rejects duplicate proofreading round identities', () => {
    const settings = createDefaultSettings()
    const id = '11111111-1111-4111-8111-111111111111'
    settings.proofreading.rounds = [
      { ...settings.hqTranslation, id, name: '第1轮' },
      { ...settings.hqTranslation, id, name: '第2轮' },
    ]

    expect(parseCurrentSettings(settings)).toBeNull()
  })

  it('accepts backend-valid text styles without frontend-only upper bounds', () => {
    const settings = createDefaultSettings()
    settings.textStyle = {
      ...settings.textStyle,
      fontSize: 1024,
      strokeWidth: 80,
      lineSpacing: 12.5,
    }

    expect(parseCurrentSettings(settings)?.textStyle).toMatchObject({
      fontSize: 1024,
      strokeWidth: 80,
      lineSpacing: 12.5,
    })
  })
})