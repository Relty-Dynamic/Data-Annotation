import {taskTitle} from './types';
import {Download,PackageCheck} from 'lucide-react';
import type {FormEvent} from 'react';
import {TaskEmpty,TaskPageHeading,TaskStatus} from './TaskPresentation';
import type {TaskItem} from './types';
export default function TaskDelivery({items,deliveries,busy,onCreate,onDownload}:{items:TaskItem[];deliveries:Array<{id:string;task_ids?:string[]}>;busy:boolean;onCreate:(event:FormEvent<HTMLFormElement>)=>void;onDownload:(id:string)=>void}){
 const accepted=items.filter(item=>item.status==='accepted');
 return <div className="task-tab-page"><TaskPageHeading title="验收交付" description="选择已验收任务生成交付批次，随时下载历史结果。" count={`${accepted.length} 个可交付任务`}/>
  <form className="task-delivery-form" onSubmit={onCreate}><header className="task-section-heading"><div><h3>创建交付批次</h3><p>仅列出已完成验收的任务。</p></div><PackageCheck size={20} aria-hidden="true"/></header>
   {accepted.length?<div className="task-delivery-options">{accepted.map(item=><label key={item.id}><input type="checkbox" name="tasks" value={item.id} disabled={busy}/><span><strong>{taskTitle(item)}</strong><small>截止 {item.deadline}</small></span><TaskStatus status={item.status}/></label>)}</div>:<TaskEmpty title="暂无可交付任务">任务验收通过后，可在此生成交付批次。</TaskEmpty>}
   <footer className="task-section-footer"><p>生成后保存核验过的结果，交付记录持续保留。</p><button className="primary" disabled={busy||!accepted.length}>{busy?'正在处理…':'生成并核验交付批次'}</button></footer>
  </form>
  <section className="task-delivery-history"><header className="task-section-heading"><div><h3>交付历史</h3><p>按已保存的批次下载结果。</p></div><span className="task-page-count">{deliveries.length} 个批次</span></header>
   {!deliveries.length?<TaskEmpty title="暂无交付记录">生成首个批次后，下载入口会显示在这里。</TaskEmpty>:<div className="task-record-list">{deliveries.map(delivery=><article className="task-record" key={delivery.id}><span className="task-record-icon"><PackageCheck size={19} aria-hidden="true"/></span><div className="task-record-info"><strong>批次 {delivery.id}</strong>{delivery.task_ids&&<span>{delivery.task_ids.length} 个任务</span>}</div><button disabled={busy} onClick={()=>onDownload(delivery.id)}><Download size={14} aria-hidden="true"/>下载 CSV</button></article>)}</div>}
  </section>
 </div>;
}
