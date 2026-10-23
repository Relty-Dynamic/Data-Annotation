import {useEffect, useState, type FormEvent} from 'react';
import {ChevronDown, Search, X} from 'lucide-react';
import {taskRequest} from './TaskPanel';
import {accountRole,accountRoleNames,type AccountRole} from '../accountRoles';
import type {TaskCapability} from './types';
import {filterAccounts, type AccountRoleFilter} from './accountFilters';
import './accounts.css';

type ManagedAccount = {id:string; username:string; email:string|null; display_name:string; role:string; active:boolean|number; can_upload:boolean; task_roles:TaskCapability[]};
const roleOptions:Array<[AccountRole,string]> = [['member','成员'],['leader','组长'],['admin','管理员']];
const initialPassword = '1234567890';
function roleName(account:ManagedAccount) {return accountRoleNames[accountRole(account)];}
function RoleChoices({role,onChange,disabled=false}:{role:AccountRole;onChange:(role:AccountRole)=>void;disabled?:boolean}) {
 return <div className="account-role-choices">{roleOptions.map(([value,label])=><label key={value} className={role===value?'selected':''}>
  <input type="radio" name="account-role" checked={role===value} disabled={disabled} onChange={()=>onChange(value)}/>{label}
 </label>)}</div>;
}
export default function TaskAccounts({onClose}:{onClose:()=>void}) {
  const [accounts,setAccounts]=useState<ManagedAccount[]>([]);
  const [nameSearch,setNameSearch]=useState(''), [roleFilter,setRoleFilter]=useState<AccountRoleFilter>('all');
  const visibleAccounts=filterAccounts(accounts,nameSearch,roleFilter);
  const [email,setEmail]=useState(''), [name,setName]=useState(''), [password,setPassword]=useState(initialPassword);
  const [busy,setBusy]=useState(false), [error,setError]=useState(''), [notice,setNotice]=useState('');
  const [resetId,setResetId]=useState(''), [resetPassword,setResetPassword]=useState(initialPassword);
  const [editingId,setEditingId]=useState(''), [editingRole,setEditingRole]=useState<AccountRole>('member');
  const editingAccount=accounts.find(account=>account.id===editingId);
  const [deletingId,setDeletingId]=useState('');
  const deletingAccount=accounts.find(account=>account.id===deletingId);
  async function refresh() {setAccounts(await taskRequest<ManagedAccount[]>('/accounts'));}
  useEffect(()=>{void refresh().catch(cause=>setError(cause instanceof Error?cause.message:'读取账号失败。'));},[]);
  useEffect(()=>{
    if(!editingId&&!deletingId)return;
    function escape(event:KeyboardEvent) {
      if(event.key==='Escape'&&!busy){event.preventDefault();event.stopPropagation();setEditingId('');setDeletingId('');document.getElementById(editingId?`account-role-${editingId}`:`account-delete-${deletingId}`)?.focus();}
    }
    window.addEventListener('keydown',escape,true);
    return ()=>window.removeEventListener('keydown',escape,true);
  },[editingId,deletingId,busy]);
  async function run(action:()=>Promise<void>) {
    setBusy(true);setError('');setNotice('');
    try {await action();await refresh();}
    catch(cause) {setError(cause instanceof Error?cause.message:'操作未成功。');await refresh().catch(()=>{});}
    finally {setBusy(false);}
  }
  function create(event:FormEvent) {
    event.preventDefault();void run(async()=>{
      await taskRequest('/accounts','POST',{email:email.trim(),display_name:name.trim(),role:'member',password});
      setEmail('');setName('');setPassword(initialPassword);
      setNotice('账号已创建，默认角色为成员。可使用邮箱和初始密码登录。');
    });
  }
  function closeRoles() {if(!busy){setEditingId('');document.getElementById(`account-role-${editingId}`)?.focus();}}
  return <div className="modal-backdrop account-backdrop">
    <section className="admin-panel account-panel" role="dialog" aria-modal="true" aria-label="账号管理" inert={!!editingId||!!deletingId}>
      <div className="admin-header"><h2>账号管理</h2><button className="secondary" onClick={onClose} disabled={busy}>关闭</button></div>
      {error&&<p className="account-message error" role="alert">{error}</p>}{notice&&<p className="account-message success" role="status">{notice}</p>}
      <h3>创建账号</h3>
      <form className="admin-form account-create-form" onSubmit={create}>
        <label>账号（邮箱）<input type="email" maxLength={254} value={email} onChange={event=>setEmail(event.target.value)} autoComplete="off" required disabled={busy}/></label>
        <label>标注人姓名<input value={name} maxLength={80} placeholder="例如李鑫" onChange={event=>setName(event.target.value)} required disabled={busy}/></label>
        <p className="account-role-hint">新账号默认角色：成员。创建后管理员可调整角色。</p>
        <label>初始密码<input type="text" value={password} minLength={8} maxLength={1024} autoComplete="off" onChange={event=>setPassword(event.target.value)} required disabled={busy}/></label>
        <div className="account-create-actions"><button className="primary" disabled={busy}>创建账号</button></div>
      </form>
      <h3>已创建账号</h3>
      <div className="account-filters">
        <label className="account-name-search"><span>按姓名搜索</span><div><Search size={15} aria-hidden="true"/><input type="search" value={nameSearch} placeholder="输入姓名或部分姓名" onChange={event=>setNameSearch(event.target.value)}/></div></label>
        <label>按角色筛选<select value={roleFilter} onChange={event=>setRoleFilter(event.target.value as AccountRoleFilter)}><option value="all">全部角色</option>{roleOptions.map(([role,label])=><option key={role} value={role}>{label}</option>)}</select></label>
        <button className="secondary" disabled={!nameSearch&&roleFilter==='all'} onClick={()=>{setNameSearch('');setRoleFilter('all');}}>重置</button>
        <span className="account-filter-count" role="status">显示 {visibleAccounts.length} / {accounts.length} 个账号</span>
      </div>
      <div className="account-table"><table><thead><tr><th>姓名</th><th>账号（邮箱）</th><th>角色</th><th>是否开放上传</th><th>操作</th></tr></thead><tbody>
        {visibleAccounts.map(account=><tr key={account.id}>
          <td>{account.display_name}{!account.active&&<span className="account-inactive">已停用</span>}</td>
          <td className="account-email">{account.email??account.username}</td>
          <td><button id={`account-role-${account.id}`} className="account-role-trigger" aria-label={`选择${account.display_name}的角色`} aria-haspopup="dialog" aria-expanded={editingId===account.id} disabled={busy} onClick={()=>{setEditingId(account.id);setEditingRole(accountRole(account));}}>{roleName(account)}<ChevronDown size={13}/></button></td>
          <td><select className={`account-upload-select ${account.can_upload?'enabled':''}`} aria-label={`${account.display_name}的上传权限`} value={account.can_upload?'enabled':'disabled'} disabled={busy||account.role==='admin'} onChange={event=>{const canUpload=event.target.value==='enabled';void run(async()=>{await taskRequest(`/accounts/${account.id}/upload`,'PUT',{can_upload:canUpload});setNotice('上传权限已更新。');});}}><option value="enabled">已开放</option><option value="disabled">未开放</option></select></td>
          <td><div className="account-row-actions">
            <button className="secondary" disabled={busy} onClick={()=>{setResetId(account.id);setResetPassword(initialPassword);}}>重置密码</button>
            <button className="secondary" disabled={busy} onClick={()=>void run(async()=>{await taskRequest(`/accounts/${account.id}/active`,'PUT',{active:!account.active});setNotice('账号状态已更新。');})}>{account.active?'停用':'启用'}</button>
            <button id={`account-delete-${account.id}`} className="secondary account-delete" disabled={busy} onClick={()=>{setError('');setDeletingId(account.id);}}>删除账号</button>
          </div></td>
        </tr>)}
        {!visibleAccounts.length&&<tr><td colSpan={5} className="account-empty">{accounts.length?'没有符合条件的账号':'暂无账号'}</td></tr>}
      </tbody></table></div>
      {resetId&&<form className="account-reset" onSubmit={event=>{event.preventDefault();void run(async()=>{await taskRequest(`/accounts/${resetId}/password`,'PUT',{password:resetPassword});setResetId('');setNotice('密码已重置，原登录已失效。');});}}>
        <h3>重置 {accounts.find(account=>account.id===resetId)?.display_name} 的密码</h3>
        <label>新密码<input type="text" minLength={8} value={resetPassword} autoComplete="off" onChange={event=>setResetPassword(event.target.value)} required disabled={busy}/></label>
        <div className="password-actions"><button type="button" className="secondary" onClick={()=>setResetId('')} disabled={busy}>取消</button><button className="primary" disabled={busy}>保存</button></div>
      </form>}
    </section>
    {deletingAccount&&<div className="modal-backdrop account-role-backdrop"><form className="admin-panel account-role-dialog" role="dialog" aria-modal="true" aria-label="删除账号确认" onSubmit={event=>{event.preventDefault();void run(async()=>{await taskRequest(`/accounts/${deletingAccount.id}`,'DELETE');setDeletingId('');setNotice('账号已删除，已有标注、任务和历史记录保留。');});}}>
      <h2>删除账号</h2><p className="account-delete-target">{deletingAccount.display_name}<br/>{deletingAccount.email??deletingAccount.username}</p><p className="account-role-hint">账号将移出列表并停止登录。已有标注、任务和操作记录保留，原邮箱账号仍保留用于追溯。</p>{error&&<p className="account-message error" role="alert">{error}</p>}<div className="password-actions"><button type="button" className="secondary" disabled={busy} autoFocus onClick={()=>setDeletingId('')}>取消</button><button className="secondary account-delete" disabled={busy}>确认删除</button></div>
    </form></div>}
    {editingAccount&&<div className="modal-backdrop account-role-backdrop" onClick={event=>{if(event.target===event.currentTarget)closeRoles();}}>
      <form className="admin-panel account-role-dialog" role="dialog" aria-modal="true" aria-label={`${editingAccount.display_name}的角色`} onSubmit={event=>{event.preventDefault();void run(async()=>{await taskRequest(`/accounts/${editingAccount.id}/role`,'PUT',{role:editingRole});setEditingId('');setNotice(`${editingAccount.display_name} 的角色已更新，原登录已失效。`);});}}>
        <div className="admin-header"><h2>{editingAccount.display_name}的角色</h2><button className="icon-button" type="button" aria-label="关闭角色选择" onClick={closeRoles} disabled={busy} autoFocus><X size={17}/></button></div>
        <RoleChoices role={editingRole} onChange={setEditingRole} disabled={busy}/>
        <p className="account-role-hint">选择一个角色。组长和管理员允许自我复核；成员不能自我复核。</p>
        {error&&<p className="account-message error" role="alert">{error}</p>}
        <div className="password-actions"><button type="button" className="secondary" disabled={busy} onClick={closeRoles}>取消</button><button className="primary" disabled={busy}>保存</button></div>
      </form>
    </div>}
  </div>;
}
