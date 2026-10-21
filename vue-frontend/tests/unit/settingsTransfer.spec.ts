import { describe, expect, it } from 'vitest'

import type { V2SettingsDocument } from '@/api/v2/settings'
import {
  buildImportTransaction,
  buildSettingsExport,
  parseSettingsExport,
  settingsExportFilename,
} from '@/utils/settingsTransfer'

function documentFixture(): V2SettingsDocument {
  return {
    settings: [
      { domain: 'insight', revision: 7, payload: { vlm: { provider: 'custom' } } },
      { domain: 'manga_import', revision: 2, payload: { veneraBaseDir: 'D:/venera' } },
    ],
    bookSettings: [],
    providerSettings: [
      {
        domain: 'insight_vlm',
        provider: 'custom',
        revision: 5,
        payload: { modelName: 'gemini-3.8-flash-high', imageMaxSize: 1024 },
      },
    ],
    credentials: [
      {
        credentialId: '11111111-1111-4111-8111-111111111111',
        credentialVersionId: '22222222-2222-4222-8222-222222222222',
        domain: 'insight_vlm',
        provider: 'custom',
        secret: { api_key: '' },
        hasKey: true,
        currentVersion: 3,
        revision: 4,
      },
    ],
  } as V2SettingsDocument
}

describe('settings export', () => {
  it('keeps settings and provider payloads but never the credential list', () => {
    const backup = buildSettingsExport(documentFixture(), '2026-10-21T14:00:00.000Z')

    expect(backup.format).toBe('miyakokb-settings')
    expect(backup.settings).toEqual([
      { domain: 'insight', payload: { vlm: { provider: 'custom' } } },
      { domain: 'manga_import', payload: { veneraBaseDir: 'D:/venera' } },
    ])
    expect(backup.providerSettings).toEqual([
      {
        domain: 'insight_vlm',
        provider: 'custom',
        payload: { modelName: 'gemini-3.8-flash-high', imageMaxSize: 1024 },
      },
    ])
    expect(JSON.stringify(backup)).not.toContain('credential')
    expect(JSON.stringify(backup)).not.toContain('api_key')
  })

  it('names the file after the export moment', () => {
    expect(settingsExportFilename(new Date(2026, 9, 21, 22, 5))).toBe('miyakokb-settings-20261021-2205.json')
  })
})

describe('settings import', () => {
  it('rejects broken or foreign files', () => {
    expect(() => parseSettingsExport('{')).toThrow('文件不是合法的 JSON')
    expect(() => parseSettingsExport('{"format":"other"}')).toThrow('不是 MiyakoKB 设置备份文件')
  })

  it('turns a backup into a transaction pinned to the current revisions', () => {
    const backup = parseSettingsExport(JSON.stringify(buildSettingsExport(documentFixture(), 'now')))
    const plan = buildImportTransaction(backup, documentFixture())

    expect(plan.settingCount).toBe(2)
    expect(plan.providerCount).toBe(1)
    expect(plan.skippedDomains).toEqual([])
    expect(plan.transaction.settings).toEqual([
      { domain: 'insight', payload: { vlm: { provider: 'custom' } }, baseRevision: 7 },
      { domain: 'manga_import', payload: { veneraBaseDir: 'D:/venera' }, baseRevision: 2 },
    ])
    expect(plan.transaction.providerSettings).toEqual([
      {
        domain: 'insight_vlm',
        provider: 'custom',
        payload: { modelName: 'gemini-3.8-flash-high', imageMaxSize: 1024 },
        baseRevision: 5,
      },
    ])
  })

  it('skips domains the current installation does not have', () => {
    const backup = parseSettingsExport(JSON.stringify({
      format: 'miyakokb-settings',
      version: 1,
      exportedAt: 'now',
      settings: [{ domain: 'retired_domain', payload: {} }],
      providerSettings: [{ domain: 'insight_vlm', provider: 'gone', payload: {} }],
    }))
    const plan = buildImportTransaction(backup, documentFixture())

    expect(plan.settingCount).toBe(0)
    expect(plan.providerCount).toBe(0)
    expect(plan.skippedDomains).toEqual(['retired_domain', 'insight_vlm/gone'])
  })
})
