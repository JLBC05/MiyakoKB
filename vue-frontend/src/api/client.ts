import axios, { type AxiosInstance, type AxiosRequestConfig, type AxiosError } from 'axios'
import type { ApiError } from '@/types'

interface ApiClientErrorInit {
  code: string
  message: string
  status: number
  details?: Record<string, unknown>
}

export class ApiClientError extends Error implements ApiError {
  readonly code: string
  readonly status: number
  readonly details?: Record<string, unknown>

  constructor({ code, message, status, details }: ApiClientErrorInit) {
    super(message)
    this.name = 'ApiClientError'
    Object.setPrototypeOf(this, new.target.prototype)
    this.code = code
    this.status = status
    this.details = details
  }
}

/** 统一冲突事件：任何 409 都会在 window 上广播一次，供 UI 统一刷新快照。 */
export const CONFLICT_EVENT = 'miyako:conflict' as const

export interface ConflictEventDetail {
  /** 后端返回的业务错误码，例如 ``revision_conflict`` / ``invalid_job_transition``。 */
  code: string
  /** 后端原始提示，可能为空字符串。 */
  message: string
  /** 触发冲突的请求路径，便于 UI 判断要不要刷新对应面板。 */
  url: string
  /** HTTP 方法（小写）。 */
  method: string
}

/**
 * 订阅统一冲突事件。
 *
 * 返回取消订阅函数，调用方应在 ``onUnmounted`` 里调用，避免重复注册。
 */
export function onApiConflict(
  handler: (detail: ConflictEventDetail) => void,
): () => void {
  if (typeof window === 'undefined') return () => {}
  const listener = (event: Event) => {
    const detail = (event as CustomEvent<ConflictEventDetail>).detail
    if (detail) handler(detail)
  }
  window.addEventListener(CONFLICT_EVENT, listener)
  return () => window.removeEventListener(CONFLICT_EVENT, listener)
}

function createApiError(error: AxiosError): ApiError {
  const response = error.response
  const data = response?.data
  const dataObject = data && typeof data === 'object' && !Array.isArray(data)
    ? data as Record<string, unknown>
    : undefined
  const backendError = dataObject?.error && typeof dataObject.error === 'object'
    ? dataObject.error as Record<string, unknown>
    : undefined
  const backendCode = typeof backendError?.code === 'string'
    ? backendError.code
    : undefined
  const backendMessage = typeof backendError?.message === 'string'
    ? backendError.message
    : undefined
  const backendDetails = backendError?.details
  const inlineDetails = backendError
    ? Object.fromEntries(
        Object.entries(backendError).filter(([key]) => !['code', 'message', 'details'].includes(key)),
      )
    : undefined
  const contentType = String(response?.headers?.['content-type'] || '').toLowerCase()
  const isNetworkFailure = response === undefined
  const isCanceled = error.code === 'ERR_CANCELED' || error.name === 'CanceledError'
  const isDevelopmentProxyFailure = response?.status === 500
    && backendError === undefined
    && (data === '' || data === undefined || contentType.startsWith('text/plain'))

  return new ApiClientError({
    code: backendCode
      || (isCanceled ? 'request_canceled' : undefined)
      || (isNetworkFailure ? 'network_error' : undefined)
      || (isDevelopmentProxyFailure ? 'proxy_connection_error' : undefined)
      || error.code
      || 'UNKNOWN_ERROR',
    message: backendMessage
      || (isCanceled ? '请求已取消' : undefined)
      || (isNetworkFailure ? '无法连接后端服务，请稍后重试' : undefined)
      || (isDevelopmentProxyFailure ? '开发代理与后端连接中断，请稍后重试' : undefined)
      || error.message,
    status: response?.status ?? 0,
    details: backendDetails && typeof backendDetails === 'object' && !Array.isArray(backendDetails)
      ? backendDetails as Record<string, unknown>
      : inlineDetails && Object.keys(inlineDetails).length > 0
        ? inlineDetails
        : undefined,
  })
}

export function isRequestCanceled(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') return true
  if (error instanceof ApiClientError) return error.code === 'request_canceled'
  if (!(error instanceof Error)) return false
  const code = (error as Error & { code?: unknown }).code
  return error.name === 'CanceledError' || code === 'ERR_CANCELED'
}

class ApiClient {
  private instance: AxiosInstance

  constructor() {
    this.instance = axios.create({
      baseURL: '',
      timeout: 300000,
      withCredentials: true,
    })

    this.instance.interceptors.request.use((config) => {
      const method = (config.method ?? 'get').toLowerCase()
      if (!['get', 'head', 'options'].includes(method) && typeof document !== 'undefined') {
        const csrf = document.cookie
          .split('; ')
          .find(value => value.startsWith('saber_csrf='))
          ?.slice('saber_csrf='.length)
        if (csrf) config.headers.set('X-CSRF-Token', decodeURIComponent(csrf))
      }
      return config
    })

    this.instance.interceptors.response.use(
      response => response,
      (error: AxiosError) => {
        const apiError = createApiError(error)
        const isSessionProbe = error.config?.url === '/api/v2/auth/me'
        if (apiError.status === 401 && !isSessionProbe && typeof window !== 'undefined') {
          window.dispatchEvent(new CustomEvent('saber:authentication-required'))
        }
        // 审计 P2-1：409 在 124 条路径上代表「你手上的快照过期了」。过去每个
        // 调用点各自弹一句「冲突」，用户不知道该刷新什么。这里统一广播一次，
        // 由 shell 层刷新任务中心与相关面板；调用点照旧展示各自的提示。
        if (apiError.status === 409 && typeof window !== 'undefined') {
          const detail: ConflictEventDetail = {
            code: apiError.code,
            message: apiError.message,
            url: String(error.config?.url ?? ''),
            method: String(error.config?.method ?? 'get').toLowerCase(),
          }
          window.dispatchEvent(new CustomEvent<ConflictEventDetail>(CONFLICT_EVENT, { detail }))
        }
        return Promise.reject(apiError)
      }
    )
  }

  async get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.instance.get<T>(url, config)
    return response.data
  }

  async post<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.instance.post<T>(url, data, config)
    return response.data
  }

  async put<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.instance.put<T>(url, data, config)
    return response.data
  }

  async patch<T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.instance.patch<T>(url, data, config)
    return response.data
  }

  async delete<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.instance.delete<T>(url, config)
    return response.data
  }

  async upload<T>(
    url: string,
    formData: FormData,
    config?: AxiosRequestConfig,
    method: 'post' | 'put' = 'post',
  ): Promise<T> {
    const response = method === 'put'
      ? await this.instance.put<T>(url, formData, config)
      : await this.instance.post<T>(url, formData, config)
    return response.data
  }

}

export const apiClient = new ApiClient()

export type { ApiError }
