import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import router from '@/router'
import { APP_ROUTE_PATHS } from '@/constants/routes'
import { useRuntimeStore } from '@/stores/runtimeStore'

// 说明：translate / insight / character-studio 路由随翻译与角色工坊域在
// 7e6e8d2 chore(cleanup) 中删除，这里改为校验当前仍在的路由表。
const routeCases = [
  { label: 'bookshelf', name: 'bookshelf', path: APP_ROUTE_PATHS.bookshelf },
  { label: 'reader', name: 'reader', path: APP_ROUTE_PATHS.reader },
  { label: 'book detail', name: 'book-detail', path: APP_ROUTE_PATHS.bookDetail },
  { label: 'graph', name: 'graph', path: APP_ROUTE_PATHS.graph },
  { label: 'qa', name: 'qa', path: APP_ROUTE_PATHS.qa },
  { label: 'settings', name: 'settings', path: APP_ROUTE_PATHS.settings },
] as const

describe('router config', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    useRuntimeStore().assumeLocalForTests()
    await router.push(APP_ROUTE_PATHS.bookshelf)
  })

  it.each(routeCases)('contains the $label route', ({ name, path }) => {
    const route = router.getRoutes().find(candidate => candidate.name === name)

    expect(route).toBeDefined()
    expect(route?.path).toBe(path)
  })

  it('redirects the account-only route out of the local profile', async () => {
    await router.push(APP_ROUTE_PATHS.account)

    expect(router.currentRoute.value.name).toBe('bookshelf')
  })
})
