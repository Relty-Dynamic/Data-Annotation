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
  useEffect(() => {
    authFetch('/api/auth/me', {cache:'no-store'}).then(async response => {
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
      const response = await authFetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username,password})});
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
  if (user) return <App user={user} onLogout={logout}/>;
  return <main className="auth-page"><form className="auth-card" onSubmit={login}>
    <h1>登录 DataMark</h1><p>使用管理员创建的账号进入标注平台。</p>
    <label>账号<input autoComplete="username" value={username} onChange={event=>setUsername(event.target.value)} required/></label>
    <label>密码<input type="password" autoComplete="current-password" value={password} onChange={event=>setPassword(event.target.value)} required/></label>
    {error&&<p role="alert" className="import-error">{error}</p>}
    <button className="primary wide" disabled={busy}>{busy?'正在登录…':'登录'}</button>
  </form></main>;
}
