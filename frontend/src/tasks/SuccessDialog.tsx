import {useEffect,useId,useRef} from 'react';
export default function SuccessDialog({message,onClose,title='操作成功'}:{message:string;onClose:()=>void;title?:string}){
 const ref=useRef<HTMLDialogElement>(null),titleId=useId(),messageId=useId();
 useEffect(()=>{const dialog=ref.current!;dialog.showModal();return()=>dialog.close()},[]);
 return <dialog ref={ref} className="task-rule-delete-dialog" aria-labelledby={titleId} aria-describedby={messageId} onCancel={event=>{event.preventDefault();onClose()}}>
  <h3 id={titleId}>{title}</h3><p id={messageId}>{message}</p><button autoFocus className="primary" onClick={onClose}>知道了</button>
 </dialog>;
}
