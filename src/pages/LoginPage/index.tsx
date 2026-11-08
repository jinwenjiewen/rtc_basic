import { useEffect, useRef, useState } from 'react';
import { Button, Checkbox, Form, Input } from '@arco-design/web-react';
import {
  IconArrowRight,
  IconExclamationCircle,
  IconLock,
  IconUser,
} from '@arco-design/web-react/icon';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '@/auth/AuthProvider';
import { AuthStatus } from '@/auth/ProtectedRoute';
import styles from './index.module.less';

interface LoginValues {
  username: string;
  password: string;
  remember: boolean;
}

export default function LoginPage() {
  const { user, loading, login, expired, error: serviceError } = useAuth();
  const location = useLocation();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef(false);
  const from = (location.state as { from?: string } | null)?.from;
  const destination =
    from?.startsWith('/') && !from.startsWith('//') && !from.startsWith('/login') ? from : '/';

  useEffect(() => {
    const previous = document.title;
    document.title = '登录 · 暗月 AI';
    return () => {
      document.title = previous;
    };
  }, []);

  const submit = async ({ username, password, remember }: LoginValues) => {
    if (pending.current) return;
    pending.current = true;
    setSubmitting(true);
    setError('');
    try {
      await login(username, password, remember);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '登录失败，请稍后再试。');
    } finally {
      pending.current = false;
      setSubmitting(false);
    }
  };

  if (loading) return <AuthStatus />;
  if (user) return <Navigate to={destination} replace />;

  return (
    <main className={styles.page}>
      <section className={styles.story} aria-label="暗月 AI 智能客服">
        <a href="/login" className={styles.brand} aria-label="暗月 AI 登录">
          <span className={styles.brandMark} aria-hidden="true" />
          <span>
            暗月<span className={styles.brandDivider}>/</span>
            <small>AI 智能客服</small>
          </span>
        </a>

        <div className={styles.storyContent}>
          <div className={styles.eyebrow}>
            <span /> REAL-TIME CONVERSATION
          </div>
          <h1>
            好对话，
            <br />
            从这里<span>开始。</span>
          </h1>
          <p className={styles.intro}>
            让声音连接彼此，让每个问题都有回应。
            <br />
            你的专属 AI 对话空间，已准备就绪。
          </p>

          <div className={styles.visual} aria-hidden="true">
            <div className={styles.orbit} />
            <div className={styles.moon} />
            <div className={styles.starOne} />
            <div className={styles.starTwo} />
            <div className={styles.voiceCard}>
              <div className={styles.voiceAvatar}>
                <span />
              </div>
              <div>
                <strong>专属课程顾问</strong>
                <p>你好，今天想了解什么？</p>
              </div>
              <div className={styles.wave}>
                {[12, 22, 32, 18, 28, 36, 22, 12].map((height, index) => (
                  <i key={height + index} style={{ height, animationDelay: `${index * 0.12}s` }} />
                ))}
              </div>
            </div>
          </div>
        </div>

        <div className={styles.storyFooter}>
          <span className={styles.footerLine} />
          <span>实时语音</span>
          <b>·</b>
          <span>自然交流</span>
          <b>·</b>
          <span>随时回应</span>
        </div>
      </section>

      <section className={styles.loginPanel} aria-labelledby="login-title">
        <div className={styles.panelTop}>
          <span className={styles.miniMoon} /> 你的对话，从此刻开始
        </div>
        <div className={styles.formContent}>
          <div className={styles.formEyebrow}>WELCOME BACK</div>
          <h2 id="login-title">欢迎回来</h2>
          <p className={styles.formIntro}>登录账号，开启你的下一场对话。</p>

          {(error || serviceError || expired) && (
            <div className={styles.error} role="alert">
              <IconExclamationCircle />
              <span>{error || serviceError || '登录已过期，请重新登录。'}</span>
            </div>
          )}

          <Form<LoginValues>
            layout="vertical"
            size="large"
            requiredSymbol={false}
            initialValues={{ username: '', password: '', remember: false }}
            onSubmit={submit}
            onChange={() => error && setError('')}
            disabled={submitting}
            className={styles.form}
          >
            <Form.Item
              label="账号"
              field="username"
              rules={[
                { required: true, message: '请输入账号' },
                {
                  match: /^[a-zA-Z0-9_.-]{3,32}$/,
                  message: '账号为 3–32 位字母、数字、下划线、短横线或点',
                },
              ]}
            >
              <Input
                prefix={<IconUser />}
                placeholder="请输入你的账号"
                autoComplete="username"
                name="username"
                aria-label="账号"
                maxLength={32}
                autoCapitalize="none"
                spellCheck={false}
              />
            </Form.Item>
            <Form.Item
              label="密码"
              field="password"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password
                prefix={<IconLock />}
                placeholder="请输入你的密码"
                autoComplete="current-password"
                name="password"
                aria-label="密码"
                maxLength={128}
              />
            </Form.Item>
            <div className={styles.formOptions}>
              <Form.Item field="remember" triggerPropName="checked" noStyle>
                <Checkbox>保持登录 7 天</Checkbox>
              </Form.Item>
              <span>仅在个人设备上使用</span>
            </div>
            <Button
              type="primary"
              htmlType="submit"
              long
              loading={submitting}
              className={styles.submit}
            >
              {submitting ? '正在登录' : '登录并进入'} {!submitting && <IconArrowRight />}
            </Button>
          </Form>

          <div className={styles.help}>
            <span />
            需要开通账号或重置密码？请联系管理员。
          </div>
        </div>
        <footer className={styles.panelFooter}>
          <span>暗月 AI</span>
          <span>让知识触手可及</span>
        </footer>
      </section>
    </main>
  );
}
