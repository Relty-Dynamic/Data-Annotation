import {useEffect, useRef, useState} from 'react';
import {ArrowLeft, BookOpen, Plus, Pencil, Trash2, X} from 'lucide-react';
import {libraryRequest, type LibraryTrack} from './trackLibrary';
import './track-library.css';

const emptyTrack=():LibraryTrack=>({id:'',name:'',mode:'state',labels:[],is_default:false});

function DeleteConfirmation({track,busy,error,onClose,onConfirm}:{track:LibraryTrack;busy:boolean;error:string;onClose:()=>void;onConfirm:()=>void}) {
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{const element=dialog.current;element?.showModal();return()=>element?.close();},[]);
  return <dialog ref={dialog} className="track-library-dialog library-confirm-dialog" aria-labelledby="library-delete-title" onCancel={event=>{event.preventDefault();event.stopPropagation();if(!busy)onClose();}}>
    <div className="library-heading"><h2 id="library-delete-title">删除时间轴</h2><button className="icon-button" aria-label="取消删除" disabled={busy} onClick={onClose}><X size={20}/></button></div>
    <p className="library-description">从{track.is_default?'所有账号的默认库':'个人时间轴库'}删除「{track.name}」？已有项目中的标注和时间轴副本均保留。</p>
    {error&&<p className="import-error" role="alert">{error}</p>}
    <div className="library-actions"><button className="secondary" autoFocus disabled={busy} onClick={onClose}>取消</button><button className="delete-custom-track-confirm" disabled={busy} onClick={onConfirm}>{busy?'删除中…':'确认从库中删除'}</button></div>
  </dialog>;
}

