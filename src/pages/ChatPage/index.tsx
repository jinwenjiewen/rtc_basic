import { FormEvent, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { v4 as uuid } from 'uuid';
import Header from '@/components/Header';
import { agentRequest, AgentSession, createTextSession, sessionAction, streamText } from '@/agent/api';
import styles from './index.module.less';

interface Line { id: string; role: 'user' | 'assistant'; text: string }
interface Menu { default_profile: string; profiles: { id: string; name: string; configured: boolean }[] }

export default function ChatPage() {
  const [menu, setMenu] = useState<Menu>();
  const [profile, setProfile] = useState('');
  const [session, setSession] = useState<AgentSession>();
  const [lines, setLines] = useState<Line[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const lease = useRef<AgentSession>();
  const current = useRef<{ round: string; abort: AbortController }>();
  const mounted = useRef(false);
  const generation = useRef(0);
  const sending = useRef(false);

  useEffect(() => {
    mounted.current = true;
    agentRequest<Menu>('models').then((result) => {
      if (mounted.current) { setMenu(result); setProfile(result.default_profile); }
    }).catch((error) => { if (mounted.current) setNotice(error.message); });
    const endOnPageHide = () => {
      current.current?.abort.abort();
      if (lease.current) sessionAction('text', lease.current.session_id, 'stop').catch(() => {});
    };
    window.addEventListener('pagehide', endOnPageHide);
    const timer = window.setInterval(() => {
      const item = lease.current;
      if (item) sessionAction('text', item.session_id, 'heartbeat').catch((error) => {
        if (lease.current !== item) return;
        generation.current += 1;
        current.current?.abort.abort();
        lease.current = undefined;
        sending.current = false;
        if (mounted.current) { setSession(undefined); setBusy(false); setNotice(error.message); }
      });
    }, 20_000);
    return () => {
      mounted.current = false;
      generation.current += 1;
      window.clearInterval(timer);
      window.removeEventListener('pagehide', endOnPageHide);
      endOnPageHide();
    };
  }, []);

  const end = async () => {
    generation.current += 1;
    current.current?.abort.abort();
    current.current = undefined;
    const item = lease.current;
    lease.current = undefined;
    setSession(undefined);
    sending.current = false;
    setBusy(false);
    if (item) await sessionAction('text', item.session_id, 'stop');
  };

  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (sending.current || !input.trim() || !profile) return;
    sending.current = true;
    setBusy(true);
    setNotice('');
    const started = generation.current;
    const value = input.trim();
    const round = uuid();
    const abort = new AbortController();
    current.current = { round, abort };
    try {
      let item = lease.current;
      if (!item) {
        item = await createTextSession(profile);
        if (!mounted.current || started !== generation.current) {
          await sessionAction('text', item.session_id, 'stop');
          return;
        }
        lease.current = item;
        setSession(item);
      }
      setInput('');
      setLines((old) => [...old, { id: round, role: 'user', text: value }, { id: `${round}-reply`, role: 'assistant', text: '' }]);
      await streamText(item.session_id, round, value, abort.signal, (type, payload) => {
        if (!mounted.current || started !== generation.current) return;
        if (type === 'delta' && typeof payload.text === 'string') {
          setLines((old) => old.map((line) => line.id === `${round}-reply` ? { ...line, text: line.text + payload.text } : line));
        } else if (type === 'terminal' && payload.state !== 'generation_completed') {
          setNotice(payload.state === 'cancelled' ? '本轮已停止。' : '本轮回复未完成，请重试。');
        }
      });
    } catch (error) {
      if (mounted.current && started === generation.current && !abort.signal.aborted) {
        setNotice(error instanceof Error ? error.message : '发送失败，请重试。');
      }
    } finally {
      if (started === generation.current) {
        current.current = undefined;
        sending.current = false;
        if (mounted.current) setBusy(false);
      }
    }
  };

  const interrupt = async () => {
    const request = current.current;
    const item = lease.current;
    if (!request) return;
    if (!item) {
      generation.current += 1;
      request.abort.abort();
      current.current = undefined;
      sending.current = false;
      setBusy(false);
      setNotice('本轮已停止。');
      return;
    }
    try {
      await sessionAction('text', item.session_id, 'interrupt', { round_id: request.round });
      request.abort.abort();
      setNotice('本轮已停止。');
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '停止失败，请重试。');
    }
  };

  return <div className={styles.page}>
    <Header />
    <main className={styles.chat}>
      <div className={styles.toolbar}>
        <div><h1>文字对话</h1><p>了解课程，确认你的学习和报名意向。</p></div>
        <label>对话模型 <select aria-label="对话模型" value={profile} disabled={!!session || busy} onChange={(e) => setProfile(e.target.value)}>
          {menu?.profiles.filter((p) => p.configured).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select></label>
        <button type="button" onClick={() => end().then(() => { setLines([]); setNotice(''); }).catch((e) => setNotice(e.message))}>新对话</button>
        <Link to="/">语音对话</Link>
      </div>
      <div className={styles.messages} role="log" aria-label="对话记录" aria-live="polite">
        {!lines.length && <p className={styles.empty}>可以问：课程适合什么人？我该怎么选择课程？</p>}
        {lines.map((line) => <article key={line.id} className={line.role === 'user' ? styles.user : styles.assistant}>
          <strong>{line.role === 'user' ? '你' : '助手'}</strong><p>{line.text || (busy ? '正在回复…' : '本轮暂无回复。')}</p>
        </article>)}
      </div>
      <p className={styles.notice} role="status">{notice}</p>
      <form className={styles.composer} onSubmit={send}>
        <textarea aria-label="消息" placeholder="输入你想了解的问题" value={input} maxLength={2000} onChange={(e) => setInput(e.target.value)} />
        {busy ? <button type="button" onClick={interrupt}>停止回复</button> : <button type="submit" disabled={!menu || !input.trim()}>发送</button>}
      </form>
    </main>
  </div>;
}
