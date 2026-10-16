import {ClipboardList} from 'lucide-react';
import {TaskEmpty,TaskStatus} from './TaskPresentation';
import type {TaskItem} from './types';
export default function TaskList({items,userId,leader,busy,filtered,selectedId,onView,onOpen,onDelete,isReview=()=>false,onReview}:{items:TaskItem[];userId:string;leader:boolean;busy:boolean;filtered:boolean;selectedId?:string;onView:(item:TaskItem)=>void;onOpen:(id:string)=>void;onDelete:(item:TaskItem)=>void;isReview?:(item:TaskItem)=>boolean;onReview?:(item:TaskItem)=>void}){
 if(!items.length)return <TaskEmpty title={filtered?'没有匹配的任务':'当前没有可见任务'}>{filtered?'调整筛选条件后重试。':leader?'完成素材登记和质检后，可在“任务分配”中创建任务。':'任务分配后会显示在这里。'}</TaskEmpty>;
 return <div className="task-record-list">{items.map(item=><article className="task-record" data-selected={selectedId===item.id} key={item.id}>
  <span className="task-record-icon"><ClipboardList size={19} aria-hidden="true"/></span>
  <div className="task-record-info"><strong>{item.video_id}</strong><span>截止 {item.deadline}</span></div><TaskStatus status={item.status}/>
  <div className="task-record-actions"><button disabled={busy} onClick={()=>onView(item)}>查看任务</button>{isReview(item)&&onReview&&<button className="primary" disabled={busy} onClick={()=>onReview(item)}>打开复核工作台</button>}{item.status!=='deleted'&&item.annotator_ids.includes(userId)&&<button className="primary" onClick={()=>onOpen(item.id)}>打开标注工作台</button>}{leader&&item.status!=='deleted'&&<button className="task-danger" disabled={busy} onClick={()=>onDelete(item)}>删除任务</button>}</div>
 </article>)}</div>;
}
