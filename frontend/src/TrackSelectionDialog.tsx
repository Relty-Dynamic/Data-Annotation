import {useEffect, useRef, useState} from 'react';
import {libraryRequest, selectionOptions, type LibraryTrack} from './trackLibrary';
import {projectTracks, type Project} from './domain';
import './track-library.css';

export default function TrackSelectionDialog({project,onClose,onSave}:{project:Project;onClose:()=>void;onSave:(ids:string[])=>Promise<void>}) {
  const dialog=useRef<HTMLDialogElement>(null);
  const [library,setLibrary]=useState<LibraryTrack[]>([]),[loading,setLoading]=useState(true),[saving,setSaving]=useState(false),[error,setError]=useState('');
  const [loadFailed,setLoadFailed]=useState(false);
  const [chosen,setChosen]=useState(()=>projectTracks(project));
  useEffect(()=>{dialog.current?.showModal();void libraryRequest<LibraryTrack[]>('/api/account/track-library').then(setLibrary).catch(reason=>{setError(reason.message);setLoadFailed(true);}).finally(()=>setLoading(false));},[]);
  const options=selectionOptions(project,library);
  const save=async()=>{setSaving(true);setError('');try{await onSave(options.filter(track=>chosen.includes(track.id)).map(track=>track.id));}catch(reason){setError(reason instanceof Error?reason.message:'保存失败');}finally{setSaving(false);}};
  return <dialog ref={dialog} className="track-library-dialog track-selection-dialog" aria-labelledby="selection-title" onCancel={event=>{event.preventDefault();if(!saving)onClose();}}><div className="library-heading"><h2 id="selection-title">选择本项目的时间轴</h2><button className="secondary" disabled={saving} onClick={onClose}>关闭</button></div><p className="library-description">从账号时间轴库选择。取消勾选只隐藏时间轴，已有标注保留；重新勾选会恢复。项目已使用的轴继续使用原定义。</p>{loading?<p role="status">正在读取时间轴库…</p>:<div className="library-items">{options.map(track=><label className="library-selection" key={track.id}><input type="checkbox" disabled={saving} checked={chosen.includes(track.id)} onChange={event=>setChosen(ids=>event.target.checked?[...ids,track.id]:ids.filter(id=>id!==track.id))}/><div><strong>{track.name}</strong><small>{track.retained?'项目保留的时间轴':track.is_default?'默认时间轴':'个人时间轴'} · {project.annotations[track.id]?.length??0} 条标注</small><div className="library-tags">{track.labels.map(label=><span key={label}>{label}</span>)}</div></div></label>)}</div>}{error&&<p role="alert" className="import-error">{error}</p>}<div className="library-actions"><span>{chosen.length} 条已选</span><button className="secondary" disabled={saving} onClick={onClose}>取消</button><button className="primary" disabled={loading||saving||loadFailed} onClick={()=>void save()}>{saving?'保存中…':'保存选择'}</button></div></dialog>;
}
