/* Local text-console behavior only: every HTTP response is mocked. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const { createSSEParser, consumeSSE, createTextConsole } = require('./text.js');

const encoder = new TextEncoder();
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const flush = async () => {
  for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve));
};
const settle = async (promise) => {
  try {
    await promise;
  } catch (_) {
    // Either public rejection or an internally handled UI error is acceptable;
    // the assertions below verify the actual recovery visible to the user.
  }
};
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};

function streamFixture() {
  let controller;
  let cancelled = 0;
  const body = new ReadableStream({
    start(value) {
      controller = value;
    },
    cancel() {
      cancelled += 1;
    },
  });
  return {
    response: new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }),
    push(text) {
      controller.enqueue(encoder.encode(text));
    },
    close() {
      controller.close();
    },
    fail(error = new Error('mock transport disconnected')) {
      controller.error(error);
    },
    get cancelled() {
      return cancelled;
    },
  };
}

function completedResponse(round = 'round-current', text = '模型回复') {
  const stream = streamFixture();
  stream.push(frame('turn', { round_id: round }));
  stream.push(frame('delta', { round_id: round, text }));
  stream.push(frame('terminal', { round_id: round, state: 'generation_completed' }));
  stream.close();
  return stream.response;
}

async function consoleFixture(t, overrides = {}) {
  const html = fs.readFileSync(path.join(__dirname, 'text.html'), 'utf8');
  const dom = new JSDOM(html.replaceAll('__M0_CLIENT_KEY__', 'mock-client-key').replaceAll('__M1_CLIENT_KEY__', 'mock-client-key'), {
    url: 'http://127.0.0.1:3101/m1/text',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const w = dom.window;
  w.TextEncoder = TextEncoder;
  w.TextDecoder = TextDecoder;
  const intervals = new Map();
  let timerSerial = 0;
  w.setInterval = (fn, delay) => {
    const id = ++timerSerial;
    intervals.set(id, { fn, delay });
    return id;
  };
  w.clearInterval = (id) => intervals.delete(id);
  const requests = [];
  let sessionSerial = 0;
  const json = (data, status = 200) => new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
  w.fetch = async (url, options = {}) => {
    const record = { url: String(url), options, body: options.body ? JSON.parse(options.body) : undefined };
    requests.push(record);
    if (record.url === '/healthz') return json(overrides.health || { transports: ['text'] });
    if (record.url === '/api/m1/models') {
      return json(overrides.inventory || { mode: 'agent_loop', default_profile: 'deepseek_v4_pro', profiles: [
        { id: 'deepseek_v4_pro', name: 'Pro', model: 'deepseek-v4-pro', configured: true },
        { id: 'deepseek_v4_flash', name: 'Flash', model: 'deepseek-v4-flash', configured: true },
      ] });
    }
    if (record.url === '/api/m1/text/sessions' && options.method === 'POST') {
      sessionSerial += 1;
      return json({ session_id: `mock-session-${sessionSerial}`, profile_id: record.body.profile_id,
        transport: 'text', mode: 'agent_loop', expires_at: Math.floor(Date.now() / 1000) + 600 });
    }
    if (record.url.endsWith('/turns')) {
      return overrides.turns ? overrides.turns(record) : completedResponse(record.body.round_id);
    }
    if (record.url.endsWith('/interrupt')) {
      return overrides.interrupt ? overrides.interrupt(record) : json({ interrupted: true, round_id: record.body.round_id });
    }
    if (record.url.endsWith('/stop')) {
      return overrides.stop ? overrides.stop(record) : json({ stopped: true });
    }
    if (record.url.endsWith('/heartbeat')) return json({ alive: true });
    if (/\/api\/m1\/text\/sessions\/mock-session-\d+$/.test(record.url)) {
      return json({ alive: true, state: 'active', notes_version: 7 });
    }
    throw new Error('unexpected mock request');
  };
  const app = createTextConsole(w);
  t.after(() => {
    app.destroy();
    w.close();
  });
  await app.init();
  const element = (id) => w.document.getElementById(id);
  const start = async () => {
    element('start-session').click();
    await flush();
    assert(app.snapshot().session_id, 'a session should exist after successful creation');
  };
  const input = (text) => {
    element('input').value = text;
    element('input').dispatchEvent(new w.Event('input', { bubbles: true }));
  };
  return { app, w, element, requests, intervals, start, input, json };
}

test('SSE parser joins multiline data and handles CRLF split between chunks and bare CR', () => {
  const events = [];
  const parser = createSSEParser((event) => events.push(event));
  const wire = ': ignored comment\r\nevent: delta\r\ndata: {"round_id":"r",\r\ndata: "text":"中文🙂"}\r\n\r\n' +
    'event: terminal\rdata: {"round_id":"r","state":"generation_completed"}\r\r';
  for (const character of wire) parser.feed(character);
  parser.end();
  assert.deepEqual(events, [
    { event: 'delta', data: '{"round_id":"r",\n"text":"中文🙂"}' },
    { event: 'terminal', data: '{"round_id":"r","state":"generation_completed"}' },
  ]);
});

test('SSE parser flushes a final data event at EOF without manufacturing empty events', () => {
  const events = [];
  const parser = createSSEParser((event) => events.push(event));
  parser.feed('event: delta\ndata: {"text":"尾部"}');
  parser.end();
  assert.deepEqual(events, [{ event: 'delta', data: '{"text":"尾部"}' }]);
});

test('SSE reader preserves Chinese and emoji when every UTF-8 byte is a separate chunk', async () => {
  const wire = frame('delta', { round_id: 'r', text: '中文🙂回声' }).replaceAll('\n', '\r\n');
  const bytes = encoder.encode(wire);
  const events = [];
  const body = new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      controller.close();
    },
  });
  await consumeSSE(new Response(body), (event) => events.push(event), new AbortController().signal);
  assert.equal(events.length, 1);
  assert.equal(JSON.parse(events[0].data).text, '中文🙂回声');
  assert.equal(body.locked, false, 'reader lock should be released at EOF');
});

test('SSE reader cancels immediately when terminal callback returns false', async () => {
  const stream = streamFixture();
  stream.push(frame('terminal', { round_id: 'r', state: 'generation_completed' }) + frame('delta', { round_id: 'r', text: 'LATE' }));
  const events = [];
  await consumeSSE(stream.response, (event) => {
    events.push(event);
    return false;
  }, new AbortController().signal);
  assert.equal(events.length, 1, 'events after a terminal must not escape the reader');
  assert.equal(stream.cancelled, 1);
  assert.equal(stream.response.body.locked, false);
});

test('SSE reader abort cancels a pending read and releases its lock', { timeout: 2000 }, async () => {
  const stream = streamFixture();
  const controller = new AbortController();
  const work = consumeSSE(stream.response, () => assert.fail('no event expected'), controller.signal);
  await flush();
  controller.abort();
  await settle(work);
  assert.equal(stream.cancelled, 1);
  assert.equal(stream.response.body.locked, false);
});

test('text console respects the configured default and locks the chosen profile for the session', async (t) => {
  const f = await consoleFixture(t);
  assert.equal(f.element('model-profile').value, 'deepseek_v4_pro');
  assert.equal(f.element('model-profile').disabled, false);
  f.element('model-profile').value = 'deepseek_v4_flash';
  await f.start();
  assert.equal(f.app.snapshot().profile_id, 'deepseek_v4_flash');
  assert.equal(f.element('model-profile').disabled, true);
  const creation = f.requests.find((item) => item.url === '/api/m1/text/sessions');
  assert.deepEqual(creation.body, { profile_id: 'deepseek_v4_flash' });
  const heartbeat = [...f.intervals.values()].find((item) => item.delay === 20000);
  assert(heartbeat, 'connected sessions should renew their heartbeat every 20 seconds');
  await heartbeat.fn();
  assert(f.requests.some((item) => item.url.endsWith('/heartbeat')));
  await f.app.closeSession();
  assert.equal(f.element('model-profile').disabled, false);
});

test('third-party configured defaults win, while unavailable defaults fall back to Flash then the first usable profile', async (t) => {
  const third = { id: 'third_party_primary', name: 'Third-party model', configured: true };
  const flash = { id: 'deepseek_v4_flash', name: 'Flash', configured: true };
  const disabled = { id: 'disabled_default', name: 'Unavailable', configured: false };
  const cases = [
    { inventory: { default_profile: third.id, profiles: [flash, third] }, expected: third.id },
    { inventory: { default_profile: disabled.id, profiles: [disabled, third, flash] }, expected: flash.id },
    { inventory: { profiles: [third, flash] }, expected: flash.id },
    { inventory: { default_profile: 'missing_profile', profiles: [disabled, third] }, expected: third.id },
  ];
  for (const scenario of cases) {
    const f = await consoleFixture(t, { inventory: { mode: 'agent_loop', ...scenario.inventory } });
    assert.equal(f.element('model-profile').value, scenario.expected);
    await f.start();
    assert.equal(f.app.snapshot().profile_id, scenario.expected);
  }
});

test('text-only server hides the unavailable RTC link and explains that voice validation is not running', async (t) => {
  const f = await consoleFixture(t);
  await flush();
  assert.equal(f.element('rtc-link').hidden, true);
  assert.equal(f.element('rtc-unavailable').hidden, false);
  assert.match(f.element('rtc-unavailable').textContent, /语音验证未启动/);
});

test('legacy health and an explicit RTC transport both expose the voice validation link', async (t) => {
  for (const health of [{}, { transports: ['text', 'rtc'] }]) {
    const f = await consoleFixture(t, { health });
    await flush();
    assert.equal(f.element('rtc-link').hidden, false);
    assert.equal(f.element('rtc-unavailable').hidden, true);
    assert.equal(f.element('rtc-link').getAttribute('href'), '/m0');
  }
});

test('text console renders user and model payloads as text instead of executable HTML', async (t) => {
  const assistant = '<img src=x onerror="window.__textXss=1"><script>window.__textXss=2</script>';
  const f = await consoleFixture(t, { turns: (record) => completedResponse(record.body.round_id, assistant) });
  await f.start();
  const user = '<svg onload="window.__textXss=3">输入</svg>';
  f.input(user);
  await f.app.send();
  assert.equal(f.w.document.querySelector('.message.user .message-body').textContent, user);
  assert.equal(f.w.document.querySelector('.message.assistant .message-body').textContent, assistant);
  assert.equal(f.element('messages').querySelectorAll('img,script,svg').length, 0);
  assert.equal(f.w.__textXss, undefined);
});

test('only the active round can append text or end a live response', async (t) => {
  const stream = streamFixture();
  const f = await consoleFixture(t, { turns: () => stream.response });
  await f.start();
  f.input('测试当前轮');
  const work = f.app.send();
  const round = f.app.snapshot().active_round;
  stream.push(frame('turn', { round_id: round }));
  stream.push(frame('delta', { round_id: 'round-other', text: 'WRONG ROUND' }));
  stream.push(frame('delta', { round_id: round, text: '正确轮次' }));
  stream.push(frame('terminal', { round_id: 'round-other', state: 'cancelled' }));
  await flush();
  assert.equal(f.app.snapshot().active_round, round);
  assert.equal(f.element('send').disabled, true);
  assert.equal(f.w.document.querySelector('.message.assistant .message-body').textContent, '正确轮次');
  stream.push(frame('terminal', { round_id: round, state: 'generation_completed' }));
  await work;
  assert.equal(f.app.snapshot().active_round, null);
  f.input('下一轮');
  assert.equal(f.element('send').disabled, false);
  assert.equal(f.element('notes-version').textContent, '7');
});

test('EOF before terminal reports a recoverable error and allows the next turn', async (t) => {
  let attempt = 0;
  const broken = streamFixture();
  const f = await consoleFixture(t, { turns: (record) => (++attempt === 1 ? broken.response : completedResponse(record.body.round_id, '恢复成功')) });
  await f.start();
  f.input('第一次');
  const first = f.app.send();
  const round = f.app.snapshot().active_round;
  broken.push(frame('turn', { round_id: round }));
  broken.push(frame('delta', { round_id: round, text: '只有部分' }));
  broken.close();
  await settle(first);
  assert(f.element('error').textContent.trim(), 'incomplete SSE must not look like a successful reply');
  assert.equal(f.element('input').disabled, false);
  f.input('重试');
  assert.equal(f.element('send').disabled, false);
  await f.app.send();
  assert.equal(f.w.document.querySelectorAll('.message.assistant .message-body')[1].textContent, '恢复成功');
  f.input('下一轮');
  assert.equal(f.element('send').disabled, false);
});

test('interrupt waits for backend acknowledgement and false does not claim cancellation', async (t) => {
  const stream = streamFixture();
  const acknowledgement = deferred();
  const f = await consoleFixture(t, { turns: () => stream.response, interrupt: () => acknowledgement.promise });
  await f.start();
  f.input('等待回复');
  const sending = f.app.send();
  const round = f.app.snapshot().active_round;
  stream.push(frame('turn', { round_id: round }));
  stream.push(frame('delta', { round_id: round, text: '仍在生成' }));
  await flush();
  const interrupting = f.app.interrupt();
  await flush();
  assert.equal(f.app.snapshot().active_round, round);
  assert.equal(f.element('send').disabled, true);
  assert.doesNotMatch(f.w.document.querySelector('.message.assistant .message-state').textContent, /已(?:取消|打断|停止)|cancelled/i);
  acknowledgement.resolve(f.json({ interrupted: false, round_id: round }));
  await settle(interrupting);
  assert.equal(f.app.snapshot().active_round, round);
  assert.equal(f.element('send').disabled, true);
  assert.equal(stream.cancelled, 0, 'negative acknowledgement must leave the current reader alive');
  stream.push(frame('terminal', { round_id: round, state: 'generation_completed' }));
  await sending;
  f.input('下一轮');
  assert.equal(f.element('send').disabled, false);
});

test('accepted interrupt still waits for the matching cancelled terminal before ending the turn', async (t) => {
  const stream = streamFixture();
  const f = await consoleFixture(t, { turns: () => stream.response });
  await f.start();
  f.input('确认取消终态');
  const sending = f.app.send();
  const round = f.app.snapshot().active_round;
  stream.push(frame('turn', { round_id: round }));
  await flush();
  await f.app.interrupt();
  assert.equal(f.app.snapshot().active_round, round);
  assert.equal(f.element('send').disabled, true);
  assert.equal(stream.cancelled, 0);
  assert.doesNotMatch(f.w.document.querySelector('.message.assistant .message-state').textContent, /已(?:取消|打断|停止)|cancelled/i);
  stream.push(frame('terminal', { round_id: round, state: 'cancelled' }));
  await sending;
  assert.equal(f.app.snapshot().active_round, null);
  assert.match(f.w.document.querySelector('.message.assistant .message-state').textContent, /已打断/);
  f.input('取消后的下一轮');
  assert.equal(f.element('send').disabled, false);
});

test('aborted old fetch cannot update a replacement session or unlock its in-flight turn', async (t) => {
  const oldResponse = deferred();
  const current = streamFixture();
  let turn = 0;
  const f = await consoleFixture(t, { turns: () => (++turn === 1 ? oldResponse.promise : current.response) });
  await f.start();
  f.input('旧请求');
  const oldWork = f.app.send();
  await flush();
  const oldRequest = f.requests.find((item) => item.url.endsWith('/turns'));
  await f.app.closeSession();
  assert.equal(oldRequest.options.signal.aborted, true);
  await f.start();
  f.input('新请求');
  const newWork = f.app.send();
  const round = f.app.snapshot().active_round;
  current.push(frame('turn', { round_id: round }));
  current.push(frame('delta', { round_id: round, text: '新会话内容' }));
  await flush();
  oldResponse.resolve(completedResponse(oldRequest.body.round_id, 'STALE CONTENT'));
  await settle(oldWork);
  await flush();
  assert.equal(f.app.snapshot().active_round, round);
  assert.equal(f.element('send').disabled, true, 'old request cleanup must not unlock the new turn');
  assert.doesNotMatch(f.element('messages').textContent, /STALE CONTENT/);
  current.push(frame('terminal', { round_id: round, state: 'generation_completed' }));
  await newWork;
  f.input('下一轮');
  assert.equal(f.element('send').disabled, false);
});

test('failed stop retains the cleanup target and permits a successful retry', async (t) => {
  let attempts = 0;
  const f = await consoleFixture(t, { stop: () => (++attempts === 1 ? Promise.reject(new Error('mock stop failure')) : new Response('{"stopped":true}', { headers: { 'Content-Type': 'application/json' } })) });
  await f.start();
  const session = f.app.snapshot().session_id;
  await settle(f.app.closeSession());
  assert.equal(f.app.snapshot().session_id, session);
  assert.equal(f.element('stop-session').disabled, false);
  assert.equal(f.element('model-profile').disabled, true);
  await f.app.closeSession();
  assert.equal(f.app.snapshot().session_id, null);
  assert.equal(f.element('model-profile').disabled, false);
});

test('pagehide isolates the active stream and sends a keepalive stop request', async (t) => {
  const stream = streamFixture();
  const f = await consoleFixture(t, { turns: () => stream.response });
  await f.start();
  f.input('离开页面');
  const sending = f.app.send();
  stream.push(frame('turn', { round_id: f.app.snapshot().active_round }));
  await flush();
  const turnRequest = f.requests.find((item) => item.url.endsWith('/turns'));
  f.w.dispatchEvent(new f.w.Event('pagehide'));
  await flush();
  const stop = f.requests.find((item) => item.url.endsWith('/stop') && item.options.keepalive === true);
  assert(stop, 'leaving the page should ask the server to clean up the exact session');
  assert.equal(turnRequest.options.signal.aborted, true);
  assert.equal(f.app.snapshot().active_round, null);
  assert.equal(f.app.snapshot().connected, false);
  await settle(sending);
});

test('UTF-8 byte limit rejects Chinese over 8192 bytes locally without losing input', async (t) => {
  const f = await consoleFixture(t);
  await f.start();
  const input = '中'.repeat(3000);
  assert(input.length < 8192 && encoder.encode(input).length > 8192);
  f.input(input);
  await f.app.send();
  assert.equal(f.requests.filter((item) => item.url.endsWith('/turns')).length, 0);
  assert.equal(f.element('input').value, input);
  assert.equal(f.element('messages').querySelectorAll('.message').length, 0);
  assert.equal(f.app.snapshot().active_round, null);
  assert(f.element('error').textContent.trim(), 'oversized UTF-8 input should explain why it was rejected');
});
