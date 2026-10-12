import {useEffect, useRef, useState} from 'react';

type Row = {original:string|null; name:string; note:string};
function splitLabel(value:string):Row {
  const match=/^(.*)（([^（）]*)）$/u.exec(value);
  return {original:value,name:match?match[1]:value,note:match?match[2]:''};
}
function fullLabel(row:Row):string {return row.note.trim()?`${row.name.trim()}（${row.note.trim()}）`:row.name.trim();}

export default function TrackLabelsDialog({trackName,initial,annotations,onCancel,onSave}: {
  trackName:string;
  initial:string[];
  annotations:Array<{label:string}>;
  onCancel:()=>void;
  onSave:(labels:string[],renames:Record<string,string>)=>Promise<void>;
}) {
  const dialog=useRef<HTMLDialogElement>(null);
  const [rows,setRows]=useState<Row[]>(initial.map(splitLabel));
  const [input,setInput]=useState(''),[newNote,setNewNote]=useState('');
  const [error,setError]=useState(''),[saving,setSaving]=useState(false);
  useEffect(()=>{dialog.current?.showModal();},[]);
  const labels=rows.map(fullLabel);
  const renames=Object.fromEntries(rows.filter(row=>row.original&&row.original!==fullLabel(row)).map(row=>[row.original!,fullLabel(row)]));
  const removed=initial.filter(value=>!labels.includes(value)&&!(value in renames));
  const affected=annotations.filter(item=>removed.includes(item.label)).length;
  const validate=(name:string,note:string)=>{
    const value=fullLabel({original:null,name,note});
    return !!name.trim()&&value.length<=80&&!/[\x00-\x1f\x7f-\x9f\uD800-\uDFFF]/u.test(value);
  };
  const add=()=>{
    if(!validate(input,newNote)){setError('标签及备注组合后须为 1–80 个字符，且不能包含控制字符。');return;}
    const row={original:null,name:input.trim(),note:newNote.trim()};
    if(labels.includes(fullLabel(row))){setError('这条时间轴已有同名标签。');return;}
    if(rows.length>=32){setError('每条时间轴最多设置 32 个标签。');return;}
    setRows([...rows,row]);setInput('');setNewNote('');setError('');
  };
  const save=async()=>{
    if(input.trim()||newNote.trim()){setError('请先点击“添加标签”，再保存。');return;}
    if(rows.some(row=>!validate(row.name,row.note))||new Set(labels).size!==labels.length){setError('请检查标签备注：组合文字不能重复或超过 80 个字符。');return;}
    setSaving(true);setError('');
    try{await onSave(labels,renames);}catch(reason){setError(reason instanceof Error?reason.message:'保存标签失败，请重试。');}
    finally{setSaving(false);}
  };
  return <dialog ref={dialog} className="custom-track-dialog track-labels-dialog" aria-labelledby="track-labels-title"
    onCancel={event=>{event.preventDefault();if(!saving)onCancel();}}>
    <h2 id="track-labels-title">管理「{trackName}」标签</h2>
    <p>备注直接显示在标签名后，例如“室内（教室）”；已有标注会同步改名，写回 JSON 使用相同文字。</p>
    <div className="track-label-list">{rows.map((row,index)=><div key={row.original??`new-${index}`}><span>{row.name}</span><input aria-label={`${row.name}的备注`} placeholder="备注（选填）" maxLength={80} value={row.note} disabled={saving} onChange={event=>setRows(items=>items.map((item,i)=>i===index?{...item,note:event.target.value}:item))}/><button className="secondary" disabled={saving} aria-label={`删除标签 ${fullLabel(row)}`} onClick={()=>setRows(items=>items.filter((_,i)=>i!==index))}>删除</button></div>)}{rows.length===0&&<p>当前没有标签按钮。</p>}</div>
    <div className="track-label-add"><input aria-label="新标签名称" maxLength={80} value={input} disabled={saving} placeholder="输入新标签" onChange={event=>setInput(event.target.value)} onKeyDown={event=>{if(event.key==='Enter'){event.preventDefault();add();}}}/><input aria-label="新标签备注" maxLength={80} value={newNote} disabled={saving} placeholder="备注（选填）" onChange={event=>setNewNote(event.target.value)} onKeyDown={event=>{if(event.key==='Enter'){event.preventDefault();add();}}}/><button className="secondary" disabled={saving} onClick={add}>添加标签</button></div>
    {affected>0&&<p className="track-label-warning" role="status">保存后会同时删除「{removed.join('、')}」的 {affected} 条已有标注。{(trackName==='姿势'||trackName==='场景')&&'留下的空白可写回，导出前须补齐。'}</p>}
    {error&&<p role="alert" className="import-error">{error}</p>}
    <div className="custom-track-actions"><button className="secondary" disabled={saving} onClick={onCancel}>取消</button><button className={affected?'delete-custom-track-confirm':'primary'} disabled={saving||labels.join('\0')===initial.join('\0')} onClick={()=>void save()}>{saving?'保存中…':affected?`保存并删除 ${affected} 条标注`:'保存标签'}</button></div>
  </dialog>;
}
