import type { V2SettingsDocument, V2SettingsTransaction } from '@/api/v2/settings'

/**
 * 设置的整库导出 / 导入。
 *
 * 导出只包含「可重建的配置」：普通设置域、供应商配置、提示词。**凭据永不导出**——
 * 读取路径已经只回传掩码，这里再显式剔除一遍，避免将来的字段变动把密钥漏进备份文件。
 *
 * 导入时用「导入前刚取到的当前修订号」构造事务，语义是覆盖式恢复；缺失的凭据需要用户
 * 重新填写，导入完成后界面会显示哪些通道还缺密钥。
 */

export type SettingsExportDocument = {
  format: 'miyakokb-settings'
  version: number
  exportedAt: string
  settings: Array<{ domain: string; payload: Record<string, unknown> }>
  providerSettings: Array<{ domain: string; provider: string; payload: Record<string, unknown> }>
}

export const SETTINGS_EXPORT_FORMAT = 'miyakokb-settings'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 从 `GET /api/v2/settings` 的响应构造可落盘备份；凭据一律丢弃。 */
export function buildSettingsExport(
  document: V2SettingsDocument,
  exportedAt: string,
): SettingsExportDocument {
  return {
    format: SETTINGS_EXPORT_FORMAT,
    version: 1,
    exportedAt,
    settings: (document.settings ?? []).map(entry => ({
      domain: entry.domain,
      payload: isRecord(entry.payload) ? entry.payload : {},
    })),
    providerSettings: (document.providerSettings ?? []).map(entry => ({
      domain: entry.domain,
      provider: entry.provider,
      payload: isRecord(entry.payload) ? entry.payload : {},
    })),
  }
}

export function settingsExportFilename(now: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `miyakokb-settings-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
    + `-${pad(now.getHours())}${pad(now.getMinutes())}.json`
}

export type ImportPlan = {
  transaction: Pick<V2SettingsTransaction, 'settings' | 'providerSettings'>
  settingCount: number
  providerCount: number
  skippedDomains: string[]
}

/** 校验备份文件形状；形状不对时抛错，避免把半截 JSON 写进设置。 */
export function parseSettingsExport(raw: string): SettingsExportDocument {
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    throw new Error('文件不是合法的 JSON')
  }
  if (!isRecord(parsed) || parsed.format !== SETTINGS_EXPORT_FORMAT) {
    throw new Error('不是 MiyakoKB 设置备份文件')
  }
  const settings = Array.isArray(parsed.settings) ? parsed.settings : []
  const providerSettings = Array.isArray(parsed.providerSettings) ? parsed.providerSettings : []
  const result: SettingsExportDocument = {
    format: SETTINGS_EXPORT_FORMAT,
    version: typeof parsed.version === 'number' ? parsed.version : 1,
    exportedAt: typeof parsed.exportedAt === 'string' ? parsed.exportedAt : '',
    settings: settings
      .filter((entry): entry is { domain: string; payload: Record<string, unknown> } =>
        isRecord(entry) && typeof entry.domain === 'string' && isRecord(entry.payload))
      .map(entry => ({ domain: entry.domain, payload: entry.payload })),
    providerSettings: providerSettings
      .filter((entry): entry is { domain: string; provider: string; payload: Record<string, unknown> } =>
        isRecord(entry) && typeof entry.domain === 'string'
        && typeof entry.provider === 'string' && isRecord(entry.payload))
      .map(entry => ({ domain: entry.domain, provider: entry.provider, payload: entry.payload })),
  }
  return result
}

/**
 * 把备份转换成一次设置事务：修订号取「当前库里的实际值」，
 * 备份里有、当前库里没有的域会作为新建（baseRevision = 0）。
 */
export function buildImportTransaction(
  backup: SettingsExportDocument,
  current: V2SettingsDocument,
): ImportPlan {
  const settingRevisions = new Map(
    (current.settings ?? []).map(entry => [entry.domain, entry.revision]),
  )
  const providerRevisions = new Map(
    (current.providerSettings ?? []).map(entry => [
      `${entry.domain}::${entry.provider}`,
      entry.revision,
    ]),
  )
  const skippedDomains: string[] = []

  const settings = backup.settings.flatMap((entry) => {
    const revision = settingRevisions.get(entry.domain)
    if (revision === undefined && entry.domain !== 'insight') {
      // 只允许「当前库已有」的域被覆盖；未知域（例如未来新增或已退役）跳过，
      // 免得导入旧备份时给后端塞不认识的载荷。
      skippedDomains.push(entry.domain)
      return []
    }
    return [{
      domain: entry.domain,
      payload: entry.payload,
      baseRevision: revision ?? 0,
    }]
  })

  const providerSettings = backup.providerSettings.flatMap((entry) => {
    const revision = providerRevisions.get(`${entry.domain}::${entry.provider}`)
    if (revision === undefined) {
      skippedDomains.push(`${entry.domain}/${entry.provider}`)
      return []
    }
    return [{
      domain: entry.domain,
      provider: entry.provider,
      payload: entry.payload,
      baseRevision: revision,
    }]
  })

  return {
    transaction: { settings, providerSettings },
    settingCount: settings.length,
    providerCount: providerSettings.length,
    skippedDomains,
  }
}
