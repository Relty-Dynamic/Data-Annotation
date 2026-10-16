import {taskTitle} from './types';
import {formatOperationTime} from '../operationTime';
import {useEffect,useId,useRef,type ReactNode} from 'react';
import {TaskStatus} from './TaskPresentation';
import type {TaskItem} from './types';
export default function TaskDetailDialog({task,busy,error,onClose,accountName,canAnnotate,canReview,onAnnotate,onReview,audit,children}:{task:TaskItem;busy:boolean;error:string;onClose:()=>void;accountName:(id:string)=>string;canAnnotate:boolean;canReview:boolean;onAnnotate:()=>void;onReview:()=>void;audit:string[];children?:ReactNode}){
 const ref=useRef<HTMLDialogElement>(null),title=useId();
 useEffect(()=>{const dialog=ref.current!;dialog.showModal();return()=>dialog.close()},[]);
 return <dialog ref={ref} className="task-rule-delete-dialog task-detail-dialog" aria-labelledby={title} onCancel={event=>{event.preventDefault();if(!busy)onClose()}}>
  <header className="task-detail-heading"><div><h3 id={title}>任务详情</h3><strong>{taskTitle(task)}</strong></div><button type="button" className="secondary" autoFocus disabled={busy} onClick={onClose}>关闭</button></header>
  {error&&<p className="task-error" role="alert">{error}</p>}
  <dl className="task-detail-fields"><div><dt>任务状态</dt><dd><TaskStatus status={task.status}/></dd></div><div><dt>截止日期</dt><dd>{task.deadline}</dd></div><div><dt>标注规则</dt><dd>{task.rule?.name??task.rule_id}</dd></div><div><dt>采集文件夹</dt><dd>{task.video?.collection_name??'未登记'}</dd></div>{task.collection&&<div><dt>视频数量</dt><dd>{task.collection.videos.length} 段 · 完整采集</dd></div>}<div><dt>标注员</dt><dd>{task.annotator_ids.map(accountName).join('、')||'待分配'}</dd></div><div><dt>复核员</dt><dd>{task.reviewer_id?accountName(task.reviewer_id):'待分配'}</dd></div></dl>
  {(canAnnotate||canReview)&&<div className="task-detail-actions">{canAnnotate&&<button className="primary" disabled={busy} onClick={onAnnotate}>打开标注工作台</button>}{canReview&&<button className="primary" disabled={busy} onClick={onReview}>打开复核工作台</button>}</div>}
  {children}
  {task.collection&&<details className="task-detail-section"><summary>采集视频 · {task.collection.videos.length} 段</summary>{task.collection.videos.map(video=><p key={video.id}>{video.video_name??video.id} · {Math.round(video.duration_ms/1000)} 秒</p>)}</details>}
  {task.rule&&<details className="task-detail-section"><summary>规则与时间轴 · {task.rule.axes.length} 条</summary>{task.rule.axes.map(axis=><div className="task-detail-axis" key={axis.id}><strong>{axis.name}</strong><p>{axis.labels.join('、')}</p></div>)}</details>}
  {task.history&&<details className="task-detail-section"><summary>提交与最终版本记录 · {task.history.submissions.length+task.history.finals.length} 个</summary>{[...task.history.submissions,...task.history.finals].map(version=><p key={version.id}>{accountName(version.user_id??version.actor_id??'')} · {formatOperationTime(version.created_at??version.at)}</p>)}</details>}
  <details className="task-detail-section"><summary>操作记录 · {audit.length} 条</summary>{audit.length?audit.map((entry,index)=><p key={index}>{entry}</p>):<p>暂无操作记录。</p>}</details>
 </dialog>;
}
