import { TextDecoder, TextEncoder } from 'util';
import { afterEach, expect, jest, test } from '@jest/globals';
import type { Mock } from 'jest-mock';
import { authenticatedFetch } from '@/auth/api';
import { streamText } from './api';
import { requestPostMethod } from '@/app/base';

jest.mock('@/auth/api', () => {
  const { jest: mockJest } = require('@jest/globals');
  return { authenticatedFetch: mockJest.fn() };
});
const fetchMock = authenticatedFetch as Mock<Promise<Response>, Parameters<typeof authenticatedFetch>>;
(globalThis as any).TextDecoder = TextDecoder;

function response(text: string, split: number = 1) {
  const bytes = new TextEncoder().encode(text);
  let offset = 0;
  const cancel = jest.fn(async () => {});
  const releaseLock = jest.fn();
  fetchMock.mockResolvedValue({ body: { getReader: () => ({
    read: async () => {
      const start = offset;
      offset += split;
      return start >= bytes.length ? { done: true } : { done: false, value: bytes.slice(start, offset) };
    }, cancel, releaseLock,
  }) } } as unknown as Response);
  return { cancel, releaseLock };
}

afterEach(() => { jest.clearAllMocks(); });

test('fragmented SSE preserves Chinese UTF-8 and emits only complete frames', async () => {
  const reader = response('event: delta\ndata: {"round_id":"r1","text":"课程介绍"}\n\nevent: terminal\ndata: {"round_id":"r1","state":"generation_completed"}\n\n');
  const receive = jest.fn();
  await streamText('owned-session', 'r1', '你好', new AbortController().signal, receive);
  expect(receive).toHaveBeenNthCalledWith(1, 'delta', { round_id: 'r1', text: '课程介绍' });
  expect(receive).toHaveBeenNthCalledWith(2, 'terminal', { round_id: 'r1', state: 'generation_completed' });
  expect(reader.cancel).toHaveBeenCalledTimes(1);
  expect(reader.releaseLock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0][0]).toBe('/api/agent/text/sessions/owned-session/turns');
});

test('truncated or foreign-round streams fail and release the reader', async () => {
  for (const [frame, message] of [
    ['event: delta\ndata: {"round_id":"r1","text":"部分"}\n\n', '连接已中断'],
    ['event: delta\ndata: {"round_id":"other","text":"外部"}\n\n', '回复轮次不匹配'],
  ]) {
    const reader = response(frame, 6);
    await expect(streamText('owned-session', 'r1', '你好', new AbortController().signal, () => {})).rejects.toThrow(message);
    expect(reader.cancel).toHaveBeenCalledTimes(1);
    expect(reader.releaseLock).toHaveBeenCalledTimes(1);
  }
});

test('formal scene request without arguments still sends the JSON object contract', async () => {
  fetchMock.mockResolvedValue({ ok: true } as Response);
  await requestPostMethod({ action: 'getScenes', apiPath: '/getScenes' })(undefined);
  expect(fetchMock).toHaveBeenCalledWith('/getScenes?Action=getScenes', expect.objectContaining({ body: '{}' }));
});
