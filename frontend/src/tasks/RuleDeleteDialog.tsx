import {useEffect,useId,useRef} from 'react';

export default function RuleDeleteDialog({name,busy,error,onCancel,onConfirm}:{name:string;busy:boolean;error:string;onCancel:()=>void;onConfirm:()=>void}){
 const ref=useRef<HTMLDialogElement>(null),titleId=useId(),descriptionId=useId();
 useEffect(()=>{const dialog=ref.current!;dialog.showModal();return()=>dialog.close()},[]);
 return <dialog ref={ref} className="task-rule-delete-dialog" role="alertdialog" aria-labelledby={titleId} aria-describedby={descriptionId} onCancel={event=>{event.preventDefault();if(!busy)onCancel()}}>
  <h3 id={titleId}>删除规则「{name}」？</h3>
  <p id={descriptionId}>删除后不再显示，也不能用于创建新任务。已绑定的任务继续使用原规则版本，历史标注和记录保留。</p>
  {error&&<p className="task-error" role="alert">{error}</p>}
  <form onSubmit={event=>{event.preventDefault();if(!busy)onConfirm()}}>
   <button type="button" autoFocus disabled={busy} onClick={onCancel}>取消</button>
   <button type="submit" className="task-danger" disabled={busy}>{busy?'正在删除…':'确认删除'}</button>
  </form>
 </dialog>
}
