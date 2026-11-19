/* M0/M1 browser harness. No dialogue, RTC token or raw SDK errors enter telemetry. */
(() => {
  'use strict';

  const byId = (id) => document.getElementById(id);
  const key = document.querySelector('meta[name="m0-client-key"]')?.content || '';
  const runtimeMode =
    document.querySelector('meta[name="runtime-mode"]')?.content || 'capture_only';
  const FIXED_TEXT = '请开始语音验证。';
  const MAX_TLV_BYTES = 65536;
  const EVENT_KINDS = new Set([
    'joined',
    'agent_joined',
    'agent_audio_published',
    'first_audio_decoded',
    'remote_level_active',
    'remote_level_silent',
    'mic_started',
    'mic_level_active',
    'mic_stopped',
    'mic_ended',
    'text_sent',
    'interrupt_sent',
    'autoplay_failed',
    'playback_resumed',
    'sdk_error',
    'player_playing',
    'heard_confirmed',
    'speech_input_confirmed',
    'stop_confirmed',
  ]);
  const ui = {
    join: byId('join'),
    text: byId('text-test'),
    mic: byId('mic'),
    interrupt: byId('interrupt'),
    stop: byId('stop'),
    resume: byId('resume'),
    heard: byId('heard'),
    speech: byId('speech-correct'),
    silent: byId('stopped-audio'),
    sample: byId('speech-sample'),
    cancelSample: byId('cancel-sample'),
    retryMeasurements: byId('retry-measurements'),
  };
  let current = null;
  let lastMeasurements = null;
  let busy = false;
  let pageLeaving = false;
  let modelInfo = null;

  function showAgentMode(description) {
    document.title = 'M1 · RTC Agent Loop';
    byId('runtime-label').textContent = 'backend_harness_loop / M1';
    byId('text-entry').hidden = false;
    byId('runtime-description').textContent = description;
    byId('text-test-description').textContent =
      '发送固定文字“请开始语音验证。”，等待所选主模型动态回复。回复内容和长度由模型决定，再经 RTC 合成语音；在 AI 有声时测试打断。';
    byId('confirm-description').textContent =
      '只有你实际听到、说过并核对后再点。SDK 事件、字幕和音量变化不会自动把 M1 判为通过。';
  }

  async function loadModelProfiles() {
    try {
      const info = await api('/api/m1/models');
      if (info.mode !== 'agent_loop' || !Array.isArray(info.profiles)) return;
      modelInfo = info;
      const select = byId('model-profile');
      for (const profile of info.profiles) {
        const option = document.createElement('option');
        option.value = profile.id;
        option.textContent = `${profile.name || profile.id} · ${profile.model}${
          profile.configured ? '' : '（未配置密钥）'
        }`;
        select.append(option);
      }
      select.value = info.default_profile;
      byId('model-panel').hidden = false;
      showAgentMode(
        '已接入真实 Agent Loop。所选主模型自主决定是否读取历史、更新笔记或回复；最终文本经 RTC 合成语音，对话历史与工具记录保存在本机。'
      );
      updateControls();
    } catch {
      // M0 has no model API; keep its existing fixed-response harness intact.
      if (runtimeMode === 'agent_loop')
        showError('无法加载 M1 模型配置，请检查后端状态后刷新页面。');
    }
  }

  function notice(message) {
    byId('notice').textContent = message;
  }
  function showError(message) {
    byId('error').textContent = message;
    byId('error').hidden = !message;
  }
  function log(message) {
    const item = document.createElement('li');
    const time = document.createElement('span');
    time.className = 'event-time';
    time.textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    item.append(time, document.createTextNode(message));
    const list = byId('event-log');
    list.prepend(item);
    while (list.children.length > 30) list.lastChild.remove();
  }
  const now = () => performance.now();
  const milliseconds = (value) => Math.min(3600000, Math.max(0, Math.round(value)));
  const elapsed = (ctx) => milliseconds(now() - ctx.clock);
  function safeCode(failure) {
    const code = failure && (failure.errorCode ?? failure.code);
    return Number.isSafeInteger(code) && Math.abs(code) <= 1000000000 ? code : undefined;
  }
  function isLive(ctx) {
    return current === ctx && !ctx.closing && !ctx.closed;
  }
  function isReady(ctx) {
    return isLive(ctx) && ctx.joined && ctx.running && ctx.agentJoined && !ctx.connectionLost;
  }

  async function api(path, body, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), options.timeout || 15000);
    try {
      const response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-M0-Client-Key': key },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        keepalive: Boolean(options.keepalive),
      });
      const text = await response.text();
      if (text.length > 65536) throw new Error('response_too_large');
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        /* Non-JSON errors receive only generic UI text. */
      }
      if (!response.ok) {
        const failure = new Error(`HTTP ${response.status}`);
        // Match only a stable, allowlisted code; never reflect provider messages or raw errors.
        if (response.status === 503 && data?.detail?.code === 'rtc_app_key_required') {
          failure.safeDetailCode = 'rtc_app_key_required';
        }
        if (response.status === 503 && data?.detail?.code === 'missing_model_api_key') {
          failure.safeDetailCode = 'missing_model_api_key';
        }
        throw failure;
      }
      if (data === undefined) throw new Error('invalid_response');
      return data;
    } finally {
      window.clearTimeout(timeout);
    }
  }
  function endpoint(ctx, suffix = '') {
    return `/api/m0/sessions/${encodeURIComponent(ctx.session.session_id)}${suffix}`;
  }
  function record(ctx, kind, extras = {}) {
    if (!ctx.session || !EVENT_KINDS.has(kind)) return Promise.resolve(false);
    const payload = {
      kind,
      elapsed_ms: Number.isFinite(extras.elapsed_ms)
        ? milliseconds(extras.elapsed_ms)
        : elapsed(ctx),
    };
    if (Number.isFinite(extras.duration_ms)) payload.duration_ms = milliseconds(extras.duration_ms);
    if (Number.isSafeInteger(extras.code) && Math.abs(extras.code) <= 1000000000)
      payload.code = extras.code;
    const isMicProof = ['mic_started', 'mic_level_active', 'mic_stopped', 'mic_ended'].includes(
      kind
    );
    const proofKey = `${kind}:${payload.elapsed_ms}`;
    const deliver = () =>
      api(endpoint(ctx, '/events'), payload)
        .then((response) => {
          if (response.recorded !== true) throw new Error('event_not_recorded');
          if (isMicProof) ctx.failedMicEvents.delete(proofKey);
          return true;
        })
        .catch(() => {
          if (isMicProof) ctx.failedMicEvents.set(proofKey, payload);
          if (!ctx.telemetryWarning && !pageLeaving) {
            ctx.telemetryWarning = true;
            log('部分客户端指标未能同步；请先保持页面打开，检查服务是否仍在运行。');
          }
          return false;
        });
    if (isMicProof) {
      ctx.micTelemetry = ctx.micTelemetry.then(deliver);
      return ctx.micTelemetry;
    }
    return deliver();
  }

  const phaseLabels = {
    waiting_speech: '请现在说一句话，然后保持安静',
    waiting_end: '正在收音，等待连续静音 500ms',
    waiting_audio: '语音终点已确认，等待首个回复音量',
    waiting_round: '音量已满足，等待新轮次关联',
    waiting_command: '正在发送打断指令',
    waiting_silence: '等待连续近似无声至少 1 秒',
  };
  const reasonLabels = {
    remote_not_silent: 'AI尚未静音',
    mic_not_started: '麦克风未开启',
    old_audio: '语音终点前出现远端声音',
    resumed_speech: '语音终点确认后又继续说话',
    overlapping_rounds: '多个新轮次重叠',
    missing_round: '缺少匹配轮次',
    command_failed: '打断发送失败',
    timeout: '采样超时',
    autoplay_failed: '浏览器阻止播放',
    track_ended: '采集轨道结束',
    mic_stopped: '麦克风关闭',
    session_closed: '会话结束',
    page_hidden: '页面隐藏',
    connection_lost: 'RTC连接中断',
    cancelled: '手动取消',
    clock_regressed: '时钟倒退',
  };
  function sampleLabel(kind) {
    return kind === 'speech_first_audio' ? '语音首音' : '打断停音';
  }
  function renderSampleRow(ctx, id) {
    const entry = ctx.sampleRows.get(id);
    if (!entry) return;
    entry.element.textContent = `#${entry.number} ${sampleLabel(entry.kind)} · ${
      entry.description
    }`;
  }
  function enqueueMeasurement(ctx, stage, payload) {
    const identity = `${stage}:${payload.sample_id}`;
    ctx.unsavedMeasurements.set(identity, { stage, payload });
    const micProof = ctx.micTelemetry;
    ctx.measurementQueue = ctx.measurementQueue.then(async () => {
      // The audio callback never waits for I/O. Proof events reach the server before terminal samples.
      try {
        if (stage === 'terminal') {
          // Re-register the immutable begin idempotently, even after an ambiguous earlier failure.
          const begun = await api(
            endpoint(ctx, '/measurement-starts'),
            ctx.sampleBegins.get(payload.sample_id),
            {
              keepalive: pageLeaving || document.hidden,
            }
          );
          if (begun.recorded !== true) throw new Error('measurement_begin_not_recorded');
          ctx.unsavedMeasurements.delete(`start:${payload.sample_id}`);
          await micProof;
          if (payload.kind === 'speech_first_audio' && payload.state === 'completed') {
            for (const [proofKey, proof] of [...ctx.failedMicEvents]) {
              if (proof.elapsed_ms > payload.trigger_ms) continue;
              const savedProof = await api(endpoint(ctx, '/events'), proof);
              if (savedProof.recorded !== true) throw new Error('mic_proof_not_recorded');
              ctx.failedMicEvents.delete(proofKey);
            }
          }
        }
        const saved = await api(
          endpoint(ctx, stage === 'start' ? '/measurement-starts' : '/measurements'),
          payload,
          {
            keepalive: pageLeaving || document.hidden,
          }
        );
        if (saved.recorded !== true) throw new Error('measurement_not_recorded');
        ctx.unsavedMeasurements.delete(identity);
        if (stage === 'terminal') {
          const entry = ctx.sampleRows.get(payload.sample_id);
          if (entry) {
            entry.description = entry.resultDescription + ' · 已保存';
            renderSampleRow(ctx, payload.sample_id);
          }
        }
      } catch {
        const entry = ctx.sampleRows.get(payload.sample_id);
        if (entry) {
          entry.description = (entry.resultDescription || '正在采样') + ' · 同步失败，可重试';
          renderSampleRow(ctx, payload.sample_id);
        }
        log('采样记录未能同步，已保留在页面内存。请保持页面打开并重试同步。');
      }
      updateControls();
      if (!pageLeaving) refresh(ctx);
    });
    updateControls();
  }
  function setupMeasurements(ctx) {
    lastMeasurements = ctx;
    ctx.tracker = window.M0Measurements.create({
      userId: ctx.session.user_id,
      agentId: ctx.session.agent_id,
      onStart(payload) {
        ctx.sampleStarted += 1;
        ctx.sampleBegins.set(payload.sample_id, payload);
        const element = document.createElement('li');
        byId('measurement-log').prepend(element);
        ctx.sampleRows.set(payload.sample_id, {
          element,
          number: ctx.sampleStarted,
          kind: payload.kind,
          description: '正在采样',
        });
        renderSampleRow(ctx, payload.sample_id);
        enqueueMeasurement(ctx, 'start', payload);
      },
      onChange(sample) {
        if (sample) {
          const entry = ctx.sampleRows.get(sample.sample_id);
          byId('sample-state').textContent = `#${
            entry?.number || ctx.sampleStarted + 1
          } ${sampleLabel(sample.kind)}：${phaseLabels[sample.phase] || '正在采样'}`;
        }
        updateControls();
      },
      onResult(payload) {
        const entry = ctx.sampleRows.get(payload.sample_id);
        const description =
          payload.state === 'completed'
            ? `${payload.observed_ms - payload.trigger_ms} ms（SDK估计）`
            : `无效：${reasonLabels[payload.reason] || '采样中断'}`;
        if (entry) {
          entry.resultDescription = description;
          entry.description = description + ' · 待保存';
          renderSampleRow(ctx, payload.sample_id);
        }
        byId('sample-state').textContent = `#${entry?.number || ctx.sampleStarted} ${sampleLabel(
          payload.kind
        )}：${description}`;
        if (payload.kind === 'interrupt_silence' && payload.state === 'completed') {
          byId('silence-estimate').textContent = `${
            payload.observed_ms - payload.trigger_ms
          } ms（仅 SDK 估计）`;
        }
        if (payload.kind === 'speech_first_audio' && ctx.speechBatch) {
          if (payload.state === 'invalid') {
            byId(
              'batch-state'
            ).textContent = `语音批次：第 ${ctx.speechBatch.attempts}/3 轮无效，批次已停止；修正后可重新开始。`;
            ctx.speechBatch = null;
          } else if (ctx.speechBatch.attempts >= 3) {
            ctx.speechBatch = null;
            byId('batch-state').textContent =
              '语音批次：3轮尝试已完成，服务端会核对不同RoundID与收音指标。';
          } else {
            ctx.speechBatch.waiting = true;
            byId(
              'batch-state'
            ).textContent = `语音批次：已完成 ${ctx.speechBatch.attempts}/3 轮；请等待AI静音，再按提示说下一句。`;
          }
        }
        enqueueMeasurement(ctx, 'terminal', payload);
      },
    });
  }
  function armSpeech(ctx) {
    if (!ctx.speechBatch || !isReady(ctx) || ctx.sampleStarted >= 20) return;
    if (ctx.tracker.speechWaitReason(elapsed(ctx), ctx.mic)) return;
    ctx.speechBatch.attempts += 1;
    ctx.speechBatch.waiting = false;
    const id = ctx.tracker.startSpeech(elapsed(ctx), ctx.mic);
    if (!id) {
      ctx.speechBatch.attempts -= 1;
      ctx.speechBatch.waiting = true;
      return;
    }
    byId(
      'batch-state'
    ).textContent = `语音批次：正在第 ${ctx.speechBatch.attempts}/3 轮。现在说一句，然后等回复。`;
    notice(`第 ${ctx.speechBatch.attempts}/3 轮采样已开始，请说一句话，然后保持安静等待AI回复。`);
  }
  function invalidateMeasurement(ctx, reason) {
    if (!ctx?.tracker) return;
    const hadBatch = Boolean(ctx.speechBatch);
    ctx.tracker.invalidate(reason, elapsed(ctx));
    ctx.speechBatch = null;
    if (hadBatch)
      byId('batch-state').textContent = `语音批次已停止：${reasonLabels[reason] || '采样中断'}。`;
    updateControls();
  }
  function showBatchWaiting(ctx, reason) {
    const labels = {
      processing_previous_round:
        '等待上一轮AI处理/回复结束（Stage4/5）；不要继续说话，也可先点击打断回复',
      remote_not_silent: '等待AI连续安静至少1秒；请先不要说话',
      local_not_silent: '等待你停止说话并保持安静500ms',
      mic_not_started: '请开启麦克风',
      sample_active: '当前轮正在采样',
    };
    const message = labels[reason] || '上一轮已结束，准备下一轮采样';
    byId(
      'batch-state'
    ).textContent = `语音批次：已完成 ${ctx.speechBatch.attempts}/3 轮；${message}。`;
    byId('sample-state').textContent = `尚未武装下一轮：${message}。看到“请现在说一句话”后再开口。`;
  }
  function renderMeasurementSummary(summary) {
    if (!summary || typeof summary !== 'object') return;
    for (const [kind, target] of [
      ['speech_first_audio', 'speech-summary'],
      ['interrupt_silence', 'interrupt-summary'],
    ]) {
      const value = summary[kind] || {};
      const fmt = (number) => (Number.isFinite(number) ? `${Math.round(number)} ms` : '—');
      byId(target).textContent = `${count(value.count)} / 3 个不同轮次 · P50 ${fmt(
        value.p50_ms
      )} / P95 ${fmt(value.p95_ms)}${value.ready === true ? ' · 样本数已满足' : ' · 待补样'}`;
    }
    byId('measurement-counts').textContent = `无效 ${count(summary.invalid_count)} · 未匹配 ${count(
      summary.unmatched_count
    )} · 重复 ${count(summary.duplicate_count)} · 未结束 ${count(summary.incomplete_count || 0)}`;
  }

  function updateControls() {
    const ctx = current;
    const ready = ctx && isReady(ctx);
    const activeSample = Boolean(ctx?.tracker?.snapshot());
    const sampling = activeSample || Boolean(ctx?.speechBatch);
    const uploadCtx = ctx || lastMeasurements;
    ui.join.disabled = busy || Boolean(ctx) || (runtimeMode === 'agent_loop' && !modelInfo);
    if (byId('model-profile')) byId('model-profile').disabled = busy || Boolean(ctx);
    ui.text.disabled = busy || !ready || sampling;
    ui.mic.disabled = busy || !ready;
    ui.interrupt.disabled = busy || !ready || activeSample || !ctx.testStarted;
    ui.sample.disabled = busy || !ready || sampling || ctx.sampleStarted >= 20 || !ctx.mic;
    ui.cancelSample.disabled = !ctx || !sampling;
    ui.retryMeasurements.hidden = !uploadCtx?.unsavedMeasurements.size;
    ui.retryMeasurements.disabled = busy || !uploadCtx?.session;
    ui.stop.disabled = !ctx || Boolean(ctx.stopPromise);
    ui.stop.textContent = ctx?.stopUnknown ? '重试停止云端任务' : '结束并释放会话';
    ui.mic.textContent = ctx?.mic ? '关闭麦克风' : '开启麦克风';
    ui.resume.disabled = busy || !ready;
    ui.resume.hidden = !ctx?.autoplayBlocked;
    ui.heard.disabled =
      busy || !ready || !ctx.testStarted || ctx.confirmations.has('heard_confirmed');
    ui.speech.disabled =
      busy || !ready || !ctx.micEverStarted || ctx.confirmations.has('speech_input_confirmed');
    ui.silent.disabled =
      busy || !ready || !ctx.interruptSent || ctx.confirmations.has('stop_confirmed');
    for (const [button, kind] of [
      [ui.heard, 'heard_confirmed'],
      [ui.speech, 'speech_input_confirmed'],
      [ui.silent, 'stop_confirmed'],
    ]) {
      button.classList.toggle('confirmed', Boolean(ctx?.confirmations.has(kind)));
    }
    if (ctx) {
      const labels = [];
      if (ctx.confirmations.has('heard_confirmed')) labels.push('听到回复');
      if (ctx.confirmations.has('speech_input_confirmed')) labels.push('语音输入正确');
      if (ctx.confirmations.has('stop_confirmed')) labels.push('打断后已停音');
      byId('confirm-state').textContent = `人工确认：${labels.join('、') || '尚未记录'}`;
      byId('mic-state').textContent = ctx.mic ? '已开启' : '关闭';
    }
  }
  function setConnection(ctx) {
    if (current !== ctx) return;
    let text = '正在连接';
    if (ctx.stopUnknown) text = '本地已释放 · 云端停止待重试';
    else if (ctx.closing) text = '正在结束';
    else if (ctx.connectionLost) text = 'RTC 连接中断 · 等待重连';
    else if (ctx.running && ctx.agentJoined) text = '已就绪';
    else if (ctx.running) text = '等待 AI 进入';
    else if (ctx.joined) text = '已进房 · 正在启动 AI';
    byId('connection-state').textContent = text;
    updateControls();
  }
  function showExpiry(value) {
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    byId('expires-at').textContent = Number.isFinite(date.getTime())
      ? date.toLocaleTimeString('zh-CN', { hour12: false })
      : '—';
  }
  function count(value) {
    if (Array.isArray(value)) return value.length;
    if (value && typeof value === 'object') {
      return Object.values(value).reduce(
        (sum, amount) => sum + (Number.isFinite(amount) && amount >= 0 ? Math.floor(amount) : 0),
        0
      );
    }
    return Number.isFinite(value) && value >= 0 ? Math.floor(value) : '—';
  }
  async function refresh(ctx) {
    if (!ctx.session || ctx.pollPending) return;
    ctx.pollPending = true;
    try {
      const data = await api(endpoint(ctx));
      if (current !== ctx && !(current === null && lastMeasurements === ctx)) return;
      const evidence = data.server_evidence || {};
      byId('server-rounds').textContent = count(evidence.custom_llm_rounds);
      byId('matched-rounds').textContent = count(evidence.matched_rounds);
      byId('rts-errors').textContent = count(evidence.rts_errors);
      renderMeasurementSummary(data.measurements_summary);
      if (ctx.session.runtime_mode === 'agent_loop') {
        try {
          const state = await api(`/api/m1/sessions/${encodeURIComponent(ctx.session.session_id)}`);
          byId('agent-status').textContent = `Agent Loop · ${state.profile_id} · epoch ${
            state.epoch
          } · 笔记版本 ${state.notes_version} · 压缩 ${count(state.context_compaction_count)} 次${
            state.context_maintenance_running ? ' · 正在维护上下文' : ''
          }${state.last_error_code ? ' · 本轮执行未完成' : ''}`;
        } catch {
          byId('agent-status').textContent = 'Agent Loop 状态暂不可用；RTC 会话仍按原状态管理。';
        }
      }
      if (data.expires_at) showExpiry(data.expires_at);
      if (['stopped', 'expired'].includes(data.state) && !ctx.closing) {
        log('服务端会话已结束，正在释放本地连接。');
        await stopContext(ctx);
      }
    } catch {
      if (!ctx.statusWarning && isLive(ctx)) {
        ctx.statusWarning = true;
        log('暂时无法读取服务端状态；结束时仍会尝试停止云端任务。');
      }
    } finally {
      ctx.pollPending = false;
    }
  }
  function startTimers(ctx) {
    ctx.heartbeat = window.setInterval(() => {
      if (!isLive(ctx)) return;
      api(endpoint(ctx, '/heartbeat'), {}).catch(() => {
        if (!ctx.heartbeatWarning) {
          ctx.heartbeatWarning = true;
          log('会话心跳未成功；请检查服务，避免测试过程中租约过期。');
        }
      });
    }, 15000);
    ctx.poll = window.setInterval(() => refresh(ctx), 3000);
    ctx.measurementTick = window.setInterval(() => {
      if (!isLive(ctx) || !ctx.tracker) return;
      const at = elapsed(ctx);
      ctx.tracker.tick(at);
      if (ctx.speechBatch?.waiting && !ctx.tracker.snapshot()) {
        const reason = ctx.tracker.speechWaitReason(at, ctx.mic);
        if (reason) showBatchWaiting(ctx, reason);
        else armSpeech(ctx);
      }
      updateControls();
    }, 100);
  }
  function clearTimers(ctx) {
    window.clearInterval(ctx.heartbeat);
    window.clearInterval(ctx.poll);
    window.clearInterval(ctx.measurementTick);
  }

  function addSubtitle(ctx, message) {
    if (
      !isLive(ctx) ||
      !(message instanceof ArrayBuffer) ||
      message.byteLength < 8 ||
      message.byteLength > MAX_TLV_BYTES
    )
      return;
    try {
      const bytes = new Uint8Array(message);
      const type = String.fromCharCode(...bytes.subarray(0, 4));
      const length = new DataView(message).getUint32(4, false);
      if (length !== message.byteLength - 8 || !['subv', 'conv'].includes(type)) return;
      const body = new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(8));
      const fingerprint = `${type}:${body}`;
      if (ctx.recentMessages.has(fingerprint)) return;
      ctx.recentMessages.add(fingerprint);
      if (ctx.recentMessages.size > 24)
        ctx.recentMessages.delete(ctx.recentMessages.values().next().value);
      const parsed = JSON.parse(body);
      const at = elapsed(ctx);
      const roundValue = (value) =>
        typeof value === 'string' || Number.isSafeInteger(value) ? String(value) : null;
      if (type === 'conv') {
        const id = roundValue(parsed.RoundID);
        if (id !== null)
          ctx.tracker?.round({ source: 'conv', round_id: id, stage: parsed.Stage?.Code }, at);
      }
      if (type !== 'subv' || !Array.isArray(parsed.data)) return;
      const list = byId('subtitles');
      for (const item of parsed.data.slice(0, 8)) {
        const id = roundValue(item?.roundId);
        if (id !== null)
          ctx.tracker?.round({ source: 'subv', round_id: id, user_id: item?.userId }, at);
        if (!item || typeof item.text !== 'string' || !item.text) continue;
        const role =
          item.userId === ctx.session.user_id
            ? '你'
            : item.userId === ctx.session.agent_id
            ? 'AI'
            : '字幕';
        const subtitle = document.createElement('li');
        const speaker = document.createElement('span');
        speaker.className = 'subtitle-role';
        speaker.textContent = role;
        subtitle.append(speaker, document.createTextNode(item.text.slice(0, 2000)));
        if (list.firstChild?.classList?.contains('empty')) list.replaceChildren();
        list.append(subtitle);
        while (list.children.length > 12) list.firstChild.remove();
      }
      list.scrollTop = list.scrollHeight;
    } catch {
      /* Malformed or unsupported messages are ignored; never upload raw payloads. */
    }
  }
  function controlMessage(command, message = '') {
    const value = new TextEncoder().encode(
      JSON.stringify({ Command: command, InterruptMode: 1, Message: message })
    );
    const buffer = new ArrayBuffer(8 + value.length);
    const bytes = new Uint8Array(buffer);
    bytes.set(new TextEncoder().encode('ctrl'), 0);
    new DataView(buffer).setUint32(4, value.length, false);
    bytes.set(value, 8);
    return buffer;
  }
  function bindEvents(ctx) {
    const engine = ctx.engine;
    const events = window.VERTC.events;
    const on = (name, handler) => {
      if (events[name])
        engine.on(events[name], (event) => {
          if (isLive(ctx)) handler(event);
        });
    };
    on('onUserJoined', (event) => {
      if (event?.userInfo?.userId !== ctx.session.agent_id) return;
      if (!ctx.agentJoined) {
        ctx.agentJoined = true;
        record(ctx, 'agent_joined');
        log('AI 已进入房间。');
      }
      setConnection(ctx);
    });
    on('onUserLeave', (event) => {
      if (event?.userInfo?.userId !== ctx.session.agent_id) return;
      invalidateMeasurement(ctx, 'connection_lost');
      ctx.agentJoined = false;
      log('AI 已离开房间；请结束会话后重新测试。');
      setConnection(ctx);
    });
    on('onUserPublishStream', (event) => {
      if (
        event?.userId !== ctx.session.agent_id ||
        !(event.mediaType & window.VERTC.MediaType.AUDIO)
      )
        return;
      ctx.agentJoined = true;
      record(ctx, 'agent_audio_published');
      log('已观测到 AI 发布音频流。');
      setConnection(ctx);
    });
    on('onRemoteAudioFirstFrame', (event) => {
      if (event?.userId !== ctx.session.agent_id || event.isScreen || ctx.firstFrame) return;
      ctx.firstFrame = true;
      record(ctx, 'first_audio_decoded');
      byId('first-frame').textContent = '已解码（不代表已听到）';
      log('SDK 已解码远端首帧音频。');
    });
    on('onPlayerEvent', (event) => {
      if (event?.userId !== ctx.session.agent_id || event.type !== 'audio') return;
      if ((event.eventName || event.rawEvent?.type) === 'playing' && !ctx.playerPlaying) {
        ctx.playerPlaying = true;
        ctx.autoplayBlocked = false;
        record(ctx, 'player_playing');
        byId('play-state').textContent = '播放器已开始';
        log('浏览器播放器报告 playing；请实际听音确认。');
        updateControls();
      }
    });
    on('onAutoplayFailed', (event) => {
      if (event?.userId !== ctx.session.agent_id || event.kind !== 'audio') return;
      ctx.autoplayBlocked = true;
      invalidateMeasurement(ctx, 'autoplay_failed');
      record(ctx, 'autoplay_failed');
      byId('play-state').textContent = '需要点击恢复';
      notice('浏览器阻止了自动播放，请点击“恢复声音”后再听。');
      log('浏览器自动播放受限。');
      updateControls();
    });
    on('onLocalAudioPropertiesReport', (items) => {
      const info =
        Array.isArray(items) &&
        items.find((item) => item.streamIndex === window.VERTC.StreamIndex.STREAM_INDEX_MAIN);
      const volume = info?.audioPropertiesInfo?.linearVolume;
      if (Number.isFinite(volume)) {
        const at = elapsed(ctx);
        byId('local-level').textContent = `${Math.max(0, Math.min(255, Math.round(volume)))} / 255`;
        const sample = ctx.tracker?.snapshot();
        const newSampleEnergy =
          sample?.kind === 'speech_first_audio' && !ctx.sampleEnergyIds.has(sample.sample_id);
        if (ctx.mic && volume > 25 && (!ctx.micEnergyRecorded || newSampleEnergy)) {
          ctx.micEnergyRecorded = true;
          if (newSampleEnergy) ctx.sampleEnergyIds.add(sample.sample_id);
          record(ctx, 'mic_level_active', { elapsed_ms: at });
        }
        ctx.tracker?.local(volume, at);
      }
    });
    on('onRemoteAudioPropertiesReport', (items) => {
      const info =
        Array.isArray(items) &&
        items.find(
          (item) =>
            item.streamKey?.userId === ctx.session.agent_id &&
            item.streamKey.streamIndex === window.VERTC.StreamIndex.STREAM_INDEX_MAIN
        );
      const volume = info?.audioPropertiesInfo?.linearVolume;
      if (!Number.isFinite(volume)) return;
      ctx.tracker?.remote(volume, elapsed(ctx));
      byId('remote-level').textContent = `${Math.max(0, Math.min(255, Math.round(volume)))} / 255`;
      const active = volume > 25;
      if (active === ctx.levelCandidate) ctx.levelSamples += 1;
      else {
        ctx.levelCandidate = active;
        ctx.levelSamples = 1;
      }
      updateControls();
      if (ctx.levelSamples < (active ? 2 : 3) || ctx.remoteActive === active) return;
      ctx.remoteActive = active;
      if (active) {
        record(ctx, 'remote_level_active');
      } else {
        record(ctx, 'remote_level_silent');
      }
    });
    on('onTrackEnded', (event) => {
      if (event?.kind !== 'audio' || event.isScreen || !ctx.mic) return;
      invalidateMeasurement(ctx, 'track_ended');
      ctx.mic = false;
      record(ctx, 'mic_ended');
      log('麦克风采集已结束；需要时可重新点击开启。');
      updateControls();
    });
    on('onRoomBinaryMessageReceived', (event) => addSubtitle(ctx, event?.message));
    on('onUserBinaryMessageReceived', (event) => addSubtitle(ctx, event?.message));
    on('onError', (event) => {
      invalidateMeasurement(ctx, 'connection_lost');
      const code = safeCode(event);
      record(ctx, 'sdk_error', { code });
      showError(
        `RTC SDK 报告错误${code !== undefined ? `（代码 ${code}）` : ''}。请结束会话后重试。`
      );
      log('RTC SDK 报告错误，未采集原始错误内容。');
    });
    on('onConnectionStateChanged', (event) => {
      if (!ctx.joined) return;
      if ([1, 4, 6].includes(event?.state)) {
        ctx.connectionLost = true;
        invalidateMeasurement(ctx, 'connection_lost');
      } else if ([3, 5].includes(event?.state)) ctx.connectionLost = false;
      setConnection(ctx);
    });
  }

  async function settleWithin(promise, delay = 2000) {
    let timer;
    try {
      await Promise.race([
        Promise.resolve(promise).catch(() => {}),
        new Promise((resolve) => {
          timer = window.setTimeout(resolve, delay);
        }),
      ]);
    } finally {
      window.clearTimeout(timer);
    }
  }
  async function releaseLocal(ctx) {
    if (ctx.localRelease) return ctx.localRelease;
    const engine = ctx.engine;
    ctx.engine = null;
    const wasMic = ctx.mic;
    ctx.mic = false;
    ctx.joined = false;
    byId('mic-state').textContent = '关闭';
    byId('local-level').textContent = '0 / 255';
    byId('remote-level').textContent = '0 / 255';
    byId('subtitles').replaceChildren();
    ctx.recentMessages.clear();
    if (wasMic) record(ctx, 'mic_stopped');
    ctx.localRelease = (async () => {
      if (!engine) return;
      try {
        await settleWithin(engine.stopAudioCapture());
        await settleWithin(engine.leaveRoom());
      } catch {
        /* destroy is still required after a rejected capture or join. */
      } finally {
        try {
          window.VERTC.destroyEngine(engine);
        } catch {
          /* Already released. */
        }
      }
    })();
    return ctx.localRelease;
  }
  async function stopContext(ctx) {
    if (ctx.stopPromise) return ctx.stopPromise;
    invalidateMeasurement(ctx, 'session_closed');
    ctx.closing = true;
    ctx.running = false;
    clearTimers(ctx);
    const local = releaseLocal(ctx);
    if (!ctx.session) {
      await local;
      ctx.closed = true;
      if (current === ctx) {
        current = null;
        byId('connection-state').textContent = '连接未完成';
        ui.resume.hidden = true;
      }
      updateControls();
      return;
    }
    ctx.stopPromise = (async () => {
      let stopped = false;
      try {
        const result = await api(endpoint(ctx, '/stop'), {}, { timeout: 45000 });
        stopped = result.stopped === true;
      } catch {
        /* Unknown means keep the identity available for an explicit retry. */
      }
      await local;
      await refresh(ctx);
      if (stopped) {
        ctx.closed = true;
        if (current === ctx) {
          current = null;
          byId('connection-state').textContent = '已结束';
          ui.resume.hidden = true;
          log('本地连接已释放，云端任务已确认停止。');
          notice(
            ctx.unsavedMeasurements.size
              ? '本轮会话已结束，部分采样仍待同步。请保持页面打开；若同步失败，可点击“重试同步样本”。'
              : '本轮会话已结束。已确认保存的指标与人工确认可在验证后端查看；可重新加入再测。'
          );
        }
      } else {
        ctx.stopUnknown = true;
        log('本地已释放；云端停止结果未知，需要重试。');
        showError('云端停止尚未得到确认。请保持验证后端运行，点击“重试停止云端任务”。');
      }
    })();
    setConnection(ctx);
    try {
      await ctx.stopPromise;
    } finally {
      ctx.stopPromise = null;
      if (current === ctx) setConnection(ctx);
      else updateControls();
    }
  }

  async function join() {
    if (busy || current) return;
    showError('');
    if (
      !window.isSecureContext ||
      !window.VERTC ||
      !window.M0Measurements ||
      !key ||
      key === '__M0_CLIENT_KEY__'
    ) {
      showError('页面或 RTC SDK 未就绪。请使用验证后端提供的 localhost 页面或 HTTPS 地址。');
      return;
    }
    const ctx = {
      clock: now(),
      session: null,
      engine: null,
      closing: false,
      closed: false,
      joined: false,
      running: false,
      agentJoined: false,
      mic: false,
      micEverStarted: false,
      testStarted: false,
      interruptSent: false,
      stopPromise: null,
      localRelease: null,
      stopUnknown: false,
      confirmations: new Set(),
      recentMessages: new Set(),
      remoteActive: null,
      levelCandidate: null,
      levelSamples: 0,
      micTelemetry: Promise.resolve(true),
      failedMicEvents: new Map(),
      micEnergyRecorded: false,
      sampleEnergyIds: new Set(),
      measurementQueue: Promise.resolve(),
      unsavedMeasurements: new Map(),
      sampleRows: new Map(),
      sampleBegins: new Map(),
      sampleStarted: 0,
      speechBatch: null,
    };
    current = ctx;
    busy = true;
    setConnection(ctx);
    byId('event-log').replaceChildren();
    byId('subtitles').replaceChildren();
    byId('first-frame').textContent = '待观测';
    byId('silence-estimate').textContent = '待打断';
    byId('play-state').textContent = '待验证';
    byId('server-rounds').textContent = '—';
    byId('matched-rounds').textContent = '—';
    byId('rts-errors').textContent = '—';
    byId('measurement-log').replaceChildren();
    byId('sample-state').textContent = '尚未采样。开启麦克风后等待本地与AI静音。';
    byId('batch-state').textContent = '语音批次：未开始';
    try {
      // Disable SDK console output so tokens and raw provider data stay out of page logs.
      window.VERTC.setLogConfig({ logLevel: 'none' });
      if (!(await window.VERTC.isSupported())) throw new Error('unsupported_browser');
      if (!isLive(ctx)) return;
      const session = await api(
        '/api/m0/sessions',
        modelInfo ? { profile_id: byId('model-profile').value } : {}
      );
      if (
        !session ||
        !['session_id', 'app_id', 'room_id', 'user_id', 'agent_id', 'token'].every(
          (name) => typeof session[name] === 'string' && session[name].length > 0
        )
      ) {
        throw new Error('invalid_session');
      }
      ctx.session = session;
      setupMeasurements(ctx);
      showExpiry(session.expires_at);
      if (!isLive(ctx)) {
        await stopContext(ctx);
        return;
      }
      ctx.engine = window.VERTC.createEngine(session.app_id);
      bindEvents(ctx);
      ctx.engine.enableAudioPropertiesReport({ interval: 100 });
      await ctx.engine.joinRoom(
        session.token,
        session.room_id,
        { userId: session.user_id },
        {
          isAutoPublish: true,
          isAutoSubscribeAudio: true,
          roomProfileType: window.VERTC.RoomProfileType.chat,
        }
      );
      // Do not retain the join token after handing it to the SDK.
      delete session.token;
      if (!isLive(ctx)) {
        await stopContext(ctx);
        return;
      }
      ctx.joined = true;
      record(ctx, 'joined');
      log('已加入独立验证房间，麦克风保持关闭。');
      setConnection(ctx);
      startTimers(ctx);
      const result = await api(endpoint(ctx, '/start'), {}, { timeout: 35000 });
      if (!isLive(ctx)) {
        await stopContext(ctx);
        return;
      }
      if (result.running !== true) throw new Error('start_not_confirmed');
      ctx.running = true;
      notice('等待 AI 进入后，先点击“发送固定文字”。听到声音后再开启麦克风测试。');
      log('云端启动请求已确认。');
      setConnection(ctx);
      await refresh(ctx);
    } catch (failure) {
      if (!ctx.closing) {
        const code = safeCode(failure);
        if (code !== undefined) record(ctx, 'sdk_error', { code });
        showError(
          failure?.safeDetailCode === 'rtc_app_key_required'
            ? 'RTC AppKey 未配置为应用密钥，请在本地配置后重启验证服务'
            : failure?.safeDetailCode === 'missing_model_api_key'
            ? '所选模型尚未配置密钥，请在后端本地 .env 中配置对应密钥并重启服务。'
            : '连接未完成。正在释放本地连接并尝试停止本轮云端任务，请检查后端状态后重试。'
        );
        log('连接失败，已进入清理流程。');
      }
      await stopContext(ctx);
      if (!ctx.session && current === ctx) {
        current = null;
        byId('connection-state').textContent = '连接未完成';
      }
    } finally {
      busy = false;
      if (ctx.closing && ctx.session && !ctx.closed && !ctx.stopUnknown) await stopContext(ctx);
      if (!ctx.session && ctx.closing && current === ctx) current = null;
      updateControls();
    }
  }

  async function action(task) {
    const ctx = current;
    if (busy || !ctx || !isReady(ctx)) return;
    busy = true;
    updateControls();
    showError('');
    try {
      await task(ctx);
    } catch (failure) {
      if (isLive(ctx)) {
        const code = safeCode(failure);
        record(ctx, 'sdk_error', { code });
        showError(
          `本次操作未完成${
            code !== undefined ? `（代码 ${code}）` : ''
          }。请检查麦克风权限或连接状态后重试。`
        );
      }
    } finally {
      busy = false;
      updateControls();
    }
  }
  ui.join.addEventListener('click', join);
  ui.text.addEventListener('click', () =>
    action(async (ctx) => {
      ctx.testStarted = true;
      await ctx.engine.sendUserBinaryMessage(
        ctx.session.agent_id,
        controlMessage('ExternalTextToLLM', FIXED_TEXT)
      );
      if (!isLive(ctx)) return;
      record(ctx, 'text_sent');
      log('固定文字已通过 RTC ctrl 指令发送。');
      notice('请听回复。如果浏览器阻止播放，点击“恢复声音”。实际听到后点击“听到回复”。');
    })
  );
  ui.mic.addEventListener('click', () =>
    action(async (ctx) => {
      if (ctx.mic) {
        invalidateMeasurement(ctx, 'mic_stopped');
        ctx.mic = false;
        await ctx.engine.stopAudioCapture();
        record(ctx, 'mic_stopped');
        log('麦克风已关闭。');
        byId('local-level').textContent = '0 / 255';
      } else {
        const engine = ctx.engine;
        await engine.startAudioCapture();
        if (!isLive(ctx)) {
          try {
            await engine.stopAudioCapture();
          } catch {
            /* Session was already destroyed. */
          }
          return;
        }
        ctx.mic = true;
        ctx.micEnergyRecorded = false;
        ctx.micEverStarted = true;
        ctx.testStarted = true;
        record(ctx, 'mic_started');
        log('麦克风已开启，仅采集音频。');
        notice('现在说“你好，请开始语音验证”。核对临时字幕和 AI 回复后，点击“语音输入正确”。');
      }
    })
  );
  ui.interrupt.addEventListener('click', () =>
    action(async (ctx) => {
      const sample = ctx.sampleStarted < 20 ? ctx.tracker.startInterrupt(elapsed(ctx)) : null;
      byId('silence-estimate').textContent = sample
        ? '等待连续近似无声至少1秒'
        : '本次未武装：需新鲜的AI有声报告且无其它采样';
      try {
        await ctx.engine.sendUserBinaryMessage(ctx.session.agent_id, controlMessage('interrupt'));
      } catch (failure) {
        if (sample) ctx.tracker.commandResult(false, elapsed(ctx));
        throw failure;
      }
      if (sample) ctx.tracker.commandResult(true, elapsed(ctx));
      if (!isLive(ctx)) return;
      ctx.interruptSent = true;
      record(ctx, 'interrupt_sent');
      log('打断指令已发送。');
      notice('请实际听音。确认 AI 已停音后，点击“打断后已停音”。仅 SDK 近似无声不会自动通过。');
    })
  );
  ui.sample.addEventListener('click', () =>
    action(async (ctx) => {
      if (document.hidden || ctx.speechBatch || ctx.tracker.snapshot() || ctx.sampleStarted >= 20)
        return;
      ctx.speechBatch = { attempts: 0, waiting: true };
      showBatchWaiting(ctx, ctx.tracker.speechWaitReason(elapsed(ctx), ctx.mic));
    })
  );
  ui.cancelSample.addEventListener('click', () => {
    if (current) invalidateMeasurement(current, 'cancelled');
  });
  ui.retryMeasurements.addEventListener('click', () => {
    const ctx = current || lastMeasurements;
    if (!ctx?.session) return;
    for (const { stage, payload } of [...ctx.unsavedMeasurements.values()])
      enqueueMeasurement(ctx, stage, payload);
  });
  ui.resume.addEventListener('click', () =>
    action(async (ctx) => {
      // action invokes this task synchronously in the click handler, preserving user activation.
      await ctx.engine.play(
        ctx.session.agent_id,
        window.VERTC.MediaType.AUDIO,
        window.VERTC.StreamIndex.STREAM_INDEX_MAIN
      );
      if (!isLive(ctx)) return;
      ctx.autoplayBlocked = false;
      record(ctx, 'playback_resumed');
      byId('play-state').textContent = '已请求恢复播放';
      log('已请求浏览器恢复音频播放。');
    })
  );
  for (const [button, kind, label] of [
    [ui.heard, 'heard_confirmed', '听到回复'],
    [ui.speech, 'speech_input_confirmed', '语音输入正确'],
    [ui.silent, 'stop_confirmed', '打断后已停音'],
  ]) {
    button.addEventListener('click', () =>
      action(async (ctx) => {
        if (ctx.confirmations.has(kind)) return;
        const ok = await record(ctx, kind);
        if (!ok) {
          showError('人工确认未能保存，请检查后端后再次点击。');
          return;
        }
        ctx.confirmations.add(kind);
        log(`已保存人工确认：${label}。`);
      })
    );
  }
  ui.stop.addEventListener('click', async () => {
    if (current) await stopContext(current);
  });
  window.addEventListener('pagehide', () => {
    pageLeaving = true;
    const ctx = current;
    if (!ctx || !ctx.session || ctx.closed) return;
    invalidateMeasurement(ctx, 'page_hidden');
    ctx.closing = true;
    clearTimers(ctx);
    // Release immediately; unload does not guarantee any asynchronous operation completes.
    releaseLocal(ctx);
    // Keep the same authenticated request as normal stop; the lease remains the fallback.
    api(endpoint(ctx, '/stop'), {}, { keepalive: true, timeout: 45000 }).catch(() => {});
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && current) invalidateMeasurement(current, 'page_hidden');
  });

  if (runtimeMode === 'agent_loop') {
    showAgentMode('正在加载主模型配置。对话历史与工具记录保存在本机，用于后续上下文。');
  }
  updateControls();
  loadModelProfiles();
})();
