import {useEffect, useState} from 'react';
import {authFetch} from './auth';

type Event = {id:string; revision:number; axis:string; segment_id:string; action:string; actor_name:string|null; recorded_at:string; before:{label?:string}|null; after:{label?:string}|null};
const axes:Record<string,string>={scene:'场景',posture:'姿势',category:'大类',habit:'习惯'};
const actions:Record<string,string>={create:'新增',update:'修改',delete:'删除'};

export default function HistoryPanel({projectId,onClose}:{projectId:string;onClose:()=>void}) {
  const [events,setEvents]=useState<Event[]>([]),[error,setError]=useState('');
  useEffect(()=>{
    authFetch(`/api/projects/${projectId}/history`,{cache:'no-store'}).then(async response=>{
      if(!response.ok)throw new Error((await response.json()).detail??'读取记录失败');
      setEvents(await response.json());
    }).catch(cause=>setError(cause instanceof Error?cause.message:'读取记录失败'));
  },[projectId]);
  return <div className="modal-backdrop"><section className="admin-panel" role="dialog" aria-modal="true" aria-label="标注编辑历史">
    <div className="admin-header"><h2>标注编辑历史</h2><button className="secondary" onClick={onClose}>关闭</button></div>
    {error&&<p role="alert" className="import-error">{error}</p>}
    {!error&&events.length===0&&<p>暂无编辑记录。旧版本导入的标注没有可追溯的编辑者。</p>}
    <div className="admin-list">{events.map(event=><div key={event.id}><span>{event.actor_name??'未知'} · {actions[event.action]??event.action} {axes[event.axis]??event.axis}「{event.after?.label??event.before?.label??''}」</span><small>{new Date(event.recorded_at).toLocaleString()} · 版本 {event.revision}</small></div>)}</div>
  </section></div>;
}
