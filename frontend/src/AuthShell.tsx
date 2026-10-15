import TaskPanel from './tasks/TaskPanel';
import TaskAccounts from './tasks/TaskAccounts';
import {useEffect, useState, type FormEvent} from 'react';
import App from './App';
import {authFetch, setCsrfToken, type Account} from './auth';
import {useBrowserLifetime} from './useBrowserLifetime';

export default function AuthShell() {
  useBrowserLifetime();
  const [user, setUser] = useState<Account | null>(null);
  const [checking, setChecking] = useState(true);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [setupDetail, setSetupDetail] = useState('');
  const [tasksAvailable,setTasksAvailable]=useState(false),[tasksOpen,setTasksOpen]=useState(false),[accountsOpen,setAccountsOpen]=useState(false);
  useEffect(() => {
    authFetch('/api/health', {cache:'no-store'}).then(async response => {
      if (!response.ok) throw new Error('无法连接标注平台。');
      const health = await response.json();
      setTasksAvailable(health.capabilities?.includes('task-management')??false);
      if (health.mode === 'public-setup' && health.storage_ready === false) {
        setSetupDetail(health.detail || 'S3 尚未配置，暂不能上传或标注。');
      }
      return authFetch('/api/auth/me', {cache:'no-store'});
    }).then(async response => {
      if (response.ok) {
        const data = await response.json();
        setCsrfToken(data.csrf ?? '');
        setUser(data);
      }
    }).catch(() => setError('无法连接标注平台。')).finally(() => setChecking(false));
  }, []);
  async function login(event: FormEvent) {
    event.preventDefault();
    setBusy(true);setError('');
    try {
      const response = await authFetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:username.trim(),password})});
      if (!response.ok) { const data=await response.json(); throw new Error(data.detail ?? '登录失败'); }
      const data = await response.json();
      setCsrfToken(data.csrf ?? '');setPassword('');setUser(data.user);
    } catch (cause) {setError(cause instanceof Error?cause.message:'登录失败');}
    finally {setBusy(false);}
  }
  async function logout() {
    await authFetch('/api/auth/logout', {method:'POST'});
    setCsrfToken('');
    setUser(null);
  }
  if (checking) return <main className="auth-page"><section className="auth-card">正在检查登录状态…</section></main>;
  if (user && accountsOpen && tasksAvailable && user.role==='admin') return <TaskAccounts onClose={()=>setAccountsOpen(false)}/>;
  if (user && tasksOpen && tasksAvailable) return <TaskPanel user={user} onClose={()=>setTasksOpen(false)} onOpenTask={id=>window.location.assign('?task='+id)}/>;
  if (user && setupDetail && !new URLSearchParams(window.location.search).has('task')) return <main className="auth-page"><section className="auth-card">
    <h1>已登录 DataMark</h1><p>{user.display_name} · {user.username}</p>
    <p>此账号与内网 NAS 使用同一个账号库。</p><p>{setupDetail}</p>{tasksAvailable&&<button className="primary" onClick={()=>setTasksOpen(true)}>任务管理</button>}
    {tasksAvailable&&user.role==='admin'&&<button className="primary" onClick={()=>setAccountsOpen(true)}>账号管理</button>}
    <button className="primary wide" onClick={logout}>退出登录</button>
  </section></main>;
  if (user) return <App user={user} onLogout={logout}/>;
  return <main className="auth-page"><form className="auth-card" onSubmit={login}>
    <h1>登录 DataMark</h1><p>使用管理员创建的账号进入标注平台。</p>
    <label>{tasksAvailable?'账号（邮箱）':'账号'}<input maxLength={254} autoComplete="username" value={username} onChange={event=>setUsername(event.target.value)} required/></label>
    <label>密码<input type="password" autoComplete="current-password" value={password} onChange={event=>setPassword(event.target.value)} required/></label>
    {error&&<p role="alert" className="import-error">{error}</p>}
    <button className="primary wide" disabled={busy}>{busy?'正在登录…':'登录'}</button>
  </form></main>;
}
