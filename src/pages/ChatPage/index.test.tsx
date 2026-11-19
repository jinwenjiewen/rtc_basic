import { act } from 'react';
import { Simulate } from 'react-dom/test-utils';
import { createRoot, Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import type { Mock } from 'jest-mock';
import { agentRequest, createTextSession, sessionAction, streamText } from '@/agent/api';
import ChatPage from './index';

jest.mock('@/components/Header', () => () => <header>产品导航</header>);
jest.mock('uuid', () => ({ v4: () => 'fixture-round' }));
jest.mock('@/agent/api', () => {
  const { jest: mockJest } = require('@jest/globals');
  return { agentRequest: mockJest.fn(), createTextSession: mockJest.fn(), sessionAction: mockJest.fn(), streamText: mockJest.fn() };
});
const menu = agentRequest as Mock<Promise<any>, any[]>;
const create = createTextSession as Mock<Promise<any>, any[]>;
const action = sessionAction as Mock<Promise<any>, any[]>;
const stream = streamText as Mock<Promise<void>, Parameters<typeof streamText>>;
let container: HTMLDivElement;
let root: Root;
let unmounted = false;

beforeEach(async () => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  jest.clearAllMocks();
  menu.mockResolvedValue({ default_profile: 'flash', profiles: [{ id: 'flash', name: 'Flash', configured: true }, { id: 'pro', name: 'Pro', configured: true }] });
  create.mockResolvedValue({ session_id: 'owned-session', profile_id: 'flash', expires_at: 9999999999 });
  action.mockResolvedValue({ stopped: true });
  stream.mockImplementation(async (_id, round, _text, _signal, receive) => {
    receive('delta', { round_id: round, text: '课程介绍回复。' });
    receive('terminal', { round_id: round, state: 'generation_completed' });
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  unmounted = false;
  await act(async () => { root.render(<MemoryRouter><ChatPage /></MemoryRouter>); });
});

afterEach(() => {
  if (root && !unmounted) act(() => root.unmount());
  container?.remove();
});

async function send() {
  act(() => { Simulate.change(container.querySelector('textarea')!, { target: { value: '介绍课程' } } as any); });
  await act(async () => { Simulate.submit(container.querySelector('form')!); });
}

test('formal text UI streams reply, pins model and ends the exact lease on new conversation', async () => {
  await send();
  expect(create).toHaveBeenCalledWith('flash');
  expect(container.querySelector('[role="log"]')?.textContent).toContain('课程介绍回复。');
  expect(container.textContent).not.toContain('本轮回复未完成');
  expect(container.querySelector('select')?.disabled).toBe(true);
  const newChat = Array.from(container.querySelectorAll('button')).find((b) => b.textContent === '新对话')!;
  await act(async () => { Simulate.click(newChat); });
  expect(action).toHaveBeenCalledWith('text', 'owned-session', 'stop');
  expect(container.querySelector('select')?.disabled).toBe(false);
  expect(container.querySelector('[role="log"]')?.textContent).not.toContain('课程介绍回复。');
});

test('late session creation after route exit is stopped and cannot render a reply', async () => {
  let resolveCreate: (value: any) => void = () => {};
  create.mockReturnValue(new Promise((resolve) => { resolveCreate = resolve; }));
  await send();
  act(() => { root.unmount(); });
  unmounted = true;
  await act(async () => {
    resolveCreate({ session_id: 'late-owned-session', profile_id: 'flash', expires_at: 9999999999 });
  });
  expect(action).toHaveBeenCalledWith('text', 'late-owned-session', 'stop');
  expect(stream).not.toHaveBeenCalled();
});