export default function TrackLibraryDialog({admin,onClose}:{admin:boolean;onClose:()=>void}) {
  const dialog=useRef<HTMLDialogElement>(null),nameInput=useRef<HTMLInputElement>(null),returnFocus=useRef<string>('');
  const [tracks,setTracks]=useState<LibraryTrack[]>([]),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [edit,setEdit]=useState<LibraryTrack|null>(null),[creating,setCreating]=useState(false),[labels,setLabels]=useState(''),[remove,setRemove]=useState<LibraryTrack|null>(null);
  const refresh=async()=>setTracks(await libraryRequest<LibraryTrack[]>('/api/account/track-library'));
  useEffect(()=>{const element=dialog.current;element?.showModal();void refresh().catch(reason=>setError(reason.message)).finally(()=>setLoading(false));return()=>element?.close();},[]);
  const editing=edit!==null;
  useEffect(()=>{
    dialog.current?.scrollTo(0,0);
    if(editing)nameInput.current?.focus();
    else Array.from(dialog.current?.querySelectorAll<HTMLButtonElement>('[data-library-focus]')??[]).find(button=>button.dataset.libraryFocus===returnFocus.current)?.focus();
  },[editing]);
  const startEdit=(track?:LibraryTrack)=>{returnFocus.current=track?`edit-${track.id}`:'create';setCreating(!track);setEdit(track?{...track}:emptyTrack());setLabels(track?.labels.join('\n')??'');setError('');};
  const back=()=>{setEdit(null);setError('');};
  const run=async(action:()=>Promise<void>)=>{setBusy(true);setError('');try{await action();}catch(reason){setError(reason instanceof Error?reason.message:'时间轴操作失败');}finally{setBusy(false);}};
  const save=()=>run(async()=>{
    if(!edit)return;
    const items=labels.split(/[\n,，、]+/).map(value=>value.trim()).filter(Boolean);
    if(!edit.name.trim()||!edit.id.trim()||!items.length||items.length>32||new Set(items).size!==items.length)throw new Error('请填写名称、JSON 名和 1–32 个不重复的标签。');
    const saved=await libraryRequest<LibraryTrack>(`/api/account/track-library${creating?'':`/${encodeURIComponent(edit.id)}`}`,creating?'POST':'PUT',{...edit,id:edit.id.trim(),name:edit.name.trim(),labels:items});
    setTracks(current=>creating?[...current,saved]:current.map(track=>track.id===saved.id&&track.is_default===saved.is_default?saved:track));
    setEdit(null);
  });
  const deleteTrack=()=>run(async()=>{
    if(!remove)return;
    await libraryRequest(`/api/account/track-library/${encodeURIComponent(remove.id)}?default=${remove.is_default}`,'DELETE',{confirmed:true});
    setTracks(current=>current.filter(track=>track.id!==remove.id||track.is_default!==remove.is_default));
    setRemove(null);
  });
  const toggleDefault=(track:LibraryTrack)=>run(async()=>{
    const saved=await libraryRequest<LibraryTrack>(`/api/account/track-library/${encodeURIComponent(track.id)}/default`,'POST',{is_default:!track.is_default});
    setTracks(current=>current.map(item=>item.id===track.id&&item.is_default===track.is_default?saved:item));
  });
  const title=edit?(creating?'创建时间轴':`修改「${tracks.find(track=>track.id===edit.id)?.name??edit.name}」`):'时间轴库';
  return <dialog ref={dialog} className="track-library-dialog" aria-labelledby="library-title" onCancel={event=>{event.preventDefault();if(!busy){if(edit)back();else onClose();}}}>
    <div className="library-heading"><div>{edit?<button className="icon-button" aria-label="返回时间轴库" disabled={busy} onClick={back}><ArrowLeft size={20}/></button>:<BookOpen size={22}/>}<h2 id="library-title">{title}</h2></div><button className="icon-button" aria-label="关闭时间轴库" disabled={busy} onClick={onClose}><X size={20}/></button></div>
    {edit?<form className="library-editor" onSubmit={event=>{event.preventDefault();void save();}}>
      <p className="library-description">{creating?'创建后可在项目中勾选使用。':'修改仅影响时间轴库，已有项目继续使用原副本。'}</p>
      <div className="library-editor-fields"><label>时间轴名称<input ref={nameInput} value={edit.name} maxLength={80} disabled={busy} onChange={event=>setEdit({...edit,name:event.target.value})}/></label><label>JSON 名<input value={edit.id} maxLength={64} disabled={busy||!creating} placeholder="例如 food" onChange={event=>setEdit({...edit,id:event.target.value})}/><small>创建后保持不变，写回生成对应 .timeline.json。</small></label></div>
      <label>标签<textarea rows={5} disabled={busy} value={labels} onChange={event=>setLabels(event.target.value)} placeholder="每行一个标签，也可用逗号分隔"/><small>填写 1–32 个不重复的标签。</small></label>
      {error&&<p className="import-error" role="alert">{error}</p>}
      <div className="library-actions"><button type="button" className="secondary" disabled={busy} onClick={back}>取消并返回</button><button className="primary" disabled={busy} type="submit">{busy?'保存中…':'保存到时间轴库'}</button></div>
    </form>:<>
      <p className="library-description">默认时间轴由管理员统一维护；个人时间轴仅当前账号可见。库的修改和删除不会改变已有项目中的时间轴或标注。{admin&&'增加为默认后所有账号可见；移除默认后转入你的个人库。'}</p>
      {loading&&<p role="status">正在读取时间轴库…</p>}
      {!loading&&<>{[true,false].map(isDefault=><section className="library-section" key={String(isDefault)}><h3>{isDefault?'默认时间轴':'个人时间轴'}<small>{isDefault?'所有账号共享':'仅当前账号'}</small></h3><div className="library-items">{tracks.filter(track=>track.is_default===isDefault).map(track=><article className="library-item" key={track.id}><div className="library-item-header"><strong>{track.name}</strong><small>{track.id}.timeline.json</small>{(!isDefault||admin)&&<div className="library-item-actions">{admin&&<button className="secondary library-default-button" disabled={busy} onClick={()=>void toggleDefault(track)}>{isDefault?'移除默认时间轴':'增加为默认时间轴'}</button>}<button className="icon-button" disabled={busy} aria-label={`修改${track.name}`} data-library-focus={`edit-${track.id}`} onClick={()=>startEdit(track)}><Pencil size={16}/></button><button className="icon-button" disabled={busy} aria-label={`删除${track.name}`} onClick={()=>{setRemove(track);setError('');}}><Trash2 size={16}/></button></div>}</div><div className="library-tags">{track.labels.map(label=><span key={label}>{label}</span>)}{track.labels.length===0&&<small>无快捷标签</small>}</div></article>)}</div>{tracks.every(track=>track.is_default!==isDefault)&&<p className="muted-text">暂无{isDefault?'默认':'个人'}时间轴</p>}</section>)}<button className="secondary" disabled={busy} data-library-focus="create" onClick={()=>startEdit()}><Plus size={16}/>创建时间轴</button></>}
      {error&&!remove&&<p className="import-error" role="alert">{error}</p>}
    </>}
    {remove&&<DeleteConfirmation track={remove} busy={busy} error={error} onClose={()=>{setRemove(null);setError('');}} onConfirm={()=>void deleteTrack()}/>}
  </dialog>;
}

