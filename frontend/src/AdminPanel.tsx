import {useEffect, useState, type FormEvent} from 'react';
import {authFetch} from './auth';
import type {Project} from './domain';

type ManagedUser = {id:string; username:string; display_name:string; email:string|null; role:string; active:number; can_upload:boolean};
type LocalSegment = {start_ms:number; end_ms:number|null};
type LocalReport = {id:string; name:string; owner_name:string; revision:number; updated_at:string; submitted_at?:string|null; has_documents:boolean; nas_relative_path?:string|null; videos:{start_ms:number;end_ms:number}[]; annotations:Record<string, LocalSegment[]>};

function coveredPercent(report:LocalReport, axis:string):number {
  const duration=report.videos.reduce((total,video)=>total+video.end_ms-video.start_ms,0);
  if(!duration)return 0;
  const covered=report.videos.reduce((total,video)=>total+(report.annotations[axis]??[]).reduce((sum,segment)=>sum+Math.max(0,Math.min(video.end_ms,segment.end_ms??video.end_ms)-Math.max(video.start_ms,segment.start_ms)),0),0);
  return Math.min(100,Math.round(covered/duration*100));
}

export default function AdminPanel({projects,mockMode=false,onClose}:{projects:Project[];mockMode?:boolean;onClose:()=>void}) {
  const [users,setUsers]=useState<ManagedUser[]>([]);
  const [localReports,setLocalReports]=useState<LocalReport[]>([]);
  const [assignments,setAssignments]=useState<Record<string,string>>({});
  const [username,setUsername]=useState(''),[displayName,setDisplayName]=useState(''),[email,setEmail]=useState(''),[password,setPassword]=useState('');
  const [resetId,setResetId]=useState(''),[resetPassword,setResetPassword]=useState('');
  const [error,setError]=useState(''),[notice,setNotice]=useState('');
  async function refresh() {
    const response=await authFetch('/api/users');
    if(!response.ok)throw new Error('读取账号失败');
    setUsers(await response.json());
    const pairs=await Promise.all(projects.map(async project=>{
      const result=await authFetch(`/api/projects/${project.id}/assignment`);
      return [project.id,(await result.json()).user_id??''] as const;
    }));
    setAssignments(Object.fromEntries(pairs));
    if(!mockMode){
      const local=await authFetch('/api/local-reports');
      if(!local.ok)throw new Error('读取本机项目总览失败');
      setLocalReports(await local.json());
    }
  }
  useEffect(()=>{void refresh().catch(cause=>setError(String(cause)));},[]);
  async function create(event:FormEvent) {
    event.preventDefault();setError('');setNotice('');
    const response=await authFetch('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,display_name:displayName,email:email.trim()||null,password})});
    if(!response.ok){setError((await response.json()).detail??'创建账号失败');return;}
    setUsername('');setDisplayName('');setEmail('');setPassword('');setNotice('账号已创建。');await refresh();
  }
  async function assign(projectId:string,userId:string) {
    setError('');
    const response=await authFetch(`/api/projects/${projectId}/assignment`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:userId||null})});
    if(!response.ok){setError((await response.json()).detail??'分配失败');return;}
    setAssignments(previous=>({...previous,[projectId]:userId}));setNotice('项目分配已保存。');
  }
  async function toggle(user:ManagedUser) {
    const response=await authFetch(`/api/users/${user.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:!user.active})});
    if(!response.ok){setError((await response.json()).detail??'更新账号失败');return;}
    await refresh();
  }
  async function setUploadPermission(user:ManagedUser) {
    setError('');setNotice('');
    const response=await authFetch(`/api/users/${user.id}/upload-permission`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({can_upload:!user.can_upload})});
    if(!response.ok){setError((await response.json()).detail??'更新上传权限失败');return;}
    await refresh();setNotice(`${user.display_name} 的上传权限已${user.can_upload?'关闭':'开放'}。`);
  }
  async function reset(event:FormEvent) {
    event.preventDefault();setError('');
    const response=await authFetch(`/api/users/${resetId}/password`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:resetPassword})});
    if(!response.ok){setError((await response.json()).detail??'重置密码失败');return;}
    setResetId('');setResetPassword('');setNotice('密码已重置，该账号原有登录已失效。');
  }
  async function downloadReport(report:LocalReport) {
    const response=await authFetch(`/api/local-reports/${report.id}/export`);
    if(!response.ok){setError((await response.json()).detail??'下载本机项目结果失败');return;}
    const url=URL.createObjectURL(await response.blob());
    const link=document.createElement('a');link.href=url;link.download=`${report.name.replace(/[<>:"/\\|?*]/g,'_')}-timelines.zip`;link.click();
    setTimeout(()=>URL.revokeObjectURL(url),10000);
  }
  return <div className="modal-backdrop"><section role="dialog" aria-modal="true" aria-label="账号与项目分配" className="admin-panel">
    <div className="admin-header"><h2>账号与项目分配</h2><button className="secondary" onClick={onClose}>关闭</button></div>
    {error&&<p role="alert" className="import-error">{error}</p>}{notice&&<p role="status">{notice}</p>}
    <h3>新建标注账号</h3><form onSubmit={event=>void create(event)} className="admin-form">
      <label>账号<input value={username} onChange={event=>setUsername(event.target.value)} autoComplete="off" required/></label>
      <label>标注人姓名<input value={displayName} onChange={event=>setDisplayName(event.target.value)} required/></label>
      <label>邮箱（选填）<input type="email" value={email} onChange={event=>setEmail(event.target.value)} autoComplete="off"/></label>
      <label>初始密码（纯数字至少 8 位，其他至少 12 位）<input type="password" value={password} onChange={event=>setPassword(event.target.value)} minLength={8} autoComplete="new-password" required/></label>
      <button className="primary">创建账号</button>
    </form>
    <h3>账号</h3><p>所有有效账号均可标注已领取的项目；视频上传权限由管理员单独开放或关闭。</p><div className="admin-list">{users.map(user=><div key={user.id}><span>{user.display_name} · {user.username}{user.email?` · ${user.email}`:''} · {user.role==='admin'?'管理员':'标注人'} · 上传{user.can_upload?'已开放':'未开放'}</span><span className="admin-buttons">{user.role==='annotator'&&<button className="secondary" onClick={()=>void setUploadPermission(user)}>{user.can_upload?'禁用上传':'开放上传'}</button>}<button className="secondary" onClick={()=>setResetId(user.id)}>重置密码</button><button className="secondary" onClick={()=>void toggle(user)}>{user.active?'停用':'启用'}</button></span></div>)}</div>
    {resetId&&<form className="admin-reset" onSubmit={event=>void reset(event)}><strong>重置 {users.find(user=>user.id===resetId)?.display_name} 的密码（纯数字至少 8 位，其他至少 12 位）</strong><input type="password" autoComplete="new-password" minLength={8} value={resetPassword} onChange={event=>setResetPassword(event.target.value)} required/><button type="button" className="secondary" onClick={()=>{setResetId('');setResetPassword('');}}>取消</button><button className="primary">保存</button></form>}
    <h3>{mockMode?'模拟项目领取人':'NAS 项目领取人'}</h3><div className="admin-list">{projects.map(project=><label key={project.id}><span>{project.name}</span><select value={assignments[project.id]??''} onChange={event=>void assign(project.id,event.target.value)}><option value="">未领取</option>{users.filter(user=>user.role==='annotator'&&user.active).map(user=><option key={user.id} value={user.id}>{user.display_name} · {user.username}</option>)}</select></label>)}</div>
    {!mockMode&&<><h3>标注员本机项目</h3><div className="admin-list">{localReports.length?localReports.map(report=><div key={report.id}><span>{report.name} · {report.owner_name} · 姿势覆盖 {coveredPercent(report,'posture')}% · 场景覆盖 {coveredPercent(report,'scene')}% · {Object.values(report.annotations).reduce((total,items)=>total+items.length,0)} 条标注 · {report.has_documents?'已写回 NAS':'标注中'}{report.nas_relative_path?` · NAS：homes/datacollection/${report.nas_relative_path}`:''} · 更新于 {report.updated_at}</span>{report.has_documents&&<button className="secondary" onClick={()=>void downloadReport(report)}>下载时间轴 ZIP</button>}</div>):<p>暂无已同步的本机项目。</p>}</div></>}
  </section></div>;
}
