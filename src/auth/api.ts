import { AIGC_PROXY_HOST } from '@/config';

export interface AuthUser {
  id: string;
  username: string;
}

export const AUTH_EXPIRED_EVENT = 'rtc:auth-expired';

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

function csrfToken() {
  return document.cookie
    .split('; ')
    .find((cookie) => cookie.startsWith('rtc_csrf='))
    ?.slice('rtc_csrf='.length);
}

async function rawFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  const csrf = csrfToken();
  if (csrf && !['GET', 'HEAD', 'OPTIONS'].includes((init.method || 'GET').toUpperCase())) {
    headers.set('X-CSRF-Token', csrf);
  }
  try {
    return await fetch(`${AIGC_PROXY_HOST}${path}`, {
      ...init,
      headers,
      credentials: 'include',
      cache: 'no-store',
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, '无法连接服务，请检查网络或确认后端已启动。');
  }
}

async function responseError(response: Response) {
  const body = await response.json().catch(() => ({}));
  return new ApiError(
    response.status,
    typeof body.detail === 'string' ? body.detail : '请求未能完成，请稍后重试。'
  );
}

let refreshing: Promise<void> | null = null;

export async function refreshSession() {
  if (!refreshing) {
    const refresh = async () => {
      // Another tab may have refreshed while this tab waited for the lock.
      const current = await rawFetch('/api/auth/me');
      if (current.ok) return;
      if (current.status !== 401) throw await responseError(current);
      const response = await rawFetch('/api/auth/refresh', { method: 'POST' });
      if (!response.ok) throw await responseError(response);
    };
    // Web Locks serialize rotation across tabs. The promise also covers browsers
    // without Web Locks and concurrent requests within the same tab.
    refreshing = (
      navigator.locks ? navigator.locks.request('rtc-auth-refresh', refresh) : refresh()
    ).finally(() => {
      refreshing = null;
    });
  }
  return refreshing;
}

export async function authenticatedFetch(path: string, init: RequestInit = {}) {
  let response = await rawFetch(path, init);
  if (response.status === 401) {
    try {
      await refreshSession();
      response = await rawFetch(path, init);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
      }
      throw error;
    }
  }
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    throw await responseError(response);
  }
  return response;
}

export async function fetchCurrentUser(): Promise<AuthUser> {
  const response = await authenticatedFetch('/api/auth/me');
  return (await response.json()).user;
}

export async function loginWithPassword(username: string, password: string, remember: boolean) {
  const response = await rawFetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: username.trim(), password, remember }),
  });
  if (!response.ok) throw await responseError(response);
  return (await response.json()).user as AuthUser;
}

export async function logoutSession() {
  await authenticatedFetch('/api/auth/logout', { method: 'POST' });
}
