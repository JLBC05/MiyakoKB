import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  ApiClientError: class ApiClientError extends Error {},
  apiClient: {
    get: mocks.get,
    post: mocks.post,
  },
}))

describe('content command API', () => {
  beforeEach(() => {
    mocks.get.mockReset()
    mocks.post.mockReset()
  })

  it('forwards every supplied page-list query value to the backend contract', async () => {
    mocks.get.mockResolvedValue({ items: [], nextCursor: null, pageOrderRevision: 1 })
    const { listChapterPages } = await import('@/api/v2/content')

    await listChapterPages('chapter/1', { cursor: 0, limit: 0 })

    expect(mocks.get).toHaveBeenCalledWith(
      '/api/v2/chapters/chapter%2F1/pages?cursor=0&limit=0',
      { signal: undefined },
    )
  })

})
