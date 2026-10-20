import { beforeEach, describe, expect, it, vi } from 'vitest'

const { getMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  apiClient: {
    get: getMock,
    post: postMock,
    put: vi.fn(),
    delete: vi.fn(),
    upload: vi.fn(),
  },
}))

import {
  buildInsightCharacterSkill,
  getInsightCharacterSkill,
  getInsightCharacterSkillStatus,
  listInsightCharacterSkills,
} from '@/api/v2/insight'

const bookId = 'book/one two'
const entityId = 'character_hu_zhang_you_ren'

beforeEach(() => {
  getMock.mockReset()
  postMock.mockReset()
})

describe('character skill API', () => {
  it('lists characters under the encoded book route', async () => {
    getMock.mockResolvedValue({ characters: [] })

    await expect(listInsightCharacterSkills(bookId)).resolves.toEqual({ characters: [] })
    expect(getMock).toHaveBeenCalledWith('/api/v2/insight/books/book%2Fone%20two/character-skills')
  })

  it('passes the minimum appearance filter as a query parameter', async () => {
    getMock.mockResolvedValue({ characters: [] })

    await listInsightCharacterSkills(bookId, { minPages: 5 })

    expect(getMock).toHaveBeenCalledWith(
      '/api/v2/insight/books/book%2Fone%20two/character-skills?minPages=5'
    )
  })

  it('keeps zero as an explicit filter value instead of dropping it', async () => {
    getMock.mockResolvedValue({ characters: [] })

    await listInsightCharacterSkills(bookId, { minPages: 0 })

    expect(getMock).toHaveBeenCalledWith(
      '/api/v2/insight/books/book%2Fone%20two/character-skills?minPages=0'
    )
  })

  it('starts a build with an idempotency key and the requested entity', async () => {
    postMock.mockResolvedValue({ state: 'running', stage: 'evidence', done: 0, total: 0 })

    await buildInsightCharacterSkill(bookId, { entityId, maxVisionPages: 9 })

    expect(postMock).toHaveBeenCalledTimes(1)
    const [path, body, options] = postMock.mock.calls[0]
    expect(path).toBe('/api/v2/insight/books/book%2Fone%20two/character-skills')
    expect(body).toEqual({ entityId, maxVisionPages: 9 })
    expect(options.headers['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('omits maxVisionPages when the caller keeps the default', async () => {
    postMock.mockResolvedValue({ state: 'running' })

    await buildInsightCharacterSkill(bookId, { entityId })

    expect(postMock.mock.calls[0][1]).toEqual({ entityId })
  })

  it('reads one skill and its status from separate routes', async () => {
    getMock.mockResolvedValue({ entityId })

    await getInsightCharacterSkill(bookId, entityId)
    await getInsightCharacterSkillStatus(bookId, entityId)

    expect(getMock.mock.calls[0][0]).toBe(
      '/api/v2/insight/books/book%2Fone%20two/character-skills/character_hu_zhang_you_ren'
    )
    expect(getMock.mock.calls[1][0]).toBe(
      '/api/v2/insight/books/book%2Fone%20two/character-skills/character_hu_zhang_you_ren/status'
    )
  })
})
