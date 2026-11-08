import { useState } from 'react';
import { Button, Message } from '@arco-design/web-react';
import { IconExport, IconUser } from '@arco-design/web-react/icon';
import { useAuth } from '@/auth/AuthProvider';
import { useLeave } from '@/lib/useCommon';
import styles from './index.module.less';

export default function AccountMenu() {
  const { user, logout } = useAuth();
  const leave = useLeave();
  const [busy, setBusy] = useState(false);
  const signOut = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await leave();
      await logout();
    } catch (error) {
      Message.error(error instanceof Error ? error.message : '退出失败，请重试。');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={styles.account}>
      <span className={styles.accountName} title={user?.username}>
        <IconUser /> {user?.username}
      </span>
      <Button
        type="text"
        size="small"
        icon={<IconExport />}
        loading={busy}
        onClick={signOut}
        aria-label="退出登录"
      >
        退出
      </Button>
    </div>
  );
}
