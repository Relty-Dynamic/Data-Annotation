import {useEffect, useRef, useState} from 'react';

export default function TrackLabelsDialog({trackName,initial,annotations,onCancel,onSave}: {
  trackName:string;
  initial:string[];
  annotations:Array<{label:string}>;
  onCancel:()=>void;
  onSave:(labels:string[])=>Promise<void>;
}) {
  const dialog=useRef<HTMLDialogElement>(null);
  const [labels,setLabels]=useState([...initial]);
  const [input,setInput]=useState('');
  const [error,setError]=useState('');
  const [saving,setSaving]=useState(false);
  useEffect(()=>{dialog.current?.showModal();},[]);
  const removed=initial.filter(value=>!labels.includes(value));
  const affected=annotations.filter(item=>removed.includes(item.label)).length;
  const add=()=>{
    const value=input.trim();
    if(!value||value.length>80||/[\x00-\x1f\x7f-\x9f\uD800-\uDFFF]/u.test(value)){
      setError('标签须为 1–80 个字符，且不能包含控制字符。');return;
    }
    if(labels.includes(value)){setError('这条时间轴已有同名标签。');return;}
    if(labels.length>=32){setError('每条时间轴最多设置 32 个标签。');return;}
    setLabels([...labels,value]);setInput('');setError('');
  };
  const save=async()=>{
    if(input.trim()){setError('请先点击“添加标签”，再保存。');return;}
    setSaving(true);setError('');
    try{await onSave(labels);}catch(reason){setError(reason instanceof Error?reason.message:'保存标签失败，请重试。');}
    finally{setSaving(false);}
  };
  return <dialog ref={dialog} className="custom-track-dialog track-labels-dialog" aria-labelledby="track-labels-title"
    onCancel={event=>{event.preventDefault();if(!saving)onCancel();}}>
    <h2 id="track-labels-title">管理「{trackName}」标签</h2>
    <p>此项目的标签按钮，可添加或删除。最多 32 个；事件轴的标签是快捷填写按钮，仍可自由输入事件内容。</p>
    <div className="track-label-list">{labels.map(value=><div key={value}><span>{value}</span><button className="secondary" disabled={saving} aria-label={`删除标签 ${value}`} onClick={()=>setLabels(labels.filter(item=>item!==value))}>删除</button></div>)}{labels.length===0&&<p>当前没有标签按钮。</p>}</div>
    <div className="track-label-add"><input aria-label="新标签名称" maxLength={80} value={input} disabled={saving} placeholder="输入新标签" onChange={event=>setInput(event.target.value)} onKeyDown={event=>{if(event.key==='Enter'){event.preventDefault();add();}}}/><button className="secondary" disabled={saving} onClick={add}>添加标签</button></div>
    {affected>0&&<p className="track-label-warning" role="status">保存后会同时删除「{removed.join('、')}」的 {affected} 条已有标注。姿势出现空白时，需补齐后才能写回。</p>}
    {error&&<p role="alert" className="import-error">{error}</p>}
    <div className="custom-track-actions"><button className="secondary" disabled={saving} onClick={onCancel}>取消</button><button className={affected?'delete-custom-track-confirm':'primary'} disabled={saving||labels.join('\0')===initial.join('\0')} onClick={()=>void save()}>{saving?'保存中…':affected?`保存并删除 ${affected} 条标注`:'保存标签'}</button></div>
  </dialog>;
}
