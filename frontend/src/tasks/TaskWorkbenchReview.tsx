import {useEffect,useRef,useState} from 'react';
import type {Account} from '../auth';
import {taskRequest} from './TaskPanel';
import TaskReview from './TaskReview';
import SuccessDialog from './SuccessDialog';
import {reviewStage} from './taskChoices';
import type {TaskItem,TaskRole,TaskVersion} from './types';
export default function TaskWorkbenchReview({taskId,user,time,onSeek,onPreview,onDirty,onBusy,selection}:{taskId:string;user:Account;time:number;onSeek:(time:number)=>void;onPreview:(version:TaskVersion)=>void;onDirty:(dirty:boolean)=>void;onBusy:(busy:boolean)=>void;selection?:{axis:string;start_ms:number;end_ms:number}}){
 const [task,setTask]=useState<TaskItem|null>(null),[roles,setRoles]=useState<TaskRole[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false),[versionId,setVersionId]=useState(''),[reset,setReset]=useState(0),[dirty,setDirty]=useState(false),[notice,setNotice]=useState(''),[success,setSuccess]=useState(''),[historyLoading,setHistoryLoading]=useState(false);
 const previewRef=useRef(onPreview);previewRef.current=onPreview;
 const actionPending=useRef(false);
 function markDirty(value:boolean){setDirty(value);onDirty(value);}
 useEffect(()=>{
  let alive=true;
  setTask(null);setError('');setSuccess('');setNotice('');
  void Promise.all([taskRequest<TaskItem>(`/items/${taskId}`),taskRequest<{roles:TaskRole[]}>('/me')]).then(([detail,me])=>{
   if(!alive)return;
   setTask(detail);setRoles(user.role==='admin'?['leader','reviewer','annotator']:me.roles);
   const version=detail.submissions?.[0];if(version){setVersionId(version.id);previewRef.current(version);}
  }).catch(cause=>alive&&setError(cause.message));
  return()=>{alive=false;onDirty(false);onBusy(false);};
 },[taskId]);
 function preview(version:TaskVersion){setVersionId(version.id);onPreview(version);}
 async function loadHistory(){
  if(historyLoading||task?.history)return;
  setHistoryLoading(true);setError('');
  try{const history=await taskRequest<TaskItem['history']>(`/items/${taskId}/history`);setTask(current=>current?.id===taskId?{...current,history}:current);}
  catch(cause){setError(cause instanceof Error?cause.message:'历史版本读取失败，请重新展开重试。');}
  finally{setHistoryLoading(false);}
 }
 async function act(endpoint:string,body?:Record<string,unknown>){
  if(!task||actionPending.current)return;
  actionPending.current=true;setBusy(true);onBusy(true);setError('');setNotice('');
  try{
   // The mutation already returns the verified task. Do not fetch the same
   // submissions and every historical S3 artifact again before showing it.
   const updated=await taskRequest<TaskItem>(`/items/${task.id}/${endpoint}`,'POST',{expected_revision:task.revision,...body});
   setTask(current=>({...updated,participants:updated.participants??current?.participants}));markDirty(false);setReset(value=>value+1);
   const message=endpoint==='start-review'?'已开始复核，请查看提交版本，检查后在下方选择通过或退回返修。':endpoint==='accept'?'验收成功，最终结果已锁定。':endpoint==='adjudicate'?'裁定已保存，等待管理员验收。':updated.status==='rework'?'复核已提交，已退回所选标注员返修。':updated.status==='adjudicating'?'复核已提交，请继续处理差异并完成裁定。':'复核已提交，可由管理员或组长继续验收。';
   if(endpoint==='start-review')setNotice(message);else setSuccess(message);
  }catch(cause){setError(cause instanceof Error?cause.message:'复核操作未确认成功，请刷新任务核对。');}
  finally{actionPending.current=false;setBusy(false);onBusy(false);}
 }
 return <section className="task-workbench-review" aria-label="任务复核"><header><h2>任务复核</h2></header>{success&&<SuccessDialog title="复核操作成功" message={success} onClose={()=>setSuccess('')}/>}{notice&&<p role="status" className="task-review-notice">{notice}</p>}{busy&&<p role="status" className="task-review-pending"><span className="task-review-spinner" aria-hidden="true"/>正在提交并核验，请稍候…</p>}{error&&<p role="alert" className="import-error">{error}</p>}{!task&&!error&&<p role="status">正在读取提交版本…</p>}{task&&<><p className="task-review-stage" role="status">{reviewStage(task)}</p><div inert={busy} onInput={()=>markDirty(true)}><TaskReview key={reset} task={task} rules={task.rule?[task.rule]:[]} user={user} roles={roles} accountName={id=>task.participants?.find(account=>account.id===id)?.display_name??id} busy={busy} onAction={(endpoint,body)=>void act(endpoint,body)} onLoadHistory={()=>void loadHistory()} historyLoading={historyLoading} workbench={{time,onSeek,onPreview:preview,versionId,selection,onDirty:()=>markDirty(true)}}/></div>{dirty&&<button className="secondary" disabled={busy} onClick={()=>{markDirty(false);setReset(value=>value+1);}}>清空未提交的复核填写</button>}</>}</section>;
}
