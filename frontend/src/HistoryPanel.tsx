import {formatOperationTime} from './operationTime';
import {useEffect, useState} from 'react';
import {authFetch} from './auth';

type Event = {id:string; revision:number; axis:string; segment_id:string; action:string; actor_name:string|null; recorded_at:string; before:{label?:string;lane?:number}|null; after:{label?:string;lane?:number}|null};
type Operation = {id:string; revision:number; action:string; actor_name:string|null; recorded_at:string; before: {selected_tracks?:string[]; name?:string}|null; after: {selected_tracks?:string[]; name?:string}};
const axes:Record<string,string>={scene:'场景',posture:'姿势',category:'大类',habit:'习惯'};
const actions:Record<string,string>={create:'新增',update:'修改',delete:'删除'};
const laneLabel=(event:Event)=>{const lane=event.after?.lane??event.before?.lane;return lane===undefined?'':lane===0?' · 主轴':` · 子轴 ${lane}`;};

export default function HistoryPanel({projectId,onClose}:{projectId:string;onClose:()=>void}) {
  const [events,setEvents]=useState<Event[]>([]),[error,setError]=useState('');
  const [operations,setOperations]=useState<Operation[]>([]);
  useEffect(()=>{
    authFetch(`/api/projects/${projectId}/history`,{cache:'no-store'}).then(async response=>{
      if(!response.ok)throw new Error((await response.json()).detail??'读取记录失败');
      setEvents(await response.json());
    }).catch(cause=>setError(cause instanceof Error?cause.message:'读取记录失败'));
    authFetch(`/api/projects/${projectId}/operations`,{cache:'no-store'}).then(async response=>{
      if(!response.ok)throw new Error((await response.json()).detail??'读取项目操作失败');
      setOperations(await response.json());
    }).catch(cause=>setError(cause instanceof Error?cause.message:'读取项目操作失败'));
  },[projectId]);
  const rows=[...events.map(event=>({...event,description:`${actions[event.action]??event.action} ${axes[event.axis]??event.axis}「${event.after?.label??event.before?.label??''}」${laneLabel(event)}`})),...operations.map(event=>({...event,description:event.action==='project_create'?'创建项目':JSON.stringify(event.before?.selected_tracks)!==JSON.stringify(event.after.selected_tracks)?`选择时间轴：${event.after.selected_tracks?.map(axis=>axes[axis]??axis).join('、')||'全部隐藏'}`:event.before?.name!==event.after.name?`项目更名：${event.after.name}`:'更新项目配置或保存状态'}))].sort((a,b)=>b.recorded_at.localeCompare(a.recorded_at));
  return <div className="modal-backdrop"><section className="admin-panel" role="dialog" aria-modal="true" aria-label="项目操作记录">
    <div className="admin-header"><h2>项目操作记录</h2><button className="secondary" onClick={onClose}>关闭</button></div>
    <p>操作记录保存在服务所在电脑的本地数据库。此处显示最近的标注编辑与项目操作，取消勾选不删除标注或记录。</p>
    {error&&<p role="alert" className="import-error">{error}</p>}
    {!error&&rows.length===0&&<p>暂无操作记录。旧版本导入的标注没有可追溯的编辑者。</p>}
    <div className="admin-list">{rows.map(event=><div key={event.id}><span>{event.actor_name??'本机'} · {event.description}</span><small>{formatOperationTime(event.recorded_at)} · 版本 {event.revision}</small></div>)}</div>
  </section></div>;
}
