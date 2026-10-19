import { describe, expect, it, vi } from 'vitest'

import { getTextStyleDefaults } from '@/defaults/textStyleDefaults'

const textStyle = {
  ...getTextStyleDefaults(),
  fontSize: 37,
}

const mocks = vi.hoisted(() => ({
  upload: vi.fn(),
}))

vi.mock('@/api/client', () => {
  class ApiClientError extends Error {
    readonly code: string
    readonly status: number

    constructor(options: { code: string; message: string; status: number }) {
      super(options.message)
      this.name = 'ApiClientError'
      Object.setPrototypeOf(this, new.target.prototype)
      this.code = options.code
      this.status = options.status
    }
  }

  return {
    ApiClientError,
    apiClient: { upload: mocks.upload },
  }
})

describe('ordinary image upload API', () => {
  it('freezes the current text style in a container import command', async () => {
    const { createContainerImportJob } = await import('@/api/v2/content')
    mocks.upload.mockResolvedValueOnce({
      batchId: 'batch-1',
      jobIds: ['job-1'],
      status: 'queued',
    })

    await createContainerImportJob(
      '00000000-0000-0000-0000-000000000002',
      new File(['archive'], 'chapter.cbz'),
      textStyle,
    )

    const body = mocks.upload.mock.calls[0]?.[1] as FormData
    expect(JSON.parse(String(body.get('textStyle')))).toEqual(textStyle)
  })
})
