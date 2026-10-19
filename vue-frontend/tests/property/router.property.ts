import { describe, expect, it } from 'vitest'
import * as fc from 'fast-check'
import router from '@/router'
import { APP_ROUTE_PATHS } from '@/constants/routes'

const namedRouteCases = [
  { path: APP_ROUTE_PATHS.login, name: 'login' },
  { path: APP_ROUTE_PATHS.register, name: 'register' },
  { path: APP_ROUTE_PATHS.recover, name: 'recover' },
  { path: APP_ROUTE_PATHS.account, name: 'account' },
  { path: APP_ROUTE_PATHS.admin, name: 'admin' },
  { path: APP_ROUTE_PATHS.bookshelf, name: 'bookshelf' },
  { path: APP_ROUTE_PATHS.reader, name: 'reader' },
  { path: APP_ROUTE_PATHS.graph, name: 'graph' },
  { path: APP_ROUTE_PATHS.qa, name: 'qa' },
  { path: APP_ROUTE_PATHS.settings, name: 'settings' },
] as const

function routeQuery(params: { book?: string; chapter?: string }): Record<string, string> {
  return Object.fromEntries(
    Object.entries(params).filter((entry): entry is [string, string] => Boolean(entry[1])),
  )
}

describe('router properties', () => {
  it('resolves current app route paths to their route names', () => {
    fc.assert(
      fc.property(fc.constantFrom(...namedRouteCases), routeCase => {
        const resolved = router.resolve(routeCase.path)

        expect(resolved.name).toBe(routeCase.name)
      }),
      { numRuns: 100 },
    )
  })

  it('resolves book detail paths with the book parameter', () => {
    fc.assert(
      fc.property(fc.string({ minLength: 1, maxLength: 24 }), bookId => {
        const resolved = router.resolve({
          name: 'book-detail',
          params: { bookId },
        })

        expect(resolved.name).toBe('book-detail')
        expect(resolved.params.bookId).toBe(bookId)
      }),
      { numRuns: 100 },
    )
  })

  it('routes undefined top-level paths through the bookshelf catch-all redirect', () => {
    const reservedSegments = new Set([
      'login',
      'register',
      'recover',
      'account',
      'admin',
      'reader',
      'graph',
      'qa',
      'settings',
      'books',
      'api',
      'static',
      'assets',
      'js',
    ])

    fc.assert(
      fc.property(
        fc
          .stringOf(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789'.split('')), {
            minLength: 1,
            maxLength: 20,
          })
          .filter(segment => !reservedSegments.has(segment)),
        randomSegment => {
          const resolved = router.resolve(`/${randomSegment}`)

          expect(resolved.matched).toHaveLength(1)
          expect(resolved.matched[0]?.redirect).toEqual({ name: 'bookshelf' })
        },
      ),
      { numRuns: 100 },
    )
  })

  it('preserves reader route query parameters', () => {
    fc.assert(
      fc.property(
        fc.record({
          book: fc.option(fc.string({ minLength: 1, maxLength: 20 }), { nil: undefined }),
          chapter: fc.option(fc.string({ minLength: 1, maxLength: 20 }), { nil: undefined }),
        }),
        params => {
          const query = routeQuery(params)
          const resolved = router.resolve({ name: 'reader', query })

          expect(resolved.name).toBe('reader')
          expect(resolved.query).toMatchObject(query)
        },
      ),
      { numRuns: 100 },
    )
  })

  it('preserves the graph route book query parameter', () => {
    fc.assert(
      fc.property(fc.option(fc.string({ minLength: 1, maxLength: 20 }), { nil: undefined }), bookId => {
        const query = routeQuery({ book: bookId })
        const resolved = router.resolve({ name: 'graph', query })

        expect(resolved.name).toBe('graph')
        expect(resolved.query).toMatchObject(query)
      }),
      { numRuns: 100 },
    )
  })
})