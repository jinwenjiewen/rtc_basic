import 'whatwg-fetch';
import { beforeEach, afterEach, expect, jest, test } from '@jest/globals';
import { authenticatedFetch, AUTH_EXPIRED_EVENT, loginWithPassword } from './api';

const fetchMock = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>();
const reply = (status: number, body: object = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const originalFetch = global.fetch;

beforeEach(() => {
  fetchMock.mockReset();
  global.fetch = fetchMock;
  document.cookie = 'rtc_csrf=test-csrf; path=/';
});

afterEach(() => {
  global.fetch = originalFetch;
  document.cookie = 'rtc_csrf=; max-age=0; path=/';
});

test('refreshes an expired access token once and retries the original business request', async () => {
  fetchMock
    .mockResolvedValueOnce(reply(401))
    .mockResolvedValueOnce(reply(401))
    .mockResolvedValueOnce(reply(200))
    .mockResolvedValueOnce(reply(200, { Result: { scenes: [] } }));
  const response = await authenticatedFetch('/getScenes', { method: 'POST', body: '{}' });
  expect(response.status).toBe(200);
  expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual(
    ['/getScenes', '/api/auth/me', '/api/auth/refresh', '/getScenes']
  );
  const request = fetchMock.mock.calls[0][1]!;
  expect(request.credentials).toBe('include');
  expect(new Headers(request.headers).get('X-CSRF-Token')).toBe('test-csrf');
  expect(fetchMock.mock.calls[3][1]?.body).toBe('{}');
});

test('coalesces concurrent unauthorized requests into a single refresh', async () => {
  const attempts = new Map<string, number>();
  fetchMock.mockImplementation(async (input) => {
    const url = String(input);
    if (url.endsWith('/api/auth/me')) return reply(401);
    if (url.endsWith('/api/auth/refresh')) return reply(200);
    const count = attempts.get(url) || 0;
    attempts.set(url, count + 1);
    return reply(count ? 200 : 401);
  });
  await Promise.all([authenticatedFetch('/first'), authenticatedFetch('/second')]);
  expect(
    fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/api/auth/refresh'))
  ).toHaveLength(1);
});

test('signals session expiry when refresh is rejected', async () => {
  const expired = jest.fn();
  window.addEventListener(AUTH_EXPIRED_EVENT, expired);
  try {
    fetchMock.mockResolvedValue(reply(401, { detail: '登录已过期' }));
    await expect(authenticatedFetch('/getScenes')).rejects.toMatchObject({ status: 401 });
    expect(expired).toHaveBeenCalledTimes(1);
  } finally {
    window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
  }
});

test('preserves login state when the server is unavailable', async () => {
  const expired = jest.fn();
  window.addEventListener(AUTH_EXPIRED_EVENT, expired);
  try {
    fetchMock
      .mockResolvedValueOnce(reply(401))
      .mockResolvedValueOnce(reply(503, { detail: '服务暂时不可用' }));
    await expect(authenticatedFetch('/getScenes')).rejects.toMatchObject({ status: 503 });
    expect(expired).not.toHaveBeenCalled();
  } finally {
    window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
  }
});

test('does not refresh or retry a forbidden request', async () => {
  fetchMock.mockResolvedValue(reply(403, { detail: '请求校验失败' }));
  await expect(authenticatedFetch('/getScenes', { method: 'POST' })).rejects.toMatchObject({
    status: 403,
  });
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test('invalid credentials surface the backend message without refreshing', async () => {
  fetchMock.mockResolvedValue(reply(401, { detail: '账号或密码错误。' }));
  await expect(loginWithPassword(' alice ', 'wrong', false)).rejects.toThrow('账号或密码错误。');
  expect(JSON.parse(fetchMock.mock.calls[0][1]?.body as string).username).toBe('alice');
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
