import { useEffect, useRef, useState } from 'react';
import {TRACK_LABELS, type FixedTrack} from './domain';

export type TrackTemplate = {name:string; json_name:string; labels:string[]};

export default function CustomTrackDialog({availableFixed,templates,canRemember,onAddFixed,onCancel,onCreate,onAddTemplate,onDeleteTemplate}: {
  availableFixed: FixedTrack[];
  templates: TrackTemplate[];
  canRemember: boolean;
  onAddFixed:(track:FixedTrack)=>Promise<void>;
  onCancel:()=>void;
  onCreate:(name:string,jsonName:string,labels:string[],remember:boolean)=>Promise<void>;
  onAddTemplate:(template:TrackTemplate)=>Promise<void>;
  onDeleteTemplate:(jsonName:string)=>Promise<void>;
}) {
  const dialog=useRef<HTMLDialogElement>(null);
  const [name,setName]=useState(''),[jsonName,setJsonName]=useState('');
  const [labels,setLabels]=useState(''),[remember,setRemember]=useState(canRemember),[error,setError]=useState(''),[saving,setSaving]=useState(false);
  useEffect(()=>{dialog.current?.showModal();},[]);
  const run=async(action:()=>Promise<void>)=>{
    setSaving(true);setError('');
    try{await action();}catch(reason){setError(reason instanceof Error?reason.message:'时间轴操作失败，请重试。');}
    finally{setSaving(false);}
  };
  const submit=async()=>{
    const title=name.trim(),slug=jsonName.trim(),items=labels.split(/[\n,，、]+/).map(value=>value.trim()).filter(Boolean);
    if(!title||title.length>80){setError('请输入不超过 80 个字符的时间轴名称。');return;}
    if(!/^[a-z][a-z0-9_-]{0,63}$/.test(slug)||['scene','posture','category','habit'].includes(slug)||slug.startsWith('custom_')){setError('JSON 名须以小写字母开头，只能使用小写字母、数字、下划线或连字符，且不能使用固定时间轴名称。');return;}
    if(!items.length||items.length>32||new Set(items).size!==items.length||items.some(item=>item.length>80)){setError('请填写 1–32 个不重复的状态标签，每个标签不超过 80 个字符。');return;}
    await run(()=>onCreate(title,slug,items,remember));
  };
  return <dialog ref={dialog} className="custom-track-dialog" aria-labelledby="custom-track-title" onCancel={event=>{event.preventDefault();if(!saving)onCancel();}}>
    <form onSubmit={event=>{event.preventDefault();void submit();}}>
      <h2 id="custom-track-title">添加时间轴</h2>
      <p>新建自定义时间轴采用互斥标注；同一时刻只有一种状态。</p>
      {availableFixed.length>0&&<div className="restore-fixed-tracks"><strong>添加固定时间轴</strong><div>{availableFixed.map(track=><button type="button" className="secondary" key={track} disabled={saving} onClick={()=>void run(()=>onAddFixed(track))}>{TRACK_LABELS[track]}</button>)}</div></div>}
      {canRemember&&templates.length>0&&<div className="restore-fixed-tracks"><strong>我的时间轴历史</strong><div>{templates.map(template=><span className="template-item" key={template.json_name}><button type="button" className="secondary" disabled={saving} title={`${template.json_name}.timeline.json · ${template.labels.join('、')}`} onClick={()=>void run(()=>onAddTemplate(template))}>{template.name} · {template.json_name}</button><button type="button" className="text-button" disabled={saving} aria-label={`删除时间轴历史 ${template.name}`} onClick={()=>void run(()=>onDeleteTemplate(template.json_name))}>移除历史</button></span>)}</div></div>}
      <strong>新建自定义时间轴</strong>
      <label>时间轴名称<input autoFocus maxLength={80} value={name} onChange={event=>setName(event.target.value)} placeholder="例如：饮食"/></label>
      <label>JSON 文件名<input maxLength={64} value={jsonName} onChange={event=>setJsonName(event.target.value)} placeholder="food"/><small>例如输入 food，写回时生成 food.timeline.json。</small></label>
      <label>状态标签<textarea value={labels} onChange={event=>setLabels(event.target.value)} placeholder={'每行一个，例如：\n进食\n未进食'} rows={4}/><small>每行或用逗号分隔；添加后点击标签进行互斥标注。</small></label>
      {canRemember&&<label className="template-save-option"><input type="checkbox" checked={remember} onChange={event=>setRemember(event.target.checked)}/>保存到我的账号，以后可从“我的时间轴历史”快捷添加</label>}
      {error&&<p role="alert" className="import-error">{error}</p>}
      <div className="custom-track-actions"><button type="button" className="secondary" disabled={saving} onClick={onCancel}>取消</button><button className="primary" type="submit" disabled={saving}>{saving?'添加中…':'添加时间轴'}</button></div>
    </form>
  </dialog>;
}
