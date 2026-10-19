import textStyleDefaultsJson from '../../../src/shared/text_style_defaults_factory.json'
import type { ImageData } from '@/types/image'
import type { TextStyleSettings } from '@/types/settings'

export type TextStyleDefaults = TextStyleSettings

const rawDefaults = textStyleDefaultsJson as Record<string, unknown>

type ImageTextStyleFields = Pick<
  ImageData,
  | 'fontSize'
  | 'autoFontSize'
  | 'fontFamily'
  | 'layoutDirection'
  | 'textColor'
  | 'fillColor'
  | 'inpaintMethod'
  | 'strokeEnabled'
  | 'strokeColor'
  | 'strokeWidth'
  | 'lineSpacing'
  | 'inlineAlign'
  | 'blockAlign'
  | 'useAutoTextColor'
>

type TextStyleFieldSource = Partial<Record<keyof TextStyleSettings, unknown>>
function failInvalidConfig(message: string): never {
  throw new Error(`[textStyleDefaults] ${message}`)
}

function expectTextDirection(value: unknown, fieldName: string): TextStyleSettings['layoutDirection'] {
  if (value === 'vertical' || value === 'horizontal' || value === 'auto') {
    return value
  }
  return failInvalidConfig(`${fieldName} must be one of auto/vertical/horizontal`)
}

function expectLogicalAlign(value: unknown, fieldName: string): TextStyleSettings['inlineAlign'] {
  if (value === 'start' || value === 'center' || value === 'end') {
    return value
  }
  return failInvalidConfig(`${fieldName} must be one of start/center/end`)
}

function expectInpaintMethod(value: unknown, fieldName: string): TextStyleSettings['inpaintMethod'] {
  if (value === 'solid' || value === 'lama_mpe' || value === 'litelama' || value === 'lama_manga') {
    return value
  }
  return failInvalidConfig(`${fieldName} must be one of solid/lama_mpe/litelama/lama_manga`)
}

function expectPositiveInt(value: unknown, fieldName: string): number {
  if (typeof value === 'number' && Number.isInteger(value) && value > 0) {
    return value
  }
  return failInvalidConfig(`${fieldName} must be a positive integer`)
}

function expectNonNegativeNumber(value: unknown, fieldName: string): number {
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0) {
    return value
  }
  return failInvalidConfig(`${fieldName} must be a non-negative number`)
}

function expectPositiveFloat(value: unknown, fieldName: string): number {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
    return value
  }
  return failInvalidConfig(`${fieldName} must be a positive number`)
}

function expectBoolean(value: unknown, fieldName: string): boolean {
  if (typeof value === 'boolean') {
    return value
  }
  return failInvalidConfig(`${fieldName} must be boolean`)
}

function expectNonEmptyString(value: unknown, fieldName: string): string {
  if (typeof value === 'string' && value.length > 0) {
    return value
  }
  return failInvalidConfig(`${fieldName} must be a non-empty string`)
}

function readTextStyleField<T>(
  source: TextStyleFieldSource,
  fieldName: keyof TextStyleSettings,
  parser: (value: unknown, fieldName: string) => T,
  base?: TextStyleSettings
): T {
  const value = source[fieldName]
  if (value !== undefined) {
    return parser(value, fieldName)
  }
  if (base) {
    return base[fieldName] as T
  }
  return failInvalidConfig(`${fieldName} is required`)
}

function buildTextStyleFields(
  source: Record<string, unknown> | Partial<TextStyleSettings> | Partial<ImageTextStyleFields>,
  base?: TextStyleSettings
): TextStyleSettings {
  const fields = source as TextStyleFieldSource
  return {
    fontSize: readTextStyleField(fields, 'fontSize', expectPositiveInt, base),
    autoFontSize: readTextStyleField(fields, 'autoFontSize', expectBoolean, base),
    fontFamily: readTextStyleField(fields, 'fontFamily', expectNonEmptyString, base),
    layoutDirection: readTextStyleField(fields, 'layoutDirection', expectTextDirection, base),
    textColor: readTextStyleField(fields, 'textColor', expectNonEmptyString, base),
    fillColor: readTextStyleField(fields, 'fillColor', expectNonEmptyString, base),
    strokeEnabled: readTextStyleField(fields, 'strokeEnabled', expectBoolean, base),
    strokeColor: readTextStyleField(fields, 'strokeColor', expectNonEmptyString, base),
    strokeWidth: readTextStyleField(fields, 'strokeWidth', expectNonNegativeNumber, base),
    inpaintMethod: readTextStyleField(fields, 'inpaintMethod', expectInpaintMethod, base),
    useAutoTextColor: readTextStyleField(fields, 'useAutoTextColor', expectBoolean, base),
    lineSpacing: readTextStyleField(fields, 'lineSpacing', expectPositiveFloat, base),
    inlineAlign: readTextStyleField(fields, 'inlineAlign', expectLogicalAlign, base),
    blockAlign: readTextStyleField(fields, 'blockAlign', expectLogicalAlign, base)
  }
}

function parseTextStyleDefaults(source: Record<string, unknown> | TextStyleSettings): TextStyleDefaults {
  return buildTextStyleFields(source)
}

const BUNDLED_TEXT_STYLE_DEFAULTS = Object.freeze(parseTextStyleDefaults(rawDefaults))

export const TEXT_STYLE_DEFAULTS: Readonly<TextStyleDefaults> = BUNDLED_TEXT_STYLE_DEFAULTS

export function getTextStyleDefaults(): TextStyleDefaults {
  return { ...TEXT_STYLE_DEFAULTS }
}

export function normalizeTextStyleSettings(
  style?: Partial<TextStyleSettings> | null
): TextStyleSettings {
  const base = getTextStyleDefaults()
  return buildTextStyleFields(style ?? {}, base)
}

export function normalizeImageTextStyleFields(
  image?: Partial<ImageData> | Record<string, unknown> | null
): ImageTextStyleFields {
  return buildTextStyleFields(image ?? {}, getTextStyleDefaults())
}
