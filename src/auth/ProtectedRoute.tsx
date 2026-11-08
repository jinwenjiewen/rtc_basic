import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from './AuthProvider';
import styles from '@/pages/LoginPage/index.module.less';

export function AuthStatus() {
  const { loading, error, retry } = useAuth();
  return (
    <div className={styles.statusPage} role="status" aria-live="polite">
      <span className={styles.miniMoon} aria-hidden="true" />
      <h1>{loading ? '正在确认登录状态' : '暂时无法连接'}</h1>
      <p>{loading ? '稍等片刻，即将进入对话空间。' : error}</p>
      {!loading && (
        <button type="button" onClick={retry}>
          重新连接
        </button>
      )}
    </div>
  );
}

export default function ProtectedRoute() {
  const { user, loading, error } = useAuth();
  const location = useLocation();
  if (loading || (!user && error)) return <AuthStatus />;
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  return <Outlet />;
}
