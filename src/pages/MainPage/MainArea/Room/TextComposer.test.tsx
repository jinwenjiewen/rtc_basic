import { act } from 'react';
import { Simulate } from 'react-dom/test-utils';
import { jest, test, expect, beforeEach, afterEach } from '@jest/globals';
import type { Mock } from 'jest-mock';
import { createRoot, Root } from 'react-dom/client';
import { configureStore } from '@reduxjs/toolkit';
import { Provider } from 'react-redux';
import roomReducer, {
  localJoinRoom,
  updateAIGCState,
  updateSceneConfig,
  updateScene,
  setHistoryMsg,
  updateShowSubtitle,
} from '@/store/slices/room';
import RtcClient from '@/lib/RtcClient';
import { COMMAND, INTERRUPT_PRIORITY } from '@/utils/handler';
import TextComposer from './TextComposer';

jest.mock('@volcengine/rtc', () => ({ NetworkQuality: { UNKNOWN: 0 } }));
jest.mock('@/lib/useCommon', () => ({ useScene: () => ({ botName: 'ChatBot01' }) }));
jest.mock('@/lib/RtcClient', () => {
  const { jest: mockJest } = require('@jest/globals');
  return {
    __esModule: true,
    default: { commandAgent: mockJest.fn(), getAgentEnabled: mockJest.fn() },
  };
});

const commandAgent = RtcClient.commandAgent as Mock<Promise<unknown>, [any]>;
const getAgentEnabled = RtcClient.getAgentEnabled as Mock<boolean, []>;
let container: HTMLDivElement;
let root: Root;
let store: ReturnType<typeof makeStore>;

function makeStore() {
  return configureStore({ reducer: { room: roomReducer } });
}

function input() {
  return container.querySelector('textarea')!;
}

function button() {
  return container.querySelector('button')!;
}

function change(text: string) {
  act(() => {
    Simulate.change(input(), { target: { value: text } } as any);
  });
}

async function submit() {
  await act(async () => {
    Simulate.submit(container.querySelector('form')!);
  });
}

beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  jest.clearAllMocks();
  commandAgent.mockResolvedValue(undefined);
  getAgentEnabled.mockReturnValue(true);
  store = makeStore();
  store.dispatch(updateSceneConfig({ Custom: { botName: 'ChatBot01', isAvatarScene: false } }));
  store.dispatch(updateScene('Custom'));
  store.dispatch(localJoinRoom({ roomId: 'room', user: { userId: 'user' } }));
  store.dispatch(updateAIGCState({ isAIGCEnable: true }));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(
      <Provider store={store}>
        <TextComposer />
      </Provider>
    );
  });
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  container.remove();
});

test('uses the existing RTC conversation and places streamed replies after the text question', async () => {
  act(() => {
    store.dispatch(updateShowSubtitle({ isShowSubtitle: false }));
    store.dispatch(
      setHistoryMsg({ user: 'ChatBot01', text: '上一段回答', paragraph: false, definite: false })
    );
  });
  change('  课程有哪些\n请介绍一下  ');
  await submit();
  expect(commandAgent).toHaveBeenCalledWith({
    agentName: 'ChatBot01',
    command: COMMAND.EXTERNAL_TEXT_TO_LLM,
    interruptMode: INTERRUPT_PRIORITY.HIGH,
    message: '课程有哪些\n请介绍一下。',
  });
  expect(input().value).toBe('');
  expect(store.getState().room.msgHistory.map((m) => m.value)).toEqual([
    '上一段回答',
    '课程有哪些\n请介绍一下',
  ]);
  act(() => {
    store.dispatch(
      setHistoryMsg({ user: 'ChatBot01', text: '课程介绍', paragraph: true, definite: true })
    );
  });
  expect(store.getState().room.msgHistory.map((m) => m.value)).toEqual([
    '上一段回答',
    '课程有哪些\n请介绍一下',
    '课程介绍',
  ]);
  expect(store.getState().room.isShowSubtitle).toBe(true);
});

test('keeps existing punctuation and limits input to leave room for protocol punctuation', async () => {
  expect(input().maxLength).toBe(199);
  change('课程价格是多少？');
  await submit();
  expect(commandAgent.mock.calls[0][0].message).toBe('课程价格是多少？');
});

test('rejects empty messages and messages while disconnected', async () => {
  expect(button().disabled).toBe(true);
  change('   ');
  await submit();
  expect(commandAgent).not.toHaveBeenCalled();
  act(() => {
    store.dispatch(updateAIGCState({ isAIGCEnable: false }));
  });
  change('你好');
  await submit();
  expect(commandAgent).not.toHaveBeenCalled();
  expect(input().disabled).toBe(true);
});

test('keeps the draft and rolls back only the failed message', async () => {
  act(() => {
    store.dispatch(
      setHistoryMsg({ user: 'ChatBot01', text: '欢迎', paragraph: true, definite: true })
    );
  });
  commandAgent.mockRejectedValueOnce(new Error('connection failed'));
  change('想了解课程');
  await submit();
  expect(input().value).toBe('想了解课程');
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('发送失败');
  expect(store.getState().room.msgHistory.map((m) => m.value)).toEqual(['欢迎']);
  expect(button().disabled).toBe(false);
  await submit();
  expect(commandAgent).toHaveBeenCalledTimes(2);
  expect(input().value).toBe('');
});

test('ignores Enter during Chinese composition and Shift+Enter; sends with plain Enter', async () => {
  change('课程');
  act(() => {
    Simulate.compositionStart(input());
    Simulate.keyDown(input(), { key: 'Enter' });
    Simulate.compositionEnd(input());
    Simulate.keyDown(input(), { key: 'Enter', shiftKey: true });
  });
  expect(commandAgent).not.toHaveBeenCalled();
  await act(async () => {
    Simulate.keyDown(input(), { key: 'Enter' });
  });
  expect(commandAgent).toHaveBeenCalledTimes(1);
});

test('prevents repeated sends until the SDK confirms delivery', async () => {
  let resolve: () => void = () => {};
  commandAgent.mockReturnValue(
    new Promise<void>((done) => {
      resolve = done;
    })
  );
  change('你好');
  act(() => {
    Simulate.submit(container.querySelector('form')!);
    Simulate.submit(container.querySelector('form')!);
  });
  expect(commandAgent).toHaveBeenCalledTimes(1);
  expect(input().readOnly).toBe(true);
  expect(button().disabled).toBe(true);
  await act(async () => {
    resolve();
  });
  expect(input().value).toBe('');
});

test('does not send to an inactive agent even if the room was joined', async () => {
  getAgentEnabled.mockReturnValue(false);
  change('你好');
  await submit();
  expect(commandAgent).not.toHaveBeenCalled();
  expect(input().value).toBe('你好');
  expect(container.querySelector('[role="alert"]')?.textContent).toContain('尚未连接');
});
