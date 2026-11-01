import { getCurrentInstance, onBeforeUnmount, onMounted } from 'vue'
import {
  onApiConflict,
  type ConflictEventDetail,
  type ConflictResource,
} from '@/api/client'
import { showToast } from '@/utils/toast'

/**
 * 统一处理「乐观锁冲突」（409 `revision_conflict`）。
 *
 * 背景：后端在 `baseRevision` 落后时会拒绝整笔写入并返回 409 `revision_conflict`。
 * 过去前端只把这类冲突当普通错误弹一句「请求失败」，用户既不知道是「别人改过」
 * 也不知道该怎么办。这里在 shell 层订阅全局冲突事件，按被改资源分派：
 *
 * - 自动重新拉取该资源的最新快照（传入的 loader）；
 * - 提示「已载入最新版本，请确认后重新保存」，让用户在被覆盖的编辑前先看清现状。
 *
 * 只处理 `revision_conflict`：其它 409（`job_conflict` / `draft_locked` 等）语义不同，
 * 交回各自调用点处理，避免误报「已载入最新版本」。
 */

export interface RevisionConflictHandlers {
  /** 每类资源对应的「重新拉取最新快照」动作；缺省则只提示不刷新。 */
  reload?: Partial<Record<ConflictResource, () => Promise<unknown>>>
  /** 自定义提示文案（默认见 `DEFAULT_REVISION_CONFLICT_TOAST`）。 */
  message?: string
  /** 去重窗口（毫秒），避免多个并发请求同时 409 造成重复刷新 / 多余提示。 */
  dedupeMs?: number
}

export const DEFAULT_REVISION_CONFLICT_TOAST =
  '数据已在其他页面被修改，已为你载入最新版本，请确认后重新保存'

/** 只对这些资源做自动刷新——'other' 靠具体页面自行处理。 */
const RELOADABLE_RESOURCES: readonly ConflictResource[] = ['settings', 'task']

export function useRevisionConflict(handlers: RevisionConflictHandlers = {}) {
  const dedupeMs = handlers.dedupeMs ?? 500
  const message = handlers.message ?? DEFAULT_REVISION_CONFLICT_TOAST
  const lastHandledAt: Partial<Record<ConflictResource, number>> = {}

  async function handle(detail: ConflictEventDetail): Promise<void> {
    if (detail.code !== 'revision_conflict') return
    if (!RELOADABLE_RESOURCES.includes(detail.resource)) return

    const now = Date.now()
    const previous = lastHandledAt[detail.resource] ?? 0
    if (now - previous < dedupeMs) return
    lastHandledAt[detail.resource] = now

    const reload = handlers.reload?.[detail.resource]
    if (reload) {
      try {
        await reload()
      } catch {
        // 重新拉取失败不阻断提示：至少让用户知道需要手动刷新。
      }
    }
    showToast(message, 'info')
  }

  let unsubscribe: () => void = () => {}

  // 仅在组件 setup 内自动订阅；在 setup 外（如单测直接调用 handle）跳过，
  // 避免 Vue 生命周期钩子警告。
  if (getCurrentInstance()) {
    onMounted(() => {
      unsubscribe = onApiConflict(detail => {
        void handle(detail)
      })
    })

    onBeforeUnmount(() => {
      unsubscribe()
    })
  }

  return { handle }
}
