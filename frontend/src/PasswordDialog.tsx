import {useState, type FormEvent} from 'react';
import {authFetch} from './auth';

export default function PasswordDialog({onClose,onChanged}:{onClose:()=>void;onChanged:()=>void}) {
  const [current,setCurrent]=useState(''),[password,setPassword]=useState(''),[error,setError]=useState('');
  async function save(event:FormEvent) {
    event.preventDefault();setError('');
    const response=await authFetch('/api/auth/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password:current,password})});
    if(!response.ok){setError((await response.json()).detail??'修改密码失败');return;}
    onChanged();
  }
  return <div className="modal-backdrop"><form role="dialog" aria-modal="true" aria-label="修改密码" className="auth-card" onSubmit={event=>void save(event)}>
    <h2>修改密码</h2><label>当前密码<input type="password" value={current} onChange={event=>setCurrent(event.target.value)} autoComplete="current-password" required/></label>
    <label>新密码（纯数字至少 8 位，其他至少 12 位）<input type="password" value={password} onChange={event=>setPassword(event.target.value)} autoComplete="new-password" minLength={8} required/></label>
    {error&&<p className="import-error" role="alert">{error}</p>}
    <div className="password-actions"><button type="button" className="secondary" onClick={onClose}>取消</button><button className="primary">保存并重新登录</button></div>
  </form></div>;
}
