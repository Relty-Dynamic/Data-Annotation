import {useEffect,useLayoutEffect,useRef,useState} from 'react';
import {createPortal} from 'react-dom';
import {ChevronDown,ClipboardList,Search,RefreshCw} from 'lucide-react';
import type {Account} from '../auth';
import {taskRequest} from './TaskPanel';
import {assignedTaskChoices,type TaskChoice} from './taskChoices';
import type {TaskItem,TaskCapability} from './types';
export default function TaskPicker({user,currentTask,review,disabled,onChoose}:{user:Account;currentTask:string|null;review:boolean;disabled:boolean;onChoose:(choice:TaskChoice)=>void}){
 const [roles,setRoles]=useState<TaskCapability[]>([]);
 const [tasks,setTasks]=useState<TaskItem[]>([]),[loading,setLoading]=useState(true),[error,setError]=useState(''),[query,setQuery]=useState('');
 const [open,setOpen]=useState(false);
 const trigger=useRef<HTMLButtonElement>(null),menu=useRef<HTMLDialogElement>(null),mounted=useRef(true);
 function close(restore=false){setOpen(false);if(restore)trigger.current?.focus({preventScroll:true});}
 useLayoutEffect(()=>{
  if(!open||disabled)return;
  const dialog=menu.current;if(!dialog)return;
  dialog.showModal();dialog.querySelector<HTMLInputElement>('input')?.focus({preventScroll:true});
  const fullscreen=()=>close(true);
  document.addEventListener('fullscreenchange',fullscreen);
  return()=>{dialog.close();document.removeEventListener('fullscreenchange',fullscreen);};
 },[open,disabled]);
 useEffect(()=>{if(disabled)setOpen(false)},[disabled]);
 async function refresh(){setLoading(true);try{const [value,me]=await Promise.all([taskRequest<TaskItem[]>('/items'),taskRequest<{roles:TaskCapability[]}>('/me')]);if(mounted.current){setTasks(value);setRoles(me.roles);setError('');}}catch(cause){if(mounted.current)setError(cause instanceof Error?cause.message:'任务读取失败');}finally{if(mounted.current)setLoading(false);}}
 useEffect(()=>{mounted.current=true;void refresh();return()=>{mounted.current=false}},[user.id]);
 const choices=assignedTaskChoices(user,tasks,roles),current=choices.find(choice=>choice.id===currentTask&&choice.kind===(review?'review':'annotate'));
 const visible=choices.filter(choice=>`${choice.name} ${choice.detail} ${choice.status}`.toLowerCase().includes(query.trim().toLowerCase()));
 return <div className="task-assigned-picker" data-workspace-menu>
  <button type="button" ref={trigger} className="task-assigned-trigger" aria-label="选择项目：已分配任务" aria-haspopup="dialog" aria-expanded={open&&!disabled} disabled={disabled} onClick={()=>{if(!open)void refresh();setOpen(value=>!value);}} onKeyDown={event=>{event.stopPropagation();if(event.key==='ArrowDown'){event.preventDefault();setOpen(true);void refresh();}}}><ClipboardList size={15} aria-hidden="true"/><span>{current?.name??'选择项目（已分配任务）'}</span><ChevronDown size={14} aria-hidden="true"/></button>
  {open&&!disabled&&createPortal(<dialog ref={menu} className="task-assigned-popover task-assigned-dialog" role="dialog" aria-modal="true" aria-label="选择已分配任务" data-workspace-menu onCancel={event=>{event.preventDefault();close(true);}} onClick={event=>{if(event.target!==event.currentTarget)return;const rect=event.currentTarget.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)close(true);}} onKeyDown={event=>{event.stopPropagation();if(event.key==='Escape'){event.preventDefault();close(true);}if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();const choices=[...menu.current?.querySelectorAll<HTMLButtonElement>('.task-assigned-choice:not(:disabled)')??[]];if(choices.length){const index=choices.indexOf(document.activeElement as HTMLButtonElement),next=index<0?(event.key==='ArrowDown'?0:choices.length-1):event.key==='ArrowDown'?(index+1)%choices.length:(index-1+choices.length)%choices.length;choices[next].focus({preventScroll:true});choices[next].scrollIntoView({block:'nearest'});}}}}>
  <div className="task-assigned-menu"><header><strong>分配给我的任务</strong><button type="button" className="quiet" aria-label="关闭任务选择" onClick={()=>close(true)}>关闭</button><button type="button" className="quiet" aria-label="刷新任务列表" disabled={loading||disabled} onClick={()=>void refresh()}><RefreshCw size={14}/></button></header><label className="task-assigned-search"><Search size={14} aria-hidden="true"/><input aria-label="筛选已分配任务" value={query} onChange={event=>setQuery(event.target.value)} placeholder="搜索文件夹、视频或状态"/></label>
   {error&&<p role="alert">{error}</p>}{loading&&<p role="status">正在读取任务…</p>}{!loading&&!visible.length&&<p>{query?'没有匹配的已分配任务。':'暂无已分配任务，请联系组长或管理员。'}</p>}
   <div className="task-assigned-options">{(['annotate','review'] as const).map(kind=>{const group=visible.filter(choice=>choice.kind===kind);return group.length>0&&<section key={kind}><h4>{kind==='annotate'?'标注任务':'复核任务'} · {group.length}</h4>{group.map(choice=><button className="task-assigned-choice" type="button" key={choice.key} disabled={disabled||loading||!!error||choice.waiting} aria-current={choice.key===current?.key?'true':undefined} onClick={()=>{close(true);onChoose(choice);}}><strong>{choice.name}</strong><span>{choice.detail}</span><small>{choice.waiting?'等待标注员提交':choice.status}</small></button>)}</section>})}</div>
  </div>
 </dialog>,document.fullscreenElement??document.body)}
 </div>;
}
