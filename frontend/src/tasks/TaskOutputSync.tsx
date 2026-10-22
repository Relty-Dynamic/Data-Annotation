import {useEffect,useState} from 'react';
import {authFetch} from '../auth';
export type NasOutputStatus={state:'ready'|'pending'|'running'|'error';detail:string;ready:number;total:number;retryable:boolean;retry_scheduled:boolean};
export type TaskOutputStatus={state:'ready'|'pending'|'error';detail:string;ready?:number;total?:number;nas?:NasOutputStatus|null};
export default function TaskOutputSync({taskId,status}:{taskId:string;status?:TaskOutputStatus|null}){
 const [current,setCurrent]=useState(status),[busy,setBusy]=useState(false),[error,setError]=useState('');
 useEffect(()=>{setCurrent(status);setError('');},[taskId,status]);
 useEffect(()=>{
  if(!current?.nas||busy||current.nas.state==='ready'||current.nas.state==='error'&&!current.nas.retry_scheduled)return;
  let alive=true,inFlight=false;
  const timer=setInterval(()=>{if(inFlight)return;inFlight=true;void authFetch(`/api/tasks/items/${encodeURIComponent(taskId)}/outputs/status`).then(async response=>{
   if(!response.ok)return;const value=await response.json();
   if(alive&&value&&['ready','pending','error'].includes(value.state)&&typeof value.detail==='string')setCurrent(value);
  }).catch(()=>{}).finally(()=>{inFlight=false;});},3000);
  return ()=>{alive=false;clearInterval(timer);};
 },[taskId,busy,current?.nas?.state,current?.nas?.retry_scheduled]);
 if(!current)return null;
 async function retry(){
  setBusy(true);setError('');
  try{
   const response=await authFetch(`/api/tasks/items/${encodeURIComponent(taskId)}/outputs/retry`,{method:'POST'});
   let body;
   try{body=JSON.parse(await response.text());}catch{throw new Error(`结果目录服务响应异常（HTTP ${response.status}），请稍后重试。`);}
   if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:'结果目录同步未确认成功。');
   if(!body||!['ready','pending','error'].includes(body.state)||typeof body.detail!=='string')throw new Error('结果目录同步状态无效，请刷新后重试。');
   setCurrent(body);
  }catch(cause){setError(cause instanceof Error?cause.message:'结果目录同步失败，请重试。');}
  finally{setBusy(false);}
 }
 return <section className={'task-output-sync '+current.state} aria-label="采集结果目录同步"><p role={current.state==='error'?'alert':'status'}>{busy?'正在同步采集目录并读回核验…':current.detail}</p>{current.nas&&<p role={current.nas.state==='error'?'alert':'status'}>NAS 同步：{current.nas.detail}</p>}{(current.state!=='ready'||current.nas?.state==='error'&&current.nas.retryable)&&<button type="button" className="secondary" disabled={busy} onClick={()=>void retry()}>{busy?'同步中…':current.state==='ready'?'重试 NAS 同步':'同步采集结果目录'}</button>}{error&&<p role="alert">{error}</p>}</section>;
}
