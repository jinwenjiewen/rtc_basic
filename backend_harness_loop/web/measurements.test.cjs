const test = require('node:test');
const assert = require('node:assert/strict');
const { create } = require('./measurements.js');

function fixture() {
  const results = [];
  let serial = 0;
  const tracker = create({
    userId: 'user',
    agentId: 'agent',
    idFactory: () => 'm0_sample_' + (++serial).toString(16).padStart(32, '0'),
    onResult: (value) => results.push(value),
  });
  return { tracker, results };
}
function quiet(t, start = 0) {
  for (let at = start; at <= start + 300; at += 100) t.remote(0, at);
}
function voiceEnd(t) {
  t.local(90, 500);
  t.local(80, 600);
  for (let at = 700; at <= 1200; at += 100) t.local(0, at);
}
function round(t, id, at, source = 'conv', user_id = 'agent') {
  t.round({ round_id: id, source, stage: 2, user_id }, at);
}
function speaking(t) {
  round(t, 'round_a', 0);
  t.remote(100, 100);
  t.remote(100, 200);
}

test('speech uses last local voice and first of two high remote reports, never decode/player time', () => {
  const { tracker: t, results } = fixture();
  quiet(t);
  t.startSpeech(400, true);
  voiceEnd(t);
  round(t, 'new_round', 1250);
  t.remote(70, 1300);
  assert.equal(results.length, 0);
  t.remote(80, 1400);
  assert.equal(results[0].state, 'completed');
  assert.equal(results[0].trigger_ms, 600);
  assert.equal(results[0].observed_ms, 1300);
  assert.equal(results[0].confirmed_ms, 1400);
});
test('old audio arriving before the speech endpoint invalidates the sample', () => {
  const { tracker: t, results } = fixture();
  quiet(t);
  t.startSpeech(400, true);
  t.local(80, 500);
  t.remote(90, 600);
  t.local(80, 700);
  assert.equal(results[0].reason, 'old_audio');
});
test('a 300ms fast response is buffered until the 500ms endpoint guard instead of being excluded', () => {
  const { tracker: t, results } = fixture();
  quiet(t);
  t.startSpeech(400, true);
  t.local(80, 500);
  t.local(0, 600);
  t.local(0, 700);
  round(t, 'fast_round', 750);
  t.remote(80, 800);
  t.local(0, 850);
  t.remote(80, 900);
  assert.equal(results.length, 0);
  t.local(0, 1000);
  t.local(0, 1100);
  assert.equal(results[0].state, 'completed');
  assert.equal(results[0].observed_ms, 800);
  assert.equal(results[0].trigger_ms, 500);
  assert.equal(results[0].confirmed_ms, 1100);
});
test('a short pause within a sentence updates the endpoint instead of completing the old one', () => {
  const { tracker: t, results } = fixture();
  quiet(t);
  t.startSpeech(400, true);
  t.local(80, 500);
  t.local(0, 600);
  t.local(0, 800);
  t.local(80, 900);
  for (let at = 1000; at <= 1500; at += 100) t.local(0, at);
  round(t, 'new', 1550);
  t.remote(70, 1600);
  t.remote(70, 1700);
  assert.equal(results[0].trigger_ms, 900);
  assert.equal(results[0].observed_ms, 1600);
});
test('speaking again after the confirmed endpoint invalidates rather than inventing a latency', () => {
  const { tracker: t, results } = fixture();
  quiet(t);
  t.startSpeech(400, true);
  voiceEnd(t);
  t.local(90, 1300);
  assert.equal(results[0].reason, 'resumed_speech');
});
test('an old round cannot associate a new speech sample; missing round is explicit', () => {
  const { tracker: t, results } = fixture();
  round(t, 'old', 0);
  t.round({ source: 'conv', round_id: 'old', stage: 5 }, 50);
  quiet(t, 100);
  t.startSpeech(450, true);
  voiceEnd(t);
  round(t, 'old', 1250);
  t.remote(80, 1300);
  t.remote(80, 1400);
  t.tick(30450);
  assert.equal(results[0].reason, 'missing_round');
});
test('overlapping new rounds are invalid and unknown subtitle speakers cannot bind', () => {
  const { tracker: t, results } = fixture();
  quiet(t);
  t.startSpeech(400, true);
  t.local(80, 500);
  round(t, 'outsider', 550, 'subv', 'outsider');
  assert.equal(t.snapshot().round_id, null);
  round(t, 'a', 600, 'subv', 'user');
  round(t, 'b', 650);
  assert.equal(results[0].reason, 'overlapping_rounds');
});
test('failed interrupt send stays invalid even if quiet appears while the command is pending', () => {
  const { tracker: t, results } = fixture();
  speaking(t);
  t.startInterrupt(300);
  for (let at = 400; at <= 1500; at += 100) t.remote(0, at);
  assert.equal(results.length, 0);
  t.commandResult(false, 1600);
  assert.equal(results[0].reason, 'command_failed');
});
test('interrupt requires one full second of uninterrupted quiet and records first low time', () => {
  const { tracker: t, results } = fixture();
  speaking(t);
  t.startInterrupt(300);
  t.commandResult(true, 350);
  for (let at = 400; at <= 800; at += 100) t.remote(0, at);
  assert.equal(results.length, 0);
  t.remote(80, 900);
  for (let at = 1000; at <= 1900; at += 100) t.remote(0, at);
  assert.equal(results.length, 0);
  t.remote(0, 2000);
  assert.equal(results[0].state, 'completed');
  assert.equal(results[0].observed_ms, 1000);
  assert.equal(results[0].confirmed_ms, 2000);
});
test('only one sample can be active and timeout terminates it exactly once', () => {
  const { tracker: t, results } = fixture();
  quiet(t);
  const id = t.startSpeech(400, true);
  assert.equal(t.startSpeech(450, true), null);
  assert.equal(t.snapshot().sample_id, id);
  t.tick(30400);
  t.tick(30500);
  assert.equal(results.length, 1);
  assert.equal(results[0].reason, 'timeout');
});
test('hidden page, disconnected session and microphone loss cannot silently discard an active sample', () => {
  for (const reason of [
    'page_hidden',
    'connection_lost',
    'track_ended',
    'autoplay_failed',
    'session_closed',
    'mic_stopped',
  ]) {
    const { tracker: t, results } = fixture();
    quiet(t);
    t.startSpeech(400, true);
    t.invalidate(reason, 500);
    assert.equal(results[0].reason, reason);
    assert.equal(t.snapshot(), null);
  }
});
test('arming requires actual fresh quiet/active remote observations and an enabled microphone', () => {
  const { tracker: t } = fixture();
  assert.equal(t.startSpeech(0, true), null);
  quiet(t, 100);
  assert.equal(t.startSpeech(450, false), null);
  assert.equal(t.startInterrupt(500), null);
  assert.equal(t.startSpeech(900, true), null);
});
test('numeric round zero is valid and late old-round messages do not overlap a newer round', () => {
  const { tracker: t, results } = fixture();
  round(t, 'old', 0);
  t.round({ source: 'conv', round_id: 'old', stage: 5 }, 50);
  quiet(t, 100);
  t.startSpeech(450, true);
  t.local(80, 500);
  round(t, 0, 600);
  round(t, 'old', 650);
  for (let at = 700; at <= 1200; at += 100) t.local(0, at);
  t.remote(80, 1300);
  t.remote(80, 1400);
  assert.equal(results[0].state, 'completed');
  assert.equal(results[0].round_id, '0');
});
test('a pending previous round blocks arming despite instantaneous silence, then waits for terminal and sustained quiet', () => {
  const { tracker: t } = fixture();
  for (let at = 100; at <= 1200; at += 100) {
    t.local(0, at);
    t.remote(0, at);
  }
  round(t, 'previous', 1250);
  assert.equal(t.speechWaitReason(1250, true), 'processing_previous_round');
  assert.equal(t.startSpeech(1250, true), null);
  t.remote(90, 1300);
  t.remote(90, 1400);
  t.round({ source: 'conv', round_id: 'previous', stage: 5 }, 1500);
  assert.equal(t.speechWaitReason(1500, true), 'remote_not_silent');
  for (let at = 1600; at <= 2600; at += 100) {
    t.local(0, at);
    t.remote(0, at);
  }
  assert.equal(t.speechWaitReason(2600, true), null);
  assert(t.startSpeech(2600, true));
});
