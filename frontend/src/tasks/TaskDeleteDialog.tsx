import {useEffect,useId,useRef} from 'react';
import type {TaskItem} from './types';

export default function TaskDeleteDialog({task,busy,error,onCancel,onConfirm}:{task:TaskItem;busy:boolean;error:string;onCancel:()=>void;onConfirm:()=>void}){
 const ref=useRef<HTMLDialogElement>(null),titleId=useId(),descriptionId=useId();
 useEffect(()=>{const dialog=ref.current!;dialog.showModal();return()=>dialog.close()},[]);
 return <dialog ref={ref} className="task-rule-delete-dialog" role="alertdialog" aria-labelledby={titleId} aria-describedby={descriptionId} onCancel={event=>{event.preventDefault();if(!busy)onCancel()}}>
  <h3 id={titleId}>删除任务「{task.video_id}」？</h3>
  <p>{task.rule?.name??task.rule_id} · 任务编号 {task.id}</p>
  <p id={descriptionId}>删除后停止该任务的标注、复核和交付，可为同一视频重新创建相同类型任务。原始视频、草稿、历史提交、已有交付文件和操作日志保留；管理人员可通过“显示已删除任务”查看历史。</p>
  {error&&<p className="task-error" role="alert">{error}</p>}
  <form onSubmit={event=>{event.preventDefault();if(!busy)onConfirm()}}>
   <button type="button" autoFocus disabled={busy} onClick={onCancel}>取消</button>
   <button type="submit" className="task-danger" disabled={busy}>{busy?'正在删除…':'确认删除'}</button>
  </form>
 </dialog>
}
