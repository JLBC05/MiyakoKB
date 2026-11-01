import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { toastInfoMock } = vi.hoisted(() => ({
  toastInfoMock: vi.fn(),
}))

vi.mock('@/utils/toast', () => ({
  showToast: toastInfoMock,
}))

// useRevisionConflict 只在 onMounted 时订阅；这里直接测返回的 handle()，
// 避免依赖组件挂载时序。
import { useRevisionConflict, DEFAULT_REVISION_CONFLICT_TOAST } from '@/composables/useRevisionConflict'
import type { ConflictEventDetail } from '@/api/client'

function detail(overrides: Partial<ConflictEventDetail> = {}): ConflictEventDetail {
  return {
    code: 'revision_conflict',
    message: '',
    url: '/api/v2/settings/transactions',
    method: 'put',
    resource: 'settings',
    ...overrides,
  }
}

describe('useRevisionConflict', () => {
  beforeEach(() => {
    toastInfoMock.mockReset()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('reloads the settings snapshot and shows the reload toast on revision_conflict', async () => {
    const reloadSettings = vi.fn().mockResolvedValue(undefined)
    const { handle } = useRevisionConflict({ reload: { settings: reloadSettings } })

    await handle(detail())

    expect(reloadSettings).toHaveBeenCalledTimes(1)
    expect(toastInfoMock).toHaveBeenCalledWith(DEFAULT_REVISION_CONFLICT_TOAST, 'info')
  })

  it('refreshes tasks for task-resource revision conflicts', async () => {
    const reloadTask = vi.fn().mockResolvedValue(undefined)
    const { handle } = useRevisionConflict({ reload: { task: reloadTask } })

    await handle(detail({ url: '/api/v2/jobs', resource: 'task' }))

    expect(reloadTask).toHaveBeenCalledTimes(1)
  })

  it('ignores non-revision_conflict 409s (e.g. job_conflict)', async () => {
    const reloadSettings = vi.fn()
    const { handle } = useRevisionConflict({ reload: { settings: reloadSettings } })

    await handle(detail({ code: 'job_conflict' }))

    expect(reloadSettings).not.toHaveBeenCalled()
    expect(toastInfoMock).not.toHaveBeenCalled()
  })

  it("ignores the 'other' resource (handled by the owning page)", async () => {
    const reloadSettings = vi.fn()
    const { handle } = useRevisionConflict({ reload: { settings: reloadSettings } })

    await handle(detail({ url: '/api/v2/books/x', resource: 'other' }))

    expect(reloadSettings).not.toHaveBeenCalled()
    expect(toastInfoMock).not.toHaveBeenCalled()
  })

  it('dedupes rapid repeats on the same resource within the window', async () => {
    const reloadSettings = vi.fn().mockResolvedValue(undefined)
    const { handle } = useRevisionConflict({ reload: { settings: reloadSettings }, dedupeMs: 500 })

    await handle(detail())
    await handle(detail())
    await handle(detail())

    expect(reloadSettings).toHaveBeenCalledTimes(1)
    expect(toastInfoMock).toHaveBeenCalledTimes(1)
  })

  it('still shows the toast when the reload throws', async () => {
    const reloadSettings = vi.fn().mockRejectedValue(new Error('network down'))
    const { handle } = useRevisionConflict({ reload: { settings: reloadSettings } })

    await handle(detail())

    expect(toastInfoMock).toHaveBeenCalledWith(DEFAULT_REVISION_CONFLICT_TOAST, 'info')
  })
})
