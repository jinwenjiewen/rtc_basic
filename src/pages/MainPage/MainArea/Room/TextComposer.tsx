/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import { useEffect, useRef, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import { v4 as uuid } from 'uuid';
import { IconSend } from '@arco-design/web-react/icon';
import { RootState } from '@/store';
import { addTextMessage, removeTextMessage, updateShowSubtitle } from '@/store/slices/room';
import { useScene } from '@/lib/useCommon';
import RtcClient from '@/lib/RtcClient';
import { COMMAND, INTERRUPT_PRIORITY } from '@/utils/handler';
import styles from './TextComposer.module.less';

// RTC 的文本指令最多 200 字；预留一字，按协议为没有句末标点的输入补上句号。
const MAX_LENGTH = 199;

function TextComposer() {
  const dispatch = useDispatch();
  const { isJoined, isAIGCEnable, localUser } = useSelector((state: RootState) => state.room);
  const { botName } = useScene();
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const sendingRef = useRef(false);
  const mountedRef = useRef(true);
  const composingRef = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const ready = isJoined && isAIGCEnable && !!botName && !!localUser.userId;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const send = async () => {
    const question = text.trim();
    if (!question || question.length > MAX_LENGTH || !ready || sendingRef.current) return;

    if (!RtcClient.getAgentEnabled()) {
      setError('AI 尚未连接，请稍后再试。');
      return;
    }

    const id = uuid();
    const message = /[。！？.!?；;：:，,…][”’"'）)\]]*$/.test(question)
      ? question
      : `${question}。`;
    sendingRef.current = true;
    setSending(true);
    setError('');
    // 先显示用户输入，使紧接着到达的 AI 字幕始终追加在这个问题之后。
    dispatch(addTextMessage({ id, text: question, user: localUser.userId! }));
    dispatch(updateShowSubtitle({ isShowSubtitle: true }));

    try {
      await RtcClient.commandAgent({
        agentName: botName,
        command: COMMAND.EXTERNAL_TEXT_TO_LLM,
        interruptMode: INTERRUPT_PRIORITY.HIGH,
        message,
      });
      if (mountedRef.current) setText('');
    } catch {
      dispatch(removeTextMessage(id));
      if (mountedRef.current) setError('发送失败，请检查连接后重试，输入内容已保留。');
    } finally {
      sendingRef.current = false;
      if (mountedRef.current) {
        setSending(false);
        inputRef.current?.focus();
      }
    }
  };

  return (
    <form
      className={styles.composer}
      aria-label="文字对话"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
    >
      <div className={styles.inputRow}>
        <textarea
          ref={inputRef}
          aria-label="输入消息"
          aria-describedby="text-composer-hint"
          placeholder={ready ? '输入你想聊的内容，也可以继续语音交流…' : '正在连接 AI，请稍候…'}
          maxLength={MAX_LENGTH}
          rows={2}
          value={text}
          readOnly={sending}
          disabled={!ready}
          onChange={(event) => {
            setText(event.target.value);
            setError('');
          }}
          onCompositionStart={() => {
            composingRef.current = true;
          }}
          onCompositionEnd={() => {
            composingRef.current = false;
          }}
          onKeyDown={(event) => {
            if (
              event.key === 'Enter' &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing &&
              !composingRef.current &&
              event.keyCode !== 229
            ) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <button type="submit" disabled={!ready || !text.trim() || sending} aria-label="发送消息">
          <IconSend />
          <span>{sending ? '发送中' : '发送'}</span>
        </button>
      </div>
      <div className={styles.footer} id="text-composer-hint">
        <span className={error ? styles.error : ''} role={error ? 'alert' : 'status'}>
          {error || 'Enter 发送 · Shift + Enter 换行'}
        </span>
        <span>
          {text.length}/{MAX_LENGTH}
        </span>
      </div>
    </form>
  );
}

export default TextComposer;
