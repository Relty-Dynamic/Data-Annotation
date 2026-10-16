import {useEffect,useId,useRef} from 'react';
import {Check, X} from 'lucide-react';
import './successDialog.css';
export default function SuccessDialog({message,onClose,title='操作成功'}:{message:string;onClose:()=>void;title?:string}){
 const ref=useRef<HTMLDialogElement>(null),titleId=useId(),messageId=useId();
 useEffect(()=>{const dialog=ref.current!;dialog.showModal();return()=>dialog.close()},[]);
 return <dialog ref={ref} className="task-success-dialog" aria-labelledby={titleId} aria-describedby={messageId} onCancel={event=>{event.preventDefault();onClose()}}>
  <button type="button" className="task-success-close" aria-label="关闭提示" onClick={onClose}><X size={18}/></button>
  <div className="task-success-symbol" aria-hidden="true"><Check size={27} strokeWidth={2.2}/></div>
  <div className="task-success-copy"><h3 id={titleId}>{title}</h3><p id={messageId}>{message}</p></div>
  <div className="task-success-actions"><button type="button" autoFocus className="primary task-success-confirm" onClick={onClose}>知道了</button></div>
 </dialog>;
}
