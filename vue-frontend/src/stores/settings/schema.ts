import type { WorkflowMode } from '@/types/workflow'

type PlainRecord = Record<string, unknown>

export interface CurrentWorkflowPreferences {
  rememberWorkflowModeEnabled: boolean
  lastWorkflowMode: WorkflowMode
}

export interface CurrentExportPreferences {
  preserveOriginalFilenames: boolean
}

const CURRENT_WORKFLOW_MODES: ReadonlySet<string> = new Set([
  'translate-current',
  'translate-batch',
  'hq-batch',
  'proofread-batch',
  'remove-current',
  'remove-batch',
  'retry-failed',
  'delete-current',
  'clear-all',
])

function isPlainRecord(value: unknown): value is PlainRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function hasExactKeys(value: PlainRecord, keys: readonly string[]): boolean {
  const actual = Object.keys(value)
  return actual.length === keys.length && keys.every(key => Object.hasOwn(value, key))
}

export function parseCurrentWorkflowPreferences(
  value: unknown,
): CurrentWorkflowPreferences | null {
  if (!isPlainRecord(value) || !hasExactKeys(value, [
    'rememberWorkflowModeEnabled',
    'lastWorkflowMode',
  ])) {
    return null
  }
  if (
    typeof value.rememberWorkflowModeEnabled !== 'boolean'
    || typeof value.lastWorkflowMode !== 'string'
    || !CURRENT_WORKFLOW_MODES.has(value.lastWorkflowMode)
  ) {
    return null
  }
  return {
    rememberWorkflowModeEnabled: value.rememberWorkflowModeEnabled,
    lastWorkflowMode: value.lastWorkflowMode as WorkflowMode,
  }
}

export function parseCurrentExportPreferences(
  value: unknown,
): CurrentExportPreferences | null {
  if (
    !isPlainRecord(value)
    || !hasExactKeys(value, ['preserveOriginalFilenames'])
    || typeof value.preserveOriginalFilenames !== 'boolean'
  ) {
    return null
  }
  return {
    preserveOriginalFilenames: value.preserveOriginalFilenames,
  }
}
