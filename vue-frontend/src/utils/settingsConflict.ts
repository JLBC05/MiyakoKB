/**
 * 设置保存的版本冲突识别。
 *
 * 后端 `PUT /settings/transactions` 在 `baseRevision` 落后时会拒绝整笔事务，
 * 前端把这种错误与普通网络错误分开处理：冲突说明别的页面已经改了同一份设置，
 * 继续自动保存只会覆盖别人的修改，因此要停下来让人重新加载。
 */
const REVISION_CONFLICT_HINTS = ['已被其他页面更新', '已被其他页面修改'] as const

export const REVISION_CONFLICT_MESSAGE = '设置已在其他页面被修改，本次保存已取消，请重新加载后再编辑'

export function isRevisionConflictError(error: unknown): boolean {
  const text = error instanceof Error ? error.message : typeof error === 'string' ? error : ''
  return REVISION_CONFLICT_HINTS.some(hint => text.includes(hint))
}
