/* Pure SDK-observation state machine. All times are supplied on one monotonic clock. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.M0Measurements = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const REASONS = Object.freeze([
    'remote_not_silent',
    'mic_not_started',
    'old_audio',
    'resumed_speech',
    'overlapping_rounds',
    'missing_round',
    'command_failed',
    'timeout',
    'autoplay_failed',
    'track_ended',
    'mic_stopped',
    'session_closed',
    'page_hidden',
    'connection_lost',
    'cancelled',
    'clock_regressed',
  ]);
  const stamp = (at) => {
    if (!Number.isFinite(at) || at < 0) throw new TypeError('nonnegative_monotonic_time_required');
    return Math.round(at);
  };
  const validRound = (id) => typeof id === 'string' && /^[a-zA-Z0-9_.:@-]{1,128}$/.test(id);

  class Tracker {
    constructor(options = {}) {
      this.onResult = options.onResult || (() => {});
      this.onStart = options.onStart || (() => {});
      this.onChange = options.onChange || (() => {});
      this.idFactory =
        options.idFactory ||
        (() => 'm0_sample_' + globalThis.crypto.randomUUID().replaceAll('-', ''));
      this.userId = options.userId;
      this.agentId = options.agentId;
      this.active = null;
      this.history = [];
      this.rounds = new Set();
      this.pendingRounds = new Set();
      this.endedRounds = new Set();
      this.currentRound = null;
      this.remoteActive = null;
      this.remoteLastAt = null;
      this.remoteLowAt = null;
      this.remoteCandidate = null;
      this.remoteCount = 0;
      this.localLowAt = null;
      this.localLastAt = null;
      this.lastTime = 0;
    }
    _time(at) {
      at = stamp(at);
      if (at < this.lastTime) {
        if (this.active) this._finish('invalid', 'clock_regressed', this.lastTime);
        return null;
      }
      this.lastTime = at;
      if (this.active && at >= this.active.deadline) {
        this._finish(
          'invalid',
          this.active.evidenceReady && !this.active.round_id ? 'missing_round' : 'timeout',
          at
        );
      }
      return at;
    }
    snapshot() {
      const s = this.active;
      return s
        ? { sample_id: s.sample_id, kind: s.kind, phase: s.phase, round_id: s.round_id || null }
        : null;
    }
    _phase(phase) {
      if (this.active && this.active.phase !== phase) {
        this.active.phase = phase;
        this.onChange(this.snapshot());
      }
    }
    _begin(kind, at) {
      const id = this.idFactory();
      if (!/^m0_sample_[a-f0-9]{32}$/.test(id)) throw new TypeError('invalid_sample_id');
      this.active = {
        sample_id: id,
        kind,
        started_ms: at,
        deadline: at + (kind === 'speech_first_audio' ? 30000 : 10000),
        baseline: new Set(this.rounds),
        phase: '',
        committed: false,
        firstSpeechAt: null,
        sealed: false,
        highAt: null,
        highCount: 0,
        lowAt: null,
        lowCount: 0,
        evidenceReady: false,
      };
      this._phase(kind === 'speech_first_audio' ? 'waiting_speech' : 'waiting_command');
      this.onStart({ sample_id: id, kind, started_ms: at });
      return id;
    }
    remoteSilentFor(at) {
      at = stamp(at);
      return this.remoteActive === false &&
        this.remoteLowAt !== null &&
        at - this.remoteLastAt <= 350
        ? Math.max(0, at - this.remoteLowAt)
        : 0;
    }
    localSilentFor(at) {
      at = stamp(at);
      return this.localLowAt !== null && at - this.localLastAt <= 350
        ? Math.max(0, at - this.localLowAt)
        : 0;
    }
    canSpeech(at, micEnabled) {
      return (
        !this.active && micEnabled && !this.pendingRounds.size && this.remoteSilentFor(at) >= 200
      );
    }
    speechWaitReason(at, micEnabled) {
      if (!micEnabled) return 'mic_not_started';
      if (this.active) return 'sample_active';
      if (this.pendingRounds.size) return 'processing_previous_round';
      if (this.remoteSilentFor(at) < 1000) return 'remote_not_silent';
      if (this.localSilentFor(at) < 500) return 'local_not_silent';
      return null;
    }
    canInterrupt(at) {
      return !this.active && this.remoteActive === true && stamp(at) - this.remoteLastAt <= 350;
    }
    startSpeech(at, micEnabled) {
      at = this._time(at);
      if (at === null || !this.canSpeech(at, micEnabled)) return null;
      return this._begin('speech_first_audio', at);
    }
    startInterrupt(at) {
      at = this._time(at);
      if (at === null || !this.canInterrupt(at)) return null;
      const id = this._begin('interrupt_silence', at);
      this.active.trigger_ms = at;
      if (this.currentRound) this.active.round_id = this.currentRound;
      return id;
    }
    commandResult(success, at) {
      at = this._time(at);
      const s = this.active;
      if (at === null || !s || s.kind !== 'interrupt_silence') return;
      if (!success) {
        this._finish('invalid', 'command_failed', at);
        return;
      }
      s.committed = true;
      this._phase('waiting_silence');
      this._complete(at);
    }
    local(volume, at) {
      at = this._time(at);
      if (at === null || !Number.isFinite(volume)) return;
      this.localLastAt = at;
      if (volume > 25) this.localLowAt = null;
      else if (this.localLowAt === null) this.localLowAt = at;
      const s = this.active;
      if (!s || s.kind !== 'speech_first_audio') return;
      if (volume > 25) {
        if (s.sealed) {
          this._finish('invalid', 'resumed_speech', at);
          return;
        }
        if (s.highAt !== null) {
          this._finish('invalid', 'old_audio', at);
          return;
        }
        if (s.firstSpeechAt === null) s.firstSpeechAt = at;
        s.trigger_ms = at;
        this._phase('waiting_end');
      } else if (
        s.firstSpeechAt !== null &&
        !s.sealed &&
        this.localLowAt !== null &&
        at - this.localLowAt >= 500
      ) {
        s.sealed = true;
        this._phase('waiting_audio');
        this._complete(at);
      }
    }
    remote(volume, at) {
      at = this._time(at);
      if (at === null || !Number.isFinite(volume)) return;
      this.remoteLastAt = at;
      const high = volume > 25;
      if (high) this.remoteLowAt = null;
      else if (this.remoteLowAt === null) this.remoteLowAt = at;
      if (high === this.remoteCandidate) this.remoteCount += 1;
      else {
        this.remoteCandidate = high;
        this.remoteCount = 1;
      }
      if (this.remoteCount >= (high ? 2 : 3)) this.remoteActive = high;
      const s = this.active;
      if (!s) return;
      if (s.kind === 'speech_first_audio') {
        if (high && s.firstSpeechAt === null) {
          this._finish('invalid', 'old_audio', at);
          return;
        }
        if (high) {
          if (s.highAt === null) s.highAt = at;
          s.highCount += 1;
          if (s.highCount >= 2) {
            s.observed_ms = s.highAt;
            s.evidenceReady = true;
            this._complete(at);
          }
        } else if (!s.evidenceReady) {
          s.highAt = null;
          s.highCount = 0;
        }
      } else if (high) {
        // A short gap inside speech is not a completed stop; restart the entire quiet window.
        s.lowAt = null;
        s.lowCount = 0;
        s.evidenceReady = false;
        delete s.observed_ms;
      } else {
        if (s.lowAt === null) s.lowAt = at;
        s.lowCount += 1;
        if (s.lowCount >= 3 && at - s.lowAt >= 1000) {
          s.observed_ms = s.lowAt;
          s.evidenceReady = true;
          this._complete(at);
        }
      }
    }
    round(info, at) {
      at = this._time(at);
      if (at === null || !info) return;
      const id = Number.isSafeInteger(info.round_id) ? String(info.round_id) : info.round_id;
      if (!validRound(id)) return;
      if (info.source === 'conv') {
        if ([4, 5].includes(info.stage)) {
          this.pendingRounds.delete(id);
          this.endedRounds.add(id);
          this.rounds.add(id);
          if (this.active?.kind === 'speech_first_audio') this.active.baseline.add(id);
          if (this.endedRounds.size > 128)
            this.endedRounds.delete(this.endedRounds.values().next().value);
          this.onChange(this.snapshot());
          return;
        }
        if (![2, 3].includes(info.stage)) return;
        if (!this.endedRounds.has(id)) this.pendingRounds.add(id);
      } else if (info.source === 'subv') {
        if (![this.userId, this.agentId].includes(info.user_id)) return;
      } else return;
      if (!this.rounds.has(id)) {
        this.rounds.add(id);
        this.currentRound = id;
        if (this.rounds.size > 128) this.rounds.delete(this.rounds.values().next().value);
      }
      const s = this.active;
      if (!s) return;
      if (s.kind === 'speech_first_audio') {
        if (s.firstSpeechAt === null) {
          s.baseline.add(id);
          return;
        }
        if (s.baseline.has(id)) return;
      } else if (s.baseline.has(id) && s.round_id && s.round_id !== id) {
        return;
      }
      if (s.round_id && s.round_id !== id) {
        this._finish('invalid', 'overlapping_rounds', at);
        return;
      }
      s.round_id = id;
      this._complete(at);
    }
    _complete(at) {
      const s = this.active;
      if (!s || !s.evidenceReady || (s.kind === 'interrupt_silence' && !s.committed)) return;
      if (s.kind === 'speech_first_audio' && !s.sealed) return;
      if (!s.round_id) {
        this._phase('waiting_round');
        return;
      }
      this._finish('completed', null, at);
    }
    tick(at) {
      at = this._time(at);
      const s = this.active;
      if (at !== null && s && at >= s.deadline) {
        this._finish('invalid', s.evidenceReady && !s.round_id ? 'missing_round' : 'timeout', at);
      }
    }
    invalidate(reason, at) {
      if (!REASONS.includes(reason)) throw new TypeError('unsupported_invalid_reason');
      at = this._time(at);
      if (at !== null && this.active) this._finish('invalid', reason, at);
    }
    _finish(state, reason, at) {
      const s = this.active;
      if (!s) return;
      const result = { sample_id: s.sample_id, kind: s.kind, state, started_ms: s.started_ms };
      for (const name of ['trigger_ms', 'observed_ms', 'round_id']) {
        if (s[name] !== undefined) result[name] = s[name];
      }
      result.confirmed_ms = Math.max(
        at,
        result.observed_ms || 0,
        result.trigger_ms || 0,
        result.started_ms
      );
      if (reason) result.reason = reason;
      this.active = null;
      this.history.push(Object.freeze(result));
      if (this.history.length > 20) this.history.shift();
      this.onResult(result);
      this.onChange(null);
    }
  }
  return Object.freeze({ create: (options) => new Tracker(options), Tracker, REASONS });
});
