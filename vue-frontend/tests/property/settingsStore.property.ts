import { afterEach, describe, expect, it, vi } from 'vitest'
import * as fc from 'fast-check'
import { createPinia, setActivePinia } from 'pinia'

import { STORAGE_KEY_THEME } from '@/constants'
import { useSettingsStore } from '@/stores/settings'

type ThemePreference = 'light' | 'dark' | 'system'

function createSettingsStore() {
  setActivePinia(createPinia())
  return useSettingsStore()
}

function installStorageMock() {
  const storage: Record<string, string> = {}
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(key => storage[key] ?? null)
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation((key, value) => {
    storage[key] = value
  })
  return storage
}

describe('settings store properties', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('persists only the UI theme preference in browser storage', () => {
    const storage = installStorageMock()
    fc.assert(fc.property(
      fc.constantFrom<ThemePreference>('light', 'dark', 'system'),
      theme => {
        const store = createSettingsStore()
        store.setTheme(theme)

        expect(store.theme).toBe(theme)
        expect(storage[STORAGE_KEY_THEME]).toBe(theme)
      },
    ))
  })
})