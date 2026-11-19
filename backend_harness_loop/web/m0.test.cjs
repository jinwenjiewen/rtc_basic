/* Wiring smoke only: SDK/fetch are mocked; measurement rules run the real module. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

test('UI records a three-round speech batch, ordered retryable proof, interrupt, and hidden-page failure', async () => {
  const html = fs
    .readFileSync(path.join(__dirname, 'm0.html'), 'utf8')
    .replace('__M0_CLIENT_KEY__', 'mock-key');
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:3101/m0',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const w = dom.window;
  let clock = 1000,
    serial = 0,
    micStarts = 0,
    stops = 0,
    leaves = 0,
    destroys = 0;
  let beginAttempts = 0;
  let failedFirstEnergy = false,
    failedFirstBegin = false;
  const handlers = {},
    intervals = new Map(),
    events = [],
    starts = new Map(),
    results = [];
  Object.defineProperty(w, 'isSecureContext', { value: true });
  Object.defineProperty(w.performance, 'now', { value: () => clock });
  Object.defineProperty(w, 'crypto', {
    value: { randomUUID: () => (++serial).toString(16).padStart(32, '0') },
  });
  w.TextEncoder = TextEncoder;
  w.TextDecoder = TextDecoder;
  w.setInterval = (fn, interval) => {
    const id = ++serial;
    intervals.set(id, { fn, interval });
    return id;
  };
  w.clearInterval = (id) => intervals.delete(id);
  const engine = {
    on(name, fn) {
      handlers[name] = fn;
    },
    enableAudioPropertiesReport() {},
    async joinRoom() {},
    async startAudioCapture() {
      micStarts += 1;
    },
    async stopAudioCapture() {},
    async leaveRoom() {
      leaves += 1;
    },
    async sendUserBinaryMessage() {},
    async play() {},
  };
  w.VERTC = {
    events: new Proxy({}, { get: (_, name) => name }),
    MediaType: { AUDIO: 1 },
    StreamIndex: { STREAM_INDEX_MAIN: 0 },
    RoomProfileType: { chat: 5 },
    setLogConfig() {},
    isSupported: async () => true,
    createEngine: () => engine,
    destroyEngine: () => {
      destroys += 1;
    },
  };
  w.fetch = async (url, options) => {
    assert.equal(options.headers['X-M0-Client-Key'], 'mock-key');
    const body = options.body && JSON.parse(options.body);
    let data = {},
      status = 200;
    if (url === '/api/m0/sessions')
      data = {
        session_id: 'm0_mock',
        app_id: 'mock',
        room_id: 'mock',
        user_id: 'user',
        agent_id: 'agent',
        token: 'mock',
        expires_at: Math.floor(Date.now() / 1000) + 600,
      };
    else if (url.endsWith('/start')) {
      handlers.onUserJoined({ userInfo: { userId: 'agent' } });
      data = { running: true };
    } else if (url.endsWith('/stop')) {
      stops += 1;
      data = { stopped: true };
    } else if (url.endsWith('/events')) {
      if (body.kind === 'mic_level_active' && !failedFirstEnergy) {
        failedFirstEnergy = true;
        status = 503;
      } else {
        events.push(body);
        data = { recorded: true };
      }
    } else if (url.endsWith('/measurement-starts')) {
      beginAttempts += 1;
      if (!failedFirstBegin) {
        failedFirstBegin = true;
        status = 503;
      } else {
        starts.set(body.sample_id, body);
        data = { recorded: true };
      }
    } else if (url.endsWith('/measurements')) {
      assert(starts.has(body.sample_id), 'terminal must follow a persisted begin');
      assert.equal(starts.get(body.sample_id).started_ms, body.started_ms);
      if (body.state === 'completed' && body.kind === 'speech_first_audio') {
        assert(events.some((e) => e.kind === 'mic_started' && e.elapsed_ms <= body.started_ms));
        assert(
          events.some(
            (e) =>
              e.kind === 'mic_level_active' &&
              body.started_ms <= e.elapsed_ms &&
              e.elapsed_ms <= body.trigger_ms
          ),
          'each sample needs its own already persisted energy proof'
        );
      }
      results.push(body);
      data = { recorded: true };
    } else
      data = {
        state: stops ? 'stopped' : 'running',
        server_evidence: { custom_llm_rounds: 0, matched_rounds: 0, rts_errors: {} },
      };
    return { ok: status === 200, status, text: async () => JSON.stringify(data) };
  };
  const flush = async () => {
    for (let i = 0; i < 4; i++) await new Promise((resolve) => setImmediate(resolve));
  };
  function at(time) {
    clock = 1000 + time;
  }
  function local(volume, time) {
    at(time);
    handlers.onLocalAudioPropertiesReport([
      { streamIndex: 0, audioPropertiesInfo: { linearVolume: volume } },
    ]);
  }
  function remote(volume, time) {
    at(time);
    handlers.onRemoteAudioPropertiesReport([
      {
        streamKey: { userId: 'agent', streamIndex: 0 },
        audioPropertiesInfo: { linearVolume: volume },
      },
    ]);
  }
  function tick(time) {
    at(time);
    for (const item of intervals.values()) if (item.interval === 100) item.fn();
  }
  function conv(id, time, stage = 2) {
    at(time);
    const value = new TextEncoder().encode(JSON.stringify({ RoundID: id, Stage: { Code: stage } }));
    const buffer = new w.ArrayBuffer(value.length + 8),
      bytes = new Uint8Array(buffer);
    bytes.set(new TextEncoder().encode('conv'));
    new DataView(buffer).setUint32(4, value.length);
    bytes.set(value, 8);
    handlers.onRoomBinaryMessageReceived({ userId: 'agent', message: buffer });
  }
  try {
    w.eval(fs.readFileSync(path.join(__dirname, 'measurements.js'), 'utf8'));
    w.eval(fs.readFileSync(path.join(__dirname, 'm0.js'), 'utf8'));
    w.document.getElementById('join').click();
    await flush();
    assert.equal(micStarts, 0);
    w.document.getElementById('mic').click();
    await flush();
    assert.equal(micStarts, 1);
    for (let time = 100; time <= 1200; time += 100) {
      local(0, time);
      remote(0, time);
      tick(time);
    }
    assert.equal(w.document.getElementById('speech-sample').disabled, false);
    conv('previous', 1250);
    w.document.getElementById('speech-sample').click();
    await flush();
    tick(1250);
    assert.equal(beginAttempts, 0, 'pending callback must not register a timed sample');
    assert(w.document.getElementById('sample-state').textContent.includes('Stage4/5'));
    remote(80, 1300);
    remote(80, 1400);
    tick(1400);
    assert.equal(beginAttempts, 0, 'old reply during the waiting phase is not a sample failure');
    conv('previous', 1500, 5);
    for (let time = 1600; time <= 2700; time += 100) {
      local(0, time);
      remote(0, time);
      tick(time);
    }
    await flush();
    assert.equal(
      beginAttempts,
      1,
      'sample begins only after terminal callback and a full second quiet'
    );
    for (let round = 0; round < 3; round++) {
      const base = 2700 + round * 1900;
      local(80, base + 100);
      local(0, base + 200);
      conv(round, base + 250);
      remote(80, base + 400);
      local(0, base + 450);
      remote(80, base + 500);
      local(0, base + 600);
      local(0, base + 700);
      await flush();
      if (round < 2) {
        conv(round, base + 750, 5);
        for (let time = base + 800; time <= base + 1900; time += 100) {
          local(0, time);
          remote(0, time);
          tick(time);
        }
        await flush();
      }
    }
    assert.equal(results.length, 3);
    assert.deepEqual(
      results.map((result) => result.round_id),
      ['0', '1', '2']
    );
    assert(
      results.every(
        (result) => result.state === 'completed' && result.observed_ms - result.trigger_ms === 300
      )
    );
    assert.equal(starts.size, 3);
    assert.equal(events.filter((event) => event.kind === 'mic_level_active').length, 3);
    assert.equal(
      w.document.getElementById('speech-sample').disabled,
      false,
      'the button can queue a waiting batch while remote is still speaking'
    );
    at(7300);
    w.document.getElementById('interrupt').click();
    await flush();
    conv(2, 7350, 4);
    for (let time = 7400; time <= 8400; time += 100) {
      local(0, time);
      remote(0, time);
      tick(time);
    }
    await flush();
    assert.equal(results[3].kind, 'interrupt_silence');
    assert.equal(results[3].observed_ms, 7400);
    assert.equal(results[3].trigger_ms, 7300);
    assert.equal(
      w.document.getElementById('silence-estimate').textContent,
      '100 ms（仅 SDK 估计）'
    );
    w.document.getElementById('speech-sample').click();
    await flush();
    tick(8500);
    await flush();
    Object.defineProperty(w.document, 'hidden', { value: true, configurable: true });
    w.document.dispatchEvent(new w.Event('visibilitychange'));
    await flush();
    assert.equal(results[4].reason, 'page_hidden');
    assert.equal(starts.size, 5);
    assert(
      events
        .filter((event) => event.kind === 'remote_level_silent')
        .every((event) => !('duration_ms' in event))
    );
    assert.equal(
      events.some((event) => event.kind.endsWith('_confirmed')),
      false
    );
    w.document.getElementById('stop').click();
    await flush();
    assert.equal(stops, 1);
    assert.equal(leaves, 1);
    assert.equal(destroys, 1);
  } finally {
    w.close();
  }
});
