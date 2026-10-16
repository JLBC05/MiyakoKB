import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  getV2Settings,
  saveV2SettingsTransaction,
  type V2CredentialSummary,
  type V2ExportPreferences,
  type V2Font,
  type V2Prompt,
  type V2SettingsDocument,
  type V2SettingsTransaction,
  type V2SettingsTransactionResult,
  type V2WorkflowPreferences,
  updateV2WorkflowPreferences,
} from '@/api/v2/settings'
import { deepClone } from '@/utils/deepClone'
import { sameSettingValue } from '@/utils/providerSettings'
import {
  parseCurrentExportPreferences,
  parseCurrentWorkflowPreferences,
} from './schema'
import { useThemePreference } from './useThemePreference'

export const useSettingsStore = defineStore('settings', () => {
  const {
    theme,
    effectiveTheme,
    setTheme,
    toggleTheme,
    loadThemeFromStorage,
  } = useThemePreference()

  const credentialSummaries = ref<V2CredentialSummary[]>([])
  const fontCatalog = ref<V2Font[]>([])
  const promptCatalog = ref<V2Prompt[]>([])
  const workflowPreferences = ref<V2WorkflowPreferences>({
    rememberWorkflowModeEnabled: false,
    lastWorkflowMode: 'translate-current',
  })
  const exportPreferences = ref<V2ExportPreferences>({
    preserveOriginalFilenames: false,
  })
  const isBackendReady = ref(false)
  const backendError = ref<string | null>(null)

  let workflowPreferencesRevision = 0
  let exportPreferencesRevision = 0
  const savedPayloads = new Map<string, Record<string, unknown>>()
  let loadPromise: Promise<boolean> | null = null

  function initSettings(): void {
    loadThemeFromStorage()
  }

  function applyBackendDocument(document: V2SettingsDocument): void {
    const settingsList = Array.isArray(document.settings) ? document.settings : []
    const workflowPreferencesEntry = settingsList.find(
      row => row.domain === 'workflow_preferences',
    )
    const exportPreferencesEntry = settingsList.find(
      row => row.domain === 'export_preferences',
    )

    if (workflowPreferencesEntry) {
      const parsedWorkflowPreferences = parseCurrentWorkflowPreferences(
        workflowPreferencesEntry.payload,
      )
      if (parsedWorkflowPreferences) {
        workflowPreferencesRevision = workflowPreferencesEntry.revision
        workflowPreferences.value = parsedWorkflowPreferences
        savedPayloads.set('workflow_preferences', deepClone(workflowPreferences.value as unknown as Record<string, unknown>))
      }
    }

    if (exportPreferencesEntry) {
      const parsedExportPreferences = parseCurrentExportPreferences(
        exportPreferencesEntry.payload,
      )
      if (parsedExportPreferences) {
        exportPreferencesRevision = exportPreferencesEntry.revision
        exportPreferences.value = parsedExportPreferences
        savedPayloads.set('export_preferences', deepClone(exportPreferences.value as unknown as Record<string, unknown>))
      }
    }

    credentialSummaries.value = Array.isArray(document.credentials)
      ? document.credentials.map(deepClone)
      : []
    fontCatalog.value = Array.isArray(document.fonts)
      ? document.fonts.map(deepClone)
      : []
    promptCatalog.value = Array.isArray(document.prompts)
      ? document.prompts.map(deepClone)
      : []
  }

  function hydrateFromBackendDocument(document: V2SettingsDocument): boolean {
    try {
      applyBackendDocument(document)
      isBackendReady.value = true
      backendError.value = null
      return true
    } catch (error) {
      isBackendReady.value = false
      backendError.value = error instanceof Error ? error.message : '后端设置解析失败'
      return false
    }
  }

  function hydrateResourceCatalogs(fonts: V2Font[] = [], prompts: V2Prompt[] = []): void {
    fontCatalog.value = Array.isArray(fonts) ? fonts.map(deepClone) : []
    promptCatalog.value = Array.isArray(prompts) ? prompts.map(deepClone) : []
  }

  function upsertFont(font: V2Font): void {
    const index = fontCatalog.value.findIndex(item => item.id === font.id)
    if (index >= 0) {
      fontCatalog.value[index] = deepClone(font)
      return
    }
    fontCatalog.value.push(deepClone(font))
  }

  async function loadFromBackend(): Promise<boolean> {
    if (loadPromise) return loadPromise
    loadPromise = (async () => {
      try {
        const document = await getV2Settings()
        const hydrated = hydrateFromBackendDocument(document)
        if (!hydrated) return false
        return true
      } catch (error) {
        isBackendReady.value = false
        backendError.value = error instanceof Error ? error.message : '设置加载失败'
        return false
      }
    })()
    try {
      return await loadPromise
    } finally {
      loadPromise = null
    }
  }

  async function saveWorkflowPreferences(
    preferences: V2WorkflowPreferences,
  ): Promise<boolean> {
    try {
      const updated = await updateV2WorkflowPreferences(
        preferences,
        workflowPreferencesRevision,
      )
      workflowPreferencesRevision = updated.revision
      workflowPreferences.value = deepClone(preferences)
      return true
    } catch (error) {
      backendError.value = error instanceof Error
        ? error.message
        : '工作流偏好保存失败'
      return false
    }
  }

  function applyTransactionResult(result: V2SettingsTransactionResult, submitted: V2SettingsTransaction): void {
    result.settings.forEach((entry) => {
      if (entry.domain === 'workflow_preferences') {
        workflowPreferencesRevision = entry.revision
      }
      if (entry.domain === 'export_preferences') {
        exportPreferencesRevision = entry.revision
      }
      const change = submitted.settings?.find(row => row.domain === entry.domain)
      if (change) savedPayloads.set(entry.domain, deepClone(change.payload))
    })
  }

  function buildSettingsTransaction(): V2SettingsTransaction {
    return {
      settings: [
        {
          domain: 'export_preferences',
          payload: deepClone(exportPreferences.value) as unknown as Record<string, unknown>,
          baseRevision: exportPreferencesRevision,
        },
      ].filter(row => !sameSettingValue(row.payload, savedPayloads.get(row.domain))),
    }
  }

  async function saveToBackend(): Promise<boolean> {
    if (!isBackendReady.value) {
      backendError.value = '后端设置尚未加载，已阻止覆盖保存'
      return false
    }
    try {
      const submitted = buildSettingsTransaction()
      if (!submitted.settings?.length) {
        backendError.value = null
        return true
      }
      const result = await saveV2SettingsTransaction(submitted)
      applyTransactionResult(result, submitted)
      backendError.value = null
      return true
    } catch (error) {
      backendError.value = error instanceof Error ? error.message : '设置保存失败'
      return false
    }
  }

  return {
    credentialSummaries,
    fontCatalog,
    promptCatalog,
    workflowPreferences,
    exportPreferences,
    isBackendReady,
    backendError,
    theme,
    effectiveTheme,

    setTheme,
    toggleTheme,
    loadThemeFromStorage,
    initSettings,
    hydrateFromBackendDocument,
    hydrateResourceCatalogs,
    upsertFont,

    loadFromBackend,
    saveToBackend,
    saveWorkflowPreferences,
  }
})
