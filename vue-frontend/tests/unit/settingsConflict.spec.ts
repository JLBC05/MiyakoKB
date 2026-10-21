import { describe, expect, it } from 'vitest'

import { isRevisionConflictError, REVISION_CONFLICT_MESSAGE } from '@/utils/settingsConflict'

describe('settings save conflict detection', () => {
  it('recognises the revision conflicts raised by the settings transaction', () => {
    expect(isRevisionConflictError(new Error('设置已被其他页面更新，请关闭并重新打开设置后再编辑'))).toBe(true)
    expect(isRevisionConflictError(new Error('provider_settings.insight_vlm 已被其他页面修改'))).toBe(true)
    expect(isRevisionConflictError('设置已被其他页面更新')).toBe(true)
  })

  it('leaves transport and validation failures on the generic error path', () => {
    expect(isRevisionConflictError(new Error('保存失败: 网络错误'))).toBe(false)
    expect(isRevisionConflictError(new Error('provider_settings.insight_vlm has invalid fields'))).toBe(false)
    expect(isRevisionConflictError(undefined)).toBe(false)
    expect(isRevisionConflictError({ message: '设置已被其他页面更新' })).toBe(false)
  })

  it('keeps one operator-facing sentence for the conflict state', () => {
    expect(REVISION_CONFLICT_MESSAGE).toBe('设置已在其他页面被修改，本次保存已取消，请重新加载后再编辑')
  })
})
