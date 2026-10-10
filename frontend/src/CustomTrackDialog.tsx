import { useEffect, useRef, useState } from 'react';
import {TRACK_LABELS, type FixedTrack} from './domain';

export default function CustomTrackDialog({availableFixed,onAddFixed,onCancel,onCreate}: {
  availableFixed: FixedTrack[];
  onAddFixed:(track:FixedTrack)=>Promise<void>;
  onCancel:()=>void;
  onCreate:(name:string,mode:'state'|'event',labels:string[])=>Promise<void>;
}) {
  const dialog=useRef<HTMLDialogElement>(null);
  const [name,setName]=useState(''),[mode,setMode]=useState<'state'|'event'>('state');
  const [labels,setLabels]=useState(''),[error,setError]=useState(''),[saving,setSaving]=useState(false);
  useEffect(()=>{dialog.current?.showModal();},[]);
  const submit=async()=>{
    const title=name.trim(),items=labels.split(/[\n,，、]+/).map(value=>value.trim()).filter(Boolean);
    if(!title||title.length>80){setError('请输入不超过 80 个字符的时间轴名称。');return;}
    if(mode==='state'&&(!items.length||items.length>32||new Set(items).size!==items.length||items.some(item=>item.length>80))){setError('请填写 1–32 个不重复的状态标签，每个标签不超过 80 个字符。');return;}
    setSaving(true);setError('');
    try{await onCreate(title,mode,mode==='state'?items:[]);}catch(reason){setError(reason instanceof Error?reason.message:'添加时间轴失败，请重试。');}
    finally{setSaving(false);}
  };
  const restore=async(track:FixedTrack)=>{
    setSaving(true);setError('');
    try{await onAddFixed(track);}catch(reason){setError(reason instanceof Error?reason.message:'添加时间轴失败，请重试。');}
    finally{setSaving(false);}
  };
  return <dialog ref={dialog} className="custom-track-dialog" aria-labelledby="custom-track-title" onCancel={event=>{event.preventDefault();if(!saving)onCancel();}}>
    <form onSubmit={event=>{event.preventDefault();void submit();}}>
      <h2 id="custom-track-title">添加时间轴</h2>
      <p>只添加到当前项目。重新添加已删除的固定轴时，从空白开始标注。</p>
      {availableFixed.length>0&&<div className="restore-fixed-tracks"><strong>添加固定时间轴</strong><div>{availableFixed.map(track=><button type="button" className="secondary" key={track} disabled={saving} onClick={()=>void restore(track)}>{TRACK_LABELS[track]}</button>)}</div></div>}
      <strong>新建自定义时间轴</strong>
      <label>时间轴名称<input autoFocus maxLength={80} value={name} onChange={event=>setName(event.target.value)} placeholder="例如：环境"/></label>
      <label>标注方式<select value={mode} onChange={event=>setMode(event.target.value as 'state'|'event')}><option value="state">互斥状态：同一时刻一种状态</option><option value="event">可重叠事件：自由填写内容与时间</option></select></label>
      {mode==='state'&&<label>状态标签<textarea value={labels} onChange={event=>setLabels(event.target.value)} placeholder={'每行一个，例如：\n安静\n嘈杂'} rows={4}/><small>每行或用逗号分隔；添加后在时间轴上选择标签标注。</small></label>}
      {error&&<p role="alert" className="import-error">{error}</p>}
      <div className="custom-track-actions"><button type="button" className="secondary" disabled={saving} onClick={onCancel}>取消</button><button className="primary" type="submit" disabled={saving}>{saving?'添加中…':'添加时间轴'}</button></div>
    </form>
  </dialog>;
}
