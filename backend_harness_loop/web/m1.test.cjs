/* M1 model-picker wiring only: no real browser, microphone, API key or RTC. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

test('M1 exposes separate model choices and sends the selected profile; missing credentials remain retryable', async () => {
  const html = fs
    .readFileSync(path.join(__dirname, 'm0.html'), 'utf8')
    .replace('__M0_CLIENT_KEY__', 'mock-client-key');
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:3101/m0', runScripts: 'outside-only' });
  const w = dom.window;
  const requests = [];
  Object.defineProperty(w, 'isSecureContext', { value: true });
  w.VERTC = { setLogConfig() {}, isSupported: async () => true };
  w.fetch = async (url, options) => {
    if (url === '/api/m1/models')
      return {
        ok: true,
        status: 200,
        text: async () =>
          JSON.stringify({
            mode: 'agent_loop',
            default_profile: 'flash',
            profiles: [
              { id: 'flash', model: 'flash-model', configured: true },
              { id: 'pro', model: 'pro-model', configured: false },
            ],
          }),
      };
    if (url === '/api/m0/sessions') {
      requests.push(JSON.parse(options.body));
      return {
        ok: false,
        status: 503,
        text: async () =>
          JSON.stringify({
            detail: { code: 'missing_model_api_key', message: 'PRIVATE PROVIDER ERROR' },
          }),
      };
    }
    throw new Error('unexpected mock request');
  };
  try {
    w.eval(fs.readFileSync(path.join(__dirname, 'measurements.js'), 'utf8'));
    w.eval(fs.readFileSync(path.join(__dirname, 'm0.js'), 'utf8'));
    const flush = async () => {
      for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve));
    };
    await flush();
    assert.equal(w.document.getElementById('model-panel').hidden, false);
    assert.equal(w.document.getElementById('model-profile').value, 'flash');
    assert.equal(w.document.getElementById('model-profile').options.length, 2);
    assert.equal(
      w.document.getElementById('runtime-label').textContent,
      'backend_harness_loop / M1'
    );
    w.document.getElementById('model-profile').value = 'pro';
    w.document.getElementById('join').click();
    await flush();
    assert.deepEqual(requests, [{ profile_id: 'pro' }]);
    assert.match(w.document.getElementById('error').textContent, /所选模型尚未配置密钥/);
    assert.doesNotMatch(w.document.body.textContent, /PRIVATE PROVIDER ERROR/);
    assert.equal(w.document.getElementById('join').disabled, false);
    assert.equal(w.document.getElementById('model-profile').disabled, false);
  } finally {
    w.close();
  }
});
