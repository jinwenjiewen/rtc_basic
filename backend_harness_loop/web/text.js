(function (root, factory) {
  'use strict';
  const exported = factory();
  if (typeof module === 'object' && module.exports) module.exports = exported;
  if (root && root.document) {
    const start = () => exported.createTextConsole(root).init();
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', start, { once: true });
    else start();
  }
})(typeof window === 'undefined' ? null : window, function () {
  'use strict';

  function createSSEParser(onEvent) {
    let line = '', event = 'message', data = [], pendingCR = false, stopped = false, size = 0;
    function dispatch() {
      if (data.length && onEvent({ event, data: data.join('\n') }) === false) stopped = true;
      event = 'message';
      data = [];
      size = 0;
    }
    function acceptLine(value) {
      if (!value) return dispatch();
      if (value[0] === ':') return;
      const colon = value.indexOf(':');
      const name = colon < 0 ? value : value.slice(0, colon);
      let body = colon < 0 ? '' : value.slice(colon + 1);
      if (body[0] === ' ') body = body.slice(1);
      if (name === 'event') event = body;
      if (name === 'data') {
        size += body.length;
        if (size > 65536) throw new Error('stream_event_limit');
        data.push(body);
      }
    }
    return {
      feed(text) {
        for (const char of text) {
          if (stopped) return;
          if (pendingCR) {
            pendingCR = false;
            if (char === '\n') continue;
          }
          if (char === '\r' || char === '\n') {
            acceptLine(line);
            line = '';
            pendingCR = char === '\r';
          } else {
            line += char;
            if (line.length > 65536) throw new Error('stream_line_limit');
          }
        }
      },
      end() {
        if (stopped) return;
        if (line) acceptLine(line);
        line = '';
        if (!stopped && data.length) dispatch();
      },
    };
  }

  async function consumeSSE(response, onEvent, signal) {
    if (!response.body || typeof response.body.getReader !== 'function') throw new Error('stream_unavailable');
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8', { fatal: true });
    let stopped = false;
    const parser = createSSEParser((event) => {
      if (onEvent(event) === false) {
        stopped = true;
        return false;
      }
      return true;
    });
    const cancel = () => { reader.cancel().catch(() => {}); };
    if (signal) signal.addEventListener('abort', cancel, { once: true });
    try {
      while (!stopped) {
        if (signal && signal.aborted) return;
        const next = await reader.read();
        if (signal && signal.aborted) return;
        if (next.done) {
          parser.feed(decoder.decode());
          parser.end();
          break;
        }
        parser.feed(decoder.decode(next.value, { stream: true }));
      }
    } finally {
      if (signal) signal.removeEventListener('abort', cancel);
      try { await reader.cancel(); } catch (_) { /* A disconnected reader is already closed. */ }
      reader.releaseLock();
    }
  }

  function createTextConsole(win) {
    const doc = win.document;
    const byId = (id) => doc.getElementById(id);
    const ui = {};
    for (const id of ['model-profile', 'start-session', 'stop-session', 'connection-state', 'expires-at',
      'notes-version', 'notice', 'error', 'input', 'send', 'interrupt', 'messages', 'composer', 'rtc-link', 'rtc-unavailable']) ui[id] = byId(id);
    const key = doc.querySelector('meta[name="m0-client-key"]')?.content || '';
    let current = null, creating = false, disposed = false, generation = 0, modelsReady = false, initialized = false;
    const terminalStates = new Set(['generation_completed', 'cancelled', 'failed']);
    const base = '/api/m1/text/sessions';
    const isCurrent = (ctx) => current === ctx && !ctx.closed && !disposed;
    const endpoint = (ctx, action = '') => `${base}/${encodeURIComponent(ctx.session_id)}${action ? '/' + action : ''}`;
    const headers = () => ({ 'X-M0-Client-Key': key, 'Content-Type': 'application/json' });
    const safeCode = (value) => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,100}$/.test(value) ? value : '';

    function errorText(code, status) {
      if (status === 401 || status === 403) return '页面访问已失效，请刷新页面后重试。';
      if (status === 404 || status === 410) return '会话已到期，请重新建立文字会话。';
      if (code === 'network_error') return '连接中断，请检查本地服务后重试。';
      if (code === 'request_timeout') return '请求超时，请重试。';
      return `请求未完成${code ? `（${code}）` : ''}，可以重试。`;
    }
    function showError(message = '') {
      ui.error.textContent = message;
      ui.error.hidden = !message;
    }
    function render() {
      const live = current && !current.closed;
      const blocked = live && (current.stopping || current.stopUnconfirmed);
      ui['start-session'].disabled = creating || !!live || !modelsReady || disposed;
      ui['model-profile'].disabled = creating || !!live || !modelsReady || disposed;
      ui['stop-session'].disabled = !live || current.stopping || disposed;
      ui.input.disabled = !live || !!blocked || disposed;
      ui.send.disabled = !live || !!blocked || !!current.active || !ui.input.value.trim() || disposed;
      ui.interrupt.disabled = !live || !!blocked || !current.active || current.active.interruptPending || disposed;
      if (creating) ui['connection-state'].textContent = '正在建立会话…';
      else if (live && current.stopping) ui['connection-state'].textContent = '正在结束会话…';
      else if (live && current.stopUnconfirmed) ui['connection-state'].textContent = '结束待确认';
      else if (live && current.active) ui['connection-state'].textContent = current.active.interruptPending ? '正在请求打断…' : '正在回复…';
      else if (live) ui['connection-state'].textContent = '会话已就绪';
    }
    async function jsonRequest(path, method = 'GET', body) {
      const controller = new win.AbortController();
      let timedOut = false;
      const timeout = win.setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
      try {
        const response = await win.fetch(path, { method, headers: headers(), credentials: 'same-origin', cache: 'no-store',
          ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal });
        let data;
        try { data = await response.json(); } catch (_) { throw { code: 'invalid_response', status: response.status }; }
        if (!response.ok) throw { code: safeCode(data?.detail?.code), status: response.status };
        return data;
      } catch (error) {
        if (error && typeof error.status === 'number') throw error;
        throw { code: timedOut ? 'request_timeout' : 'network_error', status: 0 };
      } finally { win.clearTimeout(timeout); }
    }
    function keepaliveStop(ctx) {
      try {
        Promise.resolve(win.fetch(endpoint(ctx, 'stop'), { method: 'POST', headers: headers(),
          credentials: 'same-origin', body: '{}', keepalive: true })).catch(() => {});
      } catch (_) { /* Server expiry also bounds abandoned sessions. */ }
    }
    function clearTimers(ctx) {
      win.clearInterval(ctx.heartbeatTimer);
      win.clearInterval(ctx.expiryTimer);
    }
    function detach(ctx, label) {
      ctx.closed = true;
      clearTimers(ctx);
      if (ctx.active) {
        ctx.active.row.state.textContent = label;
        ctx.active.controller.abort();
        ctx.active = null;
      }
      if (current === ctx) {
        current = null;
        ui['connection-state'].textContent = label;
        ui['expires-at'].textContent = '—';
        render();
      }
    }
    function expiryTick(ctx) {
      if (!isCurrent(ctx)) return;
      if (Date.now() / 1000 >= ctx.expires_at) {
        detach(ctx, '会话已到期');
        ui.notice.textContent = '会话已到期，请重新建立文字会话。';
        keepaliveStop(ctx);
      } else ui['expires-at'].textContent = new Date(ctx.expires_at * 1000).toLocaleTimeString();
    }
    async function syncSnapshot(ctx) {
      try {
        const data = await jsonRequest(endpoint(ctx));
        if (!isCurrent(ctx)) return;
        if (Number.isInteger(data.notes_version) && data.notes_version >= 0) {
          ctx.notes_version = Math.max(ctx.notes_version, data.notes_version);
          ui['notes-version'].textContent = String(ctx.notes_version);
        }
        if (Number.isFinite(data.expires_at)) ctx.expires_at = data.expires_at;
        if (data.alive === false || data.state === 'closed') {
          detach(ctx, '会话已结束');
          ui.notice.textContent = '当前会话已结束，可建立新会话。';
        } else expiryTick(ctx);
      } catch (error) {
        if (!isCurrent(ctx)) return;
        if (error.status === 404 || error.status === 410) detach(ctx, '会话已到期');
      }
    }
    async function heartbeat(ctx) {
      if (!isCurrent(ctx) || ctx.heartbeatPending || ctx.stopping || ctx.stopUnconfirmed) return;
      ctx.heartbeatPending = true;
      try {
        const data = await jsonRequest(endpoint(ctx, 'heartbeat'), 'POST', {});
        if (!isCurrent(ctx)) return;
        if (data.alive !== true) {
          detach(ctx, '会话已到期');
          return;
        }
        if (Number.isFinite(data.expires_at)) ctx.expires_at = data.expires_at;
        expiryTick(ctx);
      } catch (error) {
        if (!isCurrent(ctx)) return;
        if (error.status === 404 || error.status === 410) detach(ctx, '会话已到期');
        else showError('会话连接暂未确认；请检查本地服务，或结束后重新建立会话。');
      } finally { ctx.heartbeatPending = false; }
    }
    function appendMessage(role, text, label) {
      const article = doc.createElement('article');
      article.className = `message ${role}`;
      const author = doc.createElement('strong');
      author.textContent = role === 'user' ? '你' : 'Agent';
      const body = doc.createElement('p');
      body.className = 'message-body';
      body.textContent = text;
      const state = doc.createElement('small');
      state.className = 'message-state';
      state.textContent = label;
      article.append(author, body, state);
      ui.messages.append(article);
      ui.messages.scrollTop = ui.messages.scrollHeight;
      return { body, state };
    }
    async function startSession() {
      if (current || creating || !modelsReady || disposed) return;
      creating = true;
      const attempt = generation;
      showError();
      render();
      try {
        const data = await jsonRequest(base, 'POST', { profile_id: ui['model-profile'].value });
        if (typeof data.session_id !== 'string' || !data.session_id || data.transport !== 'text' || data.mode !== 'agent_loop'
          || typeof data.profile_id !== 'string' || !Number.isFinite(data.expires_at)) throw { code: 'invalid_session_response', status: 0 };
        if (disposed || attempt !== generation) { keepaliveStop(data); return; }
        const ctx = { ...data, notes_version: 0, active: null, roundCounter: 0, closed: false, stopping: false, stopUnconfirmed: false };
        current = ctx;
        ui.messages.textContent = '';
        ui['notes-version'].textContent = '0';
        ui['model-profile'].value = data.profile_id;
        ui.notice.textContent = '会话已建立，可以自由输入。';
        ctx.heartbeatTimer = win.setInterval(() => heartbeat(ctx), 20000);
        ctx.expiryTimer = win.setInterval(() => expiryTick(ctx), 1000);
        expiryTick(ctx);
        if (isCurrent(ctx)) ui.input.focus();
      } catch (error) {
        if (!disposed && attempt === generation) showError(errorText(safeCode(error.code), error.status));
      } finally {
        if (attempt === generation) { creating = false; render(); }
      }
    }
    async function send() {
      const ctx = current;
      const text = ui.input.value;
      if (!ctx || !isCurrent(ctx) || ctx.active || ctx.stopping || ctx.stopUnconfirmed || !text.trim()) return;
      if (new TextEncoder().encode(text).byteLength > 8192) {
        showError('输入文字过长，请缩短后再发送。');
        return;
      }
      expiryTick(ctx);
      if (!isCurrent(ctx)) return;
      showError();
      ctx.roundCounter += 1;
      const round = `text_${Date.now().toString(36)}_${ctx.roundCounter}`;
      appendMessage('user', text, '已发送');
      const row = appendMessage('assistant', '', '等待回复…');
      ui.input.value = '';
      const active = { round, row, controller: new win.AbortController(), terminal: false, interruptPending: false };
      ctx.active = active;
      ui.notice.textContent = '正在等待 Agent 回复…';
      render();
      try {
        const response = await win.fetch(endpoint(ctx, 'turns'), { method: 'POST', headers: headers(), credentials: 'same-origin', cache: 'no-store',
          body: JSON.stringify({ round_id: round, text }), signal: active.controller.signal });
        if (!isCurrent(ctx) || ctx.active !== active) { if (response.body) await response.body.cancel(); return; }
        if (!response.ok) {
          let code = '';
          try { code = safeCode((await response.json())?.detail?.code); } catch (_) { /* Keep the error content out of the UI. */ }
          throw { code, status: response.status };
        }
        if (!(response.headers.get('content-type') || '').toLowerCase().includes('text/event-stream')) throw { code: 'invalid_stream_response', status: 0 };
        await consumeSSE(response, (event) => {
          if (!isCurrent(ctx) || ctx.active !== active) return false;
          if (!['turn', 'delta', 'terminal'].includes(event.event)) return true;
          let data;
          try { data = JSON.parse(event.data); } catch (_) { throw { code: 'invalid_stream_event', status: 0 }; }
          if (!data || data.round_id !== round) return true;
          if (event.event === 'turn') row.state.textContent = '生成中…';
          if (event.event === 'delta') {
            if (typeof data.text !== 'string' || row.body.textContent.length + data.text.length > 20000) throw { code: 'invalid_stream_delta', status: 0 };
            row.body.textContent += data.text;
            row.state.textContent = '生成中…';
            ui.messages.scrollTop = ui.messages.scrollHeight;
          }
          if (event.event === 'terminal') {
            if (!terminalStates.has(data.state)) throw { code: 'invalid_stream_terminal', status: 0 };
            active.terminal = true;
            const label = data.state === 'generation_completed' ? '回复完成' : data.state === 'cancelled' ? '已打断' : '回复未完成';
            row.state.textContent = label;
            ui.notice.textContent = data.state === 'generation_completed' ? '回复已完成，可以继续发送。' : data.state === 'cancelled' ? '后端已结束本轮回复，可以继续发送。' : '本轮回复未完成，可以重试或继续发送。';
            if (data.state === 'failed') showError(errorText(safeCode(data.error_code), 0));
            ctx.active = null;
            render();
            return false;
          }
          return true;
        }, active.controller.signal);
        if (!active.terminal && isCurrent(ctx) && ctx.active === active && !ctx.stopping) throw { code: 'stream_closed_before_terminal', status: 0 };
      } catch (error) {
        if (!isCurrent(ctx) || ctx.active !== active || ctx.stopping) return;
        row.state.textContent = '回复连接已中断';
        showError(errorText(safeCode(error.code) || 'network_error', error.status));
        ui.notice.textContent = '本轮回复可能未完成；连接恢复后可重试或继续发送。';
        if (!ui.input.value) ui.input.value = text;
        if (error.status === 404 || error.status === 410) detach(ctx, '会话已到期');
      } finally {
        if (isCurrent(ctx)) {
          if (ctx.active === active) ctx.active = null;
          render();
          if (!ctx.stopping && !ctx.stopUnconfirmed) await syncSnapshot(ctx);
        }
      }
    }
    async function interrupt() {
      const ctx = current, active = ctx?.active;
      if (!ctx || !active || !isCurrent(ctx) || ctx.stopping || active.interruptPending) return;
      active.interruptPending = true;
      showError();
      ui.notice.textContent = '正在请求后端打断本轮回复…';
      render();
      try {
        const data = await jsonRequest(endpoint(ctx, 'interrupt'), 'POST', { round_id: active.round });
        if (!isCurrent(ctx) || ctx.active !== active) return;
        if (data.interrupted === true && data.round_id === active.round) ui.notice.textContent = '后端已接受打断，正在等待本轮终态…';
        else ui.notice.textContent = '后端未接受本轮打断，正在等待回复状态。';
      } catch (error) {
        if (isCurrent(ctx) && ctx.active === active) showError('打断尚未获得确认，可以重试或结束会话。');
      } finally {
        active.interruptPending = false;
        if (isCurrent(ctx)) render();
      }
    }
    async function closeSession() {
      const ctx = current;
      if (!ctx || !isCurrent(ctx) || ctx.stopping) return;
      ctx.stopping = true;
      if (ctx.active) {
        ctx.active.row.state.textContent = '正在结束会话…';
        ctx.active.controller.abort();
        ctx.active = null;
      }
      showError();
      render();
      try {
        const data = await jsonRequest(endpoint(ctx, 'stop'), 'POST', {});
        if (!isCurrent(ctx)) return;
        if (data.stopped !== true) throw { code: 'stop_not_confirmed', status: 0 };
        detach(ctx, '会话已结束');
        ui.notice.textContent = '文字会话已结束，可以选择模型后重新建立。';
      } catch (error) {
        if (!isCurrent(ctx)) return;
        if (error.status === 404 || error.status === 410) detach(ctx, '会话已到期');
        else {
          ctx.stopUnconfirmed = true;
          showError('结束会话尚未获得确认，请再次点击“结束会话”。');
        }
      } finally {
        ctx.stopping = false;
        render();
      }
    }
    function onPageHide() {
      generation += 1;
      creating = false;
      if (current) {
        const ctx = current;
        detach(ctx, '会话已离开');
        keepaliveStop(ctx);
      }
      render();
    }
    function onVisible() {
      if (doc.visibilityState === 'visible' && current) {
        expiryTick(current);
        if (current) { heartbeat(current); syncSnapshot(current); }
      }
    }
    function onSubmit(event) { event.preventDefault(); send(); }
    function onInput() { render(); }
    function onKey(event) {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        send();
      }
    }
    async function loadRtcAvailability() {
      try {
        const health = await jsonRequest('/healthz');
        if (disposed) return;
        const available = !Object.prototype.hasOwnProperty.call(health, 'transports')
          || (Array.isArray(health.transports) && health.transports.includes('rtc'));
        ui['rtc-link'].hidden = !available;
        ui['rtc-unavailable'].hidden = available;
      } catch (_) {
        if (!disposed) {
          ui['rtc-link'].hidden = true;
          ui['rtc-unavailable'].hidden = false;
        }
      }
    }
    async function init() {
      if (initialized || disposed) return;
      initialized = true;
      ui['start-session'].addEventListener('click', startSession);
      ui['stop-session'].addEventListener('click', closeSession);
      ui.interrupt.addEventListener('click', interrupt);
      ui.composer.addEventListener('submit', onSubmit);
      ui.input.addEventListener('input', onInput);
      ui.input.addEventListener('keydown', onKey);
      win.addEventListener('pagehide', onPageHide);
      doc.addEventListener('visibilitychange', onVisible);
      const rtcReady = loadRtcAvailability();
      try {
        const info = await jsonRequest('/api/m1/models');
        if (disposed) return;
        if (info.mode !== 'agent_loop' || !Array.isArray(info.profiles)) throw { code: 'invalid_model_inventory', status: 0 };
        ui['model-profile'].textContent = '';
        const available = [];
        for (const profile of info.profiles) {
          if (!profile || typeof profile.id !== 'string') continue;
          const option = doc.createElement('option');
          option.value = profile.id;
          option.textContent = `${profile.name || profile.id}${profile.configured ? '' : '（未配置）'}`;
          option.disabled = profile.configured !== true;
          ui['model-profile'].append(option);
          if (profile.configured === true) available.push(profile.id);
        }
        if (!available.length) throw { code: 'no_configured_models', status: 0 };
        ui['model-profile'].value = (available.includes(info.default_profile) ? info.default_profile : null)
          || available.find((id) => id === 'deepseek_v4_flash') || available.find((id) => /flash/i.test(id)) || available[0];
        modelsReady = true;
      } catch (error) {
        if (!disposed) showError(errorText(safeCode(error.code), error.status));
      }
      render();
      await rtcReady;
    }
    function destroy() {
      if (disposed) return;
      onPageHide();
      disposed = true;
      ui['start-session'].removeEventListener('click', startSession);
      ui['stop-session'].removeEventListener('click', closeSession);
      ui.interrupt.removeEventListener('click', interrupt);
      ui.composer.removeEventListener('submit', onSubmit);
      ui.input.removeEventListener('input', onInput);
      ui.input.removeEventListener('keydown', onKey);
      win.removeEventListener('pagehide', onPageHide);
      doc.removeEventListener('visibilitychange', onVisible);
      render();
    }
    function snapshot() {
      return { session_id: current?.session_id || null, profile_id: current?.profile_id || null,
        active_round: current?.active?.round || null, connected: !!current, creating, stopping: !!current?.stopping };
    }
    return { init, send, interrupt, closeSession, destroy, snapshot };
  }

  return { createSSEParser, consumeSSE, createTextConsole };
});
