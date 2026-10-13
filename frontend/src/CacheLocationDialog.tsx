import {useEffect,useRef,useState} from 'react';
import {authFetch} from './auth';
import {chooseCacheDirectory} from './cacheDirectory';
import type {Project} from './domain';
export default function CacheLocationDialog({project,onClose,onChanged}:{project?:Project|null;onClose:()=>void;onChanged:(project?:Project)=>void}){
  const dialog=useRef<HTMLDialogElement>(null);
  const [collection,setCollection]=useState(project?.collection_folder_name??'');
  const [mode,setMode]=useState<'native'|'browser'|null>(null),[root,setRoot]=useState(''),[error,setError]=useState(''),[saving,setSaving]=useState(false);
  useEffect(()=>{dialog.current?.showModal();let stopped=false;void authFetch('/api/cache-location',{cache:'no-store'}).then(async r=>{const body=await r.json();if(!r.ok)throw new Error(body.detail??'无法读取缓存位置');if(!stopped){setMode(body.mode);setRoot(body.root??'');}}).catch(reason=>{if(!stopped)setError(reason.message);});return()=>{stopped=true;};},[]);
  async function save(){
    const name=collection.trim();
    if(project&&(!name||/[<>:"/\\|?*\x00-\x1f\x7f]/.test(name)||/[. ]$/.test(name)||new TextEncoder().encode(name).length>100||/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(name))){setError('请填写简短的采集目录名，不使用路径、系统保留名或特殊字符。');return;}
    setSaving(true);setError('');
    try{
      if(mode==='browser'){await chooseCacheDirectory();}
      else{const r=await authFetch('/api/cache-location',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({root:root.trim()})});const body=await r.json();if(!r.ok)throw new Error(body.detail??'缓存位置修改失败');}
      let updated:Project|undefined;
      if(project&&collection.trim()!==project.collection_folder_name){
        const response=await authFetch(`/api/projects/${project.id}/cache-collection`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:collection.trim(),expected_revision:project.revision})});
        const body=await response.json();if(!response.ok)throw new Error(body.detail??'采集目录名设置失败');updated=body;
      }
      onChanged(updated);onClose();
    }catch(reason){setError(reason instanceof Error?reason.message:'缓存位置设置失败。');}finally{setSaving(false);}
  }
  return <dialog ref={dialog} className="custom-track-dialog cache-location-dialog" aria-labelledby="cache-location-title" onCancel={e=>{e.preventDefault();if(!saving)onClose();}}>
    <h2 id="cache-location-title">缓存位置</h2><p>默认保存到桌面 dataMarkProject，以采集目录名分组。普通视频、倍速视频、封面与悬停图均保存在这里。</p>
    {mode==='native'?<label>本机缓存根目录<input aria-label="本机缓存根目录" value={root} disabled={saving} onChange={e=>setRoot(e.target.value)}/><small>修改位置会停止当前准备任务。原目录缓存保留，之后需重新准备。</small></label>:mode==='browser'?<p>请选择桌面的 dataMarkProject 文件夹；选择桌面时会自动创建该文件夹。浏览器需要目录读写授权，已有项目和标注不会移动。</p>:<p>正在读取当前设置…</p>}
    {project&&<label>采集目录名<input aria-label="缓存采集目录名" maxLength={100} disabled={saving} value={collection} onChange={e=>setCollection(e.target.value)}/><small>默认沿用原采集目录名；修改后旧缓存保留，下次准备时迁移并复用。</small></label>}
    {error&&<p role="alert" className="import-error">{error}</p>}
    <div className="custom-track-actions"><button className="secondary" disabled={saving} onClick={onClose}>取消</button><button className="primary" disabled={saving||!mode||(mode==='native'&&!root.trim())} onClick={()=>void save()}>{saving?'设置中…':mode==='browser'?'选择并授权缓存目录':'保存缓存位置'}</button></div>
  </dialog>;
}
