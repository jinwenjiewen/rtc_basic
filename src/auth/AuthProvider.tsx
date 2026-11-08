import {
  createContext,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  ApiError,
  AUTH_EXPIRED_EVENT,
  AuthUser,
  fetchCurrentUser,
  loginWithPassword,
  logoutSession,
} from './api';

interface AuthState {
  user: AuthUser | null;
  loading: boolean;
  error: string;
  expired: boolean;
  retry: () => Promise<void>;
  login: (username: string, password: string, remember: boolean) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expired, setExpired] = useState(false);
  const channel = useRef<BroadcastChannel | null>(null);
  const hasAuthenticated = useRef(false);
  const generation = useRef(0);

  const checkSession = useCallback(async () => {
    const started = generation.current;
    try {
      const account = await fetchCurrentUser();
      if (started !== generation.current) return;
      hasAuthenticated.current = true;
      setUser(account);
      setError('');
    } catch (failure) {
      if (started !== generation.current) return;
      if (failure instanceof ApiError && failure.status === 401) {
        setUser(null);
        setError('');
      } else {
        setError(failure instanceof Error ? failure.message : '暂时无法确认登录状态。');
      }
    } finally {
      setLoading(false);
    }
  }, []);

  const retry = useCallback(async () => {
    setLoading(true);
    await checkSession();
  }, [checkSession]);

  useEffect(() => {
    checkSession();
    const expire = () => {
      generation.current += 1;
      setUser(null);
      setExpired(hasAuthenticated.current);
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, expire);
    if (typeof BroadcastChannel !== 'undefined') {
      const updates = new BroadcastChannel('rtc-auth');
      channel.current = updates;
      updates.onmessage = ({ data }) => {
        if (data === 'logout') expire();
        else if (data === 'login') checkSession();
      };
    }
    return () => {
      window.removeEventListener(AUTH_EXPIRED_EVENT, expire);
      channel.current?.close();
      channel.current = null;
    };
  }, [checkSession]);

  useEffect(() => {
    if (!user) return undefined;
    const checkVisible = () => {
      if (document.visibilityState === 'visible') checkSession();
    };
    const timer = window.setInterval(checkVisible, 60_000);
    window.addEventListener('focus', checkVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', checkVisible);
    };
  }, [user, checkSession]);

  const login = useCallback(async (username: string, password: string, remember: boolean) => {
    generation.current += 1;
    const account = await loginWithPassword(username, password, remember);
    hasAuthenticated.current = true;
    setUser(account);
    setExpired(false);
    setError('');
    channel.current?.postMessage('login');
  }, []);

  const logout = useCallback(async () => {
    await logoutSession();
    generation.current += 1;
    hasAuthenticated.current = false;
    setUser(null);
    setExpired(false);
    setError('');
    channel.current?.postMessage('logout');
  }, []);

  const value = useMemo(
    () => ({ user, loading, error, expired, retry, login, logout }),
    [user, loading, error, expired, retry, login, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error('useAuth requires AuthProvider');
  return auth;
}
