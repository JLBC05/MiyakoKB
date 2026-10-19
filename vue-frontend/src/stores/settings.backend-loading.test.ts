import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const settingsApiMocks = vi.hoisted(() => ({
  getV2Settings: vi.fn(),
  saveV2SettingsTransaction: vi.fn(),
  updateV2WorkflowPreferences: vi.fn(),
}))

vi.mock('@/api/v2/settings', () => ({
  getV2Settings: settingsApiMocks.getV2Settings,
  saveV2SettingsTransaction: settingsApiMocks.saveV2SettingsTransaction,
  updateV2WorkflowPreferences: settingsApiMocks.updateV2WorkflowPreferences,
}))

import { useSettingsStore } from './settings'

function backendDocument(overrides: {
  exportPayload?: Record<string, unknown>
  exportRevision?: number
  workflowPayload?: Record<string, unknown>
} = {}) {
  return {
    settings: [
      {
        domain: 'workflow_preferences',
        revision: 3,
        payload: overrides.workflowPayload ?? {
          rememberWorkflowModeEnabled: true,
          lastWorkflowMode: 'retry-failed',
        },
      },
      {
        domain: 'export_preferences',
        revision: overrides.exportRevision ?? 7,
        payload: overrides.exportPayload ?? { preserveOriginalFilenames: true },
      },
    ],
    bookSettings: [],
    providerSettings: [
      {
        domain: 'insight_vlm',
        provider: 'custom',
        revision: 2,
        payload: { model: 'vision-model' },
      },
    ],
    credentials: [
      {
        credentialId: '11111111-1111-4111-8111-111111111111',
        credentialVersionId: '22222222-2222-4222-8222-222222222222',
        domain: 'insight_vlm',
        provider: 'custom',
        secret: {},
        hasKey: true,
        currentVersion: 1,
        revision: 4,
      },
    ],
  }
}

describe('useSettingsStore backend-first loading', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    settingsApiMocks.getV2Settings.mockReset()
    settingsApiMocks.saveV2SettingsTransaction.mockReset()
    settingsApiMocks.updateV2WorkflowPreferences.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('blocks backend writes while authoritative settings are unavailable', async () => {
    settingsApiMocks.getV2Settings.mockRejectedValue(new Error('offline'))

    const store = useSettingsStore()
    expect(await store.loadFromBackend()).toBe(false)
    expect(store.isBackendReady).toBe(false)
    expect(store.backendError).toBe('offline')
    expect(await store.saveToBackend()).toBe(false)
    expect(store.backendError).toContain('后端设置尚未加载')
    expect(settingsApiMocks.saveV2SettingsTransaction).not.toHaveBeenCalled()
  })

  it('hydrates the authoritative domains, provider settings and credentials', async () => {
    settingsApiMocks.getV2Settings.mockResolvedValue(backendDocument() as never)

    const store = useSettingsStore()
    expect(await store.loadFromBackend()).toBe(true)
    expect(store.isBackendReady).toBe(true)
    expect(store.backendError).toBeNull()
    expect(store.workflowPreferences).toEqual({
      rememberWorkflowModeEnabled: true,
      lastWorkflowMode: 'retry-failed',
    })
    expect(store.exportPreferences).toEqual({ preserveOriginalFilenames: true })
    expect(store.credentialSummaries).toHaveLength(1)
    expect(store.credentialSummaries[0]?.credentialId).toBe('11111111-1111-4111-8111-111111111111')
  })

  it('ignores a payload that does not match the current schema', async () => {
    settingsApiMocks.getV2Settings.mockResolvedValue(
      backendDocument({
        exportPayload: { preserveOriginalFilenames: 'yes' },
        workflowPayload: { lastWorkflowMode: 'not-a-current-mode' },
      }) as never,
    )

    const store = useSettingsStore()
    expect(await store.loadFromBackend()).toBe(true)
    expect(store.exportPreferences).toEqual({ preserveOriginalFilenames: false })
    expect(store.workflowPreferences).toEqual({
      rememberWorkflowModeEnabled: false,
      lastWorkflowMode: 'translate-current',
    })
  })

  it('saves only the changed domain and reuses the revision returned by the server', async () => {
    settingsApiMocks.getV2Settings.mockResolvedValue(backendDocument() as never)
    settingsApiMocks.saveV2SettingsTransaction.mockResolvedValue({
      settings: [
        {
          domain: 'export_preferences',
          revision: 8,
          payload: { preserveOriginalFilenames: false },
        },
      ],
    } as never)

    const store = useSettingsStore()
    expect(await store.loadFromBackend()).toBe(true)

    store.exportPreferences.preserveOriginalFilenames = false
    expect(await store.saveToBackend()).toBe(true)
    expect(settingsApiMocks.saveV2SettingsTransaction).toHaveBeenCalledTimes(1)
    expect(settingsApiMocks.saveV2SettingsTransaction).toHaveBeenCalledWith({
      settings: [
        {
          domain: 'export_preferences',
          payload: { preserveOriginalFilenames: false },
          baseRevision: 7,
        },
      ],
    })

    store.exportPreferences.preserveOriginalFilenames = true
    expect(await store.saveToBackend()).toBe(true)
    expect(settingsApiMocks.saveV2SettingsTransaction).toHaveBeenCalledTimes(2)
    expect(settingsApiMocks.saveV2SettingsTransaction).toHaveBeenLastCalledWith({
      settings: [
        {
          domain: 'export_preferences',
          payload: { preserveOriginalFilenames: true },
          baseRevision: 8,
        },
      ],
    })
  })

  it('treats an unchanged document as nothing to save', async () => {
    settingsApiMocks.getV2Settings.mockResolvedValue(backendDocument() as never)

    const store = useSettingsStore()
    expect(await store.loadFromBackend()).toBe(true)
    expect(await store.saveToBackend()).toBe(true)
    expect(settingsApiMocks.saveV2SettingsTransaction).not.toHaveBeenCalled()
  })

  it('keeps backend failures visible to the caller', async () => {
    settingsApiMocks.getV2Settings.mockResolvedValue(backendDocument() as never)
    settingsApiMocks.saveV2SettingsTransaction.mockRejectedValue(new Error('revision conflict'))

    const store = useSettingsStore()
    expect(await store.loadFromBackend()).toBe(true)

    store.exportPreferences.preserveOriginalFilenames = false
    expect(await store.saveToBackend()).toBe(false)
    expect(store.backendError).toContain('revision conflict')
  })

  it('replaces and appends resource catalog entries by id', () => {
    const store = useSettingsStore()

    store.hydrateResourceCatalogs(
      [{ id: 'font-a', label: 'Font A' }] as never,
      [{ id: 'prompt-a', type: 'analysis_system' }] as never,
    )
    expect(store.fontCatalog).toHaveLength(1)
    expect(store.promptCatalog).toHaveLength(1)

    store.upsertFont({ id: 'font-a', label: 'Font A v2' } as never)
    expect(store.fontCatalog).toHaveLength(1)
    expect(store.fontCatalog[0]?.label).toBe('Font A v2')

    store.upsertFont({ id: 'font-b', label: 'Font B' } as never)
    expect(store.fontCatalog.map(font => font.id)).toEqual(['font-a', 'font-b'])
  })
})