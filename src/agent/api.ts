import { authenticatedFetch } from '@/auth/api';

export interface AgentSession {
  session_id: string;
  profile_id: string;
  expires_at: number;
}

export interface RtcSession extends AgentSession {
  app_id: string;
  room_id: string;
  user_id: string;
  agent_id: string;
  token: string;
}

export async function agentRequest<T>(path: string, body?: object): Promise<T> {
  const response = await authenticatedFetch(`/api/agent/${path}`, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    keepalive: true,
  });
  return response.json();
}

export const createTextSession = (profileId: string) =>
  agentRequest<AgentSession>('text/sessions', { profile_id: profileId });
export const createRtcSession = (profileId: string) =>
  agentRequest<RtcSession>('rtc/sessions', { profile_id: profileId });
export const sessionAction = (transport: 'text' | 'rtc', id: string, action: string, body: object = {}) =>
  agentRequest<any>(`${transport}/sessions/${encodeURIComponent(id)}/${action}`, body);

export async function streamText(id: string, roundId: string, text: string,
  signal: AbortSignal, onEvent: (event: string, payload: any) => void) {
  const response = await authenticatedFetch(`/api/agent/text/sessions/${encodeURIComponent(id)}/turns`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ round_id: roundId, text }), signal,
  });
  const reader = response.body?.getReader();
  if (!reader) throw new Error('浏览器不支持流式回复。');
  const decoder = new TextDecoder();
  let buffer = '';
  let terminal = false;
  try {
    while (true) {
      // The reader is ordered: decode each chunk before requesting the next.
      // eslint-disable-next-line no-await-in-loop
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, '\n');
      if (buffer.length > 65_536) throw new Error('回复格式异常，请重新开始。');
      let boundary: number;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const lines = frame.split('\n');
        const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
        const data = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
        if (!event || !data) continue;
        const payload = JSON.parse(data);
        if (payload.round_id !== roundId) throw new Error('回复轮次不匹配。');
        terminal = terminal || event === 'terminal';
        onEvent(event, payload);
      }
      if (done) break;
    }
    if (!terminal) throw new Error('连接已中断，可重试或重新开始。');
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
