import { act } from 'react';
import { createRoot, Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, beforeEach, expect, jest, test } from '@jest/globals';
import type { Mock } from 'jest-mock';
import VERTC from '@volcengine/rtc';
import { Modal } from '@arco-design/web-react';
import Apis from '@/app/index';
import RtcClient from '@/lib/RtcClient';
import roomReducer, { updateScene, updateSceneConfig } from '@/store/slices/room';
import deviceReducer from '@/store/slices/device';
import { useJoin } from './useCommon';

jest.mock('@volcengine/rtc', () => {
  const { jest: mockJest } = require('@jest/globals');
  return {
    __esModule: true,
    default: { isSupported: mockJest.fn() },
    MediaType: { AUDIO: 0, VIDEO: 1 },
    NetworkQuality: { UNKNOWN: 0 },
  };
});
jest.mock('@arco-design/web-react', () => {
  const { jest: mockJest } = require('@jest/globals');
  return { Modal: { error: mockJest.fn() } };
});
jest.mock('@/app/index', () => {
  const { jest: mockJest } = require('@jest/globals');
  return { __esModule: true, default: { Basic: { getScenes: mockJest.fn() } } };
});
jest.mock('@/lib/listenerHooks', () => ({ __esModule: true, default: () => ({}) }));
jest.mock('@/lib/RtcClient', () => {
  const { jest: mockJest } = require('@jest/globals');
  return {
    __esModule: true,
    default: {
      basicInfo: {},
      createEngine: mockJest.fn(),
      addEventListeners: mockJest.fn(),
      joinRoom: mockJest.fn(),
      getDevices: mockJest.fn(),
      startAgent: mockJest.fn(),
      stopAgent: mockJest.fn(),
      release: mockJest.fn(),
      getAgentEnabled: mockJest.fn(),
    },
  };
});

const mockMethod = (method: unknown) => method as Mock<Promise<any>, any[]>;
let root: Root;
let container: HTMLDivElement;
let store: ReturnType<typeof makeStore>;
let join: () => Promise<boolean | void>;

function makeStore() {
  return configureStore({ reducer: { room: roomReducer, device: deviceReducer } });
}

function Probe() {
  const [joining, requestJoin] = useJoin();
  join = requestJoin;
  return <span>{joining ? 'connecting' : 'idle'}</span>;
}

beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  jest.clearAllMocks();
  mockMethod(VERTC.isSupported).mockResolvedValue(true);
  mockMethod(Apis.Basic.getScenes).mockResolvedValue({
    scenes: [
      {
        scene: { id: 'Custom' },
        rtc: { AppId: 'app', RoomId: 'ChatRoom01', UserId: 'Huoshan01', Token: 'fresh-token' },
      },
    ],
  });
  for (const method of [
    RtcClient.createEngine,
    RtcClient.joinRoom,
    RtcClient.startAgent,
    RtcClient.release,
  ]) {
    mockMethod(method).mockReset().mockResolvedValue(undefined);
  }
  mockMethod(RtcClient.getDevices).mockResolvedValue({
    audioInputs: [],
    audioOutputs: [],
    videoInputs: [],
  });
  (RtcClient.getAgentEnabled as Mock<boolean, []>).mockReturnValue(false);
  store = makeStore();
  store.dispatch(updateScene('Custom'));
  store.dispatch(updateSceneConfig({ Custom: { id: 'Custom', botName: 'ChatBot01' } }));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() =>
    root.render(
      <Provider store={store}>
        <Probe />
      </Provider>
    )
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test('failed RTC token validation releases resources, resets loading and allows retry', async () => {
  mockMethod(RtcClient.joinRoom).mockRejectedValueOnce(new Error('token_error'));
  await act(async () => {
    expect(await join()).toBe(false);
  });
  expect(container.textContent).toBe('idle');
  expect(RtcClient.release).toHaveBeenCalledTimes(1);
  expect(RtcClient.startAgent).not.toHaveBeenCalled();
  expect(store.getState().room.isJoined).toBe(false);
  expect(Modal.error).toHaveBeenCalledWith(
    expect.objectContaining({ content: expect.stringContaining('进房凭证') })
  );
  await act(async () => {
    expect(await join()).toBe(true);
  });
  expect(Apis.Basic.getScenes).toHaveBeenCalledTimes(2);
  expect(RtcClient.basicInfo.token).toBe('fresh-token');
  expect(store.getState().room.isJoined).toBe(true);
});

test('voice-chat API failure is caught and does not leave a false joined state', async () => {
  mockMethod(RtcClient.startAgent).mockRejectedValueOnce(new Error('回调服务暂时不可用'));
  await act(async () => {
    expect(await join()).toBe(false);
  });
  expect(container.textContent).toBe('idle');
  expect(RtcClient.release).toHaveBeenCalledTimes(1);
  expect(store.getState().room.isJoined).toBe(false);
  expect(store.getState().room.isAIGCEnable).toBe(false);
  expect(Modal.error).toHaveBeenCalledWith(
    expect.objectContaining({ content: '回调服务暂时不可用' })
  );
});

test('rapid repeated starts share a single join attempt', async () => {
  await act(async () => {
    const first = join();
    const second = join();
    await Promise.all([first, second]);
  });
  expect(RtcClient.joinRoom).toHaveBeenCalledTimes(1);
  expect(RtcClient.startAgent).toHaveBeenCalledTimes(1);
});
