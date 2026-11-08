import { act } from 'react';
import { createRoot, Root } from 'react-dom/client';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import type { Mock } from 'jest-mock';
import { useAuth } from './AuthProvider';
import ProtectedRoute from './ProtectedRoute';

jest.mock('./AuthProvider', () => {
  const { jest: mockJest } = require('@jest/globals');
  return { useAuth: mockJest.fn() };
});
const authMock = useAuth as Mock<ReturnType<typeof useAuth>, []>;
let root: Root;
let container: HTMLDivElement;

function LocationProbe() {
  const location = useLocation();
  return (
    <span>
      {location.pathname}:{location.state?.from}
    </span>
  );
}

function render() {
  act(() =>
    root.render(
      <MemoryRouter initialEntries={['/conversation?tab=voice']}>
        <Routes>
          <Route
            path="/login"
            element={
              <>
                <div>登录页</div>
                <LocationProbe />
              </>
            }
          />
          <Route element={<ProtectedRoute />}>
            <Route path="/conversation" element={<div>受保护的对话空间</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    )
  );
}

beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  authMock.mockReturnValue({
    user: null,
    loading: false,
    error: '',
    expired: false,
    retry: async () => {},
    login: async () => {},
    logout: async () => {},
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test('redirects anonymous visitors and preserves their intended destination', () => {
  render();
  expect(container.textContent).toContain('登录页');
  expect(container.textContent).toContain('/login:/conversation?tab=voice');
  expect(container.textContent).not.toContain('受保护的对话空间');
});

test('never mounts protected content while checking login', () => {
  authMock.mockReturnValue({ ...authMock(), loading: true });
  render();
  expect(container.textContent).toContain('正在确认登录状态');
  expect(container.textContent).not.toContain('受保护的对话空间');
});

test('renders protected content for an authenticated account', () => {
  authMock.mockReturnValue({ ...authMock(), user: { id: 'alice-id', username: 'alice' } });
  render();
  expect(container.textContent).toContain('受保护的对话空间');
});

test('offers a retry when authentication cannot be checked', () => {
  authMock.mockReturnValue({ ...authMock(), error: '账号服务暂时不可用' });
  render();
  expect(container.textContent).toContain('账号服务暂时不可用');
  expect(container.querySelector('button')?.textContent).toBe('重新连接');
  expect(container.textContent).not.toContain('登录页');
});
