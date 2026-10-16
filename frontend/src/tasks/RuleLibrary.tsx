import {ChevronDown, FileText, Plus} from 'lucide-react';
import RuleEditor from './RuleEditor';
import {TaskEmpty} from './TaskPresentation';
import type {TaskAxis, TaskRule} from './types';

export default function RuleLibrary({rules,leader,busy,axes,onAxes,onCopy,onPublish,onDelete}:{
 rules:TaskRule[];leader:boolean;busy:boolean;axes:TaskAxis[];onAxes:(axes:TaskAxis[])=>void;
 onCopy:()=>void;onPublish:(body:Record<string,unknown>)=>void;onDelete?:(rule:TaskRule)=>void;
}){
 return <>
  <details className="task-rule-group">
   <summary><FileText size={18} aria-hidden="true"/><span className="task-rule-group-title">已发布规则</span><span className="task-rule-group-count">{rules.length} 个版本</span><ChevronDown size={17} className="task-rule-group-chevron" aria-hidden="true"/></summary>
   <div className="task-rule-group-content">
    {!rules.length&&<TaskEmpty title="暂无已发布规则">{leader?'展开“发布新规则”创建规则。':'管理人员发布规则后会显示在这里。'}</TaskEmpty>}
    {rules.map(rule=><details className="task-published-rule" key={rule.id}>
     <summary><span>{rule.name}</span><span className="task-rule-type">{rule.task_type}</span><span className="task-rule-count">{rule.axes.length} 条时间轴</span></summary>
     <p className="task-rule-tolerance">边界容差：{rule.boundary_tolerance_ms===null?'不限制（人工复核）':`${rule.boundary_tolerance_ms??0} 毫秒`}</p>
     <div className="task-rule-definition">{rule.axes.map(axis=><div className="task-rule-definition-axis" key={axis.id}><strong>{axis.name}</strong><p>{axis.labels.join('、')}</p><span>最短标注时长：{!axis.minimum_ms?'不限制':`${axis.minimum_ms} 毫秒`}</span></div>)}</div>
     {onDelete&&<button type="button" className="task-danger" disabled={busy} onClick={()=>onDelete(rule)}>删除规则</button>}
    </details>)}
   </div>
  </details>
  {leader&&<details className="task-rule-group task-rule-create">
   <summary><Plus size={18} aria-hidden="true"/><span className="task-rule-group-title">发布新规则</span><span className="task-rule-group-count">设置时间轴与标注要求</span><ChevronDown size={17} className="task-rule-group-chevron" aria-hidden="true"/></summary>
   <div className="task-rule-group-content"><RuleEditor axes={axes} onAxes={onAxes} busy={busy} onCopy={onCopy} onPublish={onPublish}/></div>
  </details>}
 </>;
}
