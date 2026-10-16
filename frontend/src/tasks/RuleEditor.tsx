import {useState,type FormEvent} from 'react';
import {labels,type TaskAxis} from './types';

export default function RuleEditor({axes,onAxes,busy,onCopy,onPublish}:{axes:TaskAxis[];onAxes:(axes:TaskAxis[])=>void;busy:boolean;onCopy:()=>void;onPublish:(body:Record<string,unknown>)=>void}){
 const [limitTolerance,setLimitTolerance]=useState(false);
 function submit(event:FormEvent<HTMLFormElement>){
  event.preventDefault();
  const data=new FormData(event.currentTarget),text=(key:string)=>String(data.get(key)??'').trim();
  onPublish({name:text('name'),task_type:text('type'),axes,boundary_tolerance_ms:limitTolerance?Number(text('tolerance')):null});
 }
 return <form className="task-rule-form" onSubmit={submit}>
  <div className="task-assignment-heading"><div><h3>规则内容</h3><p>设置任务规则和时间轴，容差及最短标注时长默认不限制。</p></div><button type="button" disabled={busy} onClick={onCopy}>从时间轴库复制</button></div>
  <fieldset className="task-rule-basics"><legend>基本信息</legend>
   <label>规则名称<input name="name" required placeholder="例如：姿势标注规则 V1.0"/></label>
   <label>任务类型<input name="type" required placeholder="例如：姿势标注"/></label>
   <label>边界容差<select value={limitTolerance?'custom':'none'} disabled={busy} onChange={event=>setLimitTolerance(event.target.value==='custom')}><option value="none">不限制（默认）</option><option value="custom">设置边界容差</option></select>{limitTolerance&&<span className="task-rule-duration"><input aria-label="边界容差（毫秒）" name="tolerance" type="number" min="0" step="1" required defaultValue="0"/><span>毫秒</span></span>}<span className="task-rule-hint">不限制时由复核员人工判断，保留主动标记的待复核项。</span></label>
  </fieldset>
  <div className="task-rule-section-title"><h4>时间轴配置</h4><span>{axes.length} 条时间轴</span></div>
  <div className="task-rule-axes">{axes.map((axis,index)=>{
   const change=(patch:Partial<TaskAxis>)=>onAxes(axes.map((a,i)=>i===index?{...a,...patch}:a));
   return <fieldset className="task-rule-axis" key={index}><legend>时间轴 {index+1}{axis.name?` · ${axis.name}`:''}</legend>
    <div className="task-rule-axis-fields">
     <label>轴编号<input value={axis.id} required placeholder="例如：posture" onChange={e=>change({id:e.target.value})}/></label>
     <label>轴名称<input value={axis.name} required placeholder="例如：姿势" onChange={e=>change({name:e.target.value})}/></label>
     <label>类型<select value={axis.mode} onChange={e=>change({mode:e.target.value as 'state'|'event'})}><option value="state">状态</option><option value="event">事件</option></select></label>
     <label className="task-rule-labels">标签<input key={axis.labels.join()} defaultValue={axis.labels.join('，')} placeholder="多个标签用逗号分隔" onBlur={e=>change({labels:labels(e.target.value)})}/><span className="task-rule-hint">例如：坐、站、躺；输入时请使用逗号分隔。</span></label>
     <label className="task-rule-minimum">最短标注时长<select value={axis.minimum_ms===undefined?'none':'custom'} onChange={e=>change({minimum_ms:e.target.value==='none'?undefined:1000})}><option value="none">不限制（默认）</option><option value="custom">设置最短时长</option></select>{axis.minimum_ms!==undefined&&<span className="task-rule-duration"><input aria-label={`时间轴 ${index+1} 最短标注时长（毫秒）`} type="number" min="1" step="1" required value={axis.minimum_ms||''} onChange={e=>change({minimum_ms:e.target.value?Number(e.target.value):0})}/><span>毫秒</span></span>}<span className="task-rule-hint">每个标注色块的最短持续时间，不限制时可提交任意合法时长。</span></label>
    </div>
    <details className="task-rule-optional"><summary>交通工具要求<span>选填</span></summary><div>
     <label>需填写交通工具的标签<input key={axis.transport_labels?.join()} defaultValue={axis.transport_labels?.join('，')??''} placeholder="多个标签用逗号分隔" onBlur={e=>change({transport_labels:labels(e.target.value)})}/></label>
     <label>交通工具选项<input key={axis.transport_types?.join()} defaultValue={axis.transport_types?.join('，')??''} placeholder="例如：汽车，公交车，地铁" onBlur={e=>change({transport_types:labels(e.target.value)})}/></label>
    </div></details>
    <div className="task-rule-axis-actions"><button type="button" disabled={busy||axes.length===1} onClick={()=>onAxes(axes.filter((_,i)=>i!==index))}>移除此轴</button></div>
   </fieldset>;
  })}</div>
  <button className="task-rule-add" type="button" disabled={busy} onClick={()=>onAxes([...axes,{id:'',name:'',mode:'state',labels:[]}])}>＋ 增加时间轴</button>
  <div className="task-assignment-footer"><p>发布后内容不可修改，已有任务始终使用绑定的规则版本。</p><button className="primary" disabled={busy}>发布规则版本</button></div>
 </form>;
}
