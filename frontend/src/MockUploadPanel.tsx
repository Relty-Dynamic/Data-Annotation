import {useState} from 'react';
import {authFetch, type Account} from './auth';

type UploadStatus = {id:string; prefix:string; state:string; progress:number; detail:string; selected:number; total:number; collector_name:string; uploader_name:string; uploaded_by_username:string};
type LocalSelection = {selection_id:string; capture_date:string; files:{name:string;size:number}[]};

async function responseJson<T>(response:Response):Promise<T> {
  if(!response.ok){
    let detail=`请求失败 (${response.status})`;
    try{const value=await response.json();if(typeof value.detail==='string')detail=value.detail;}catch{ /* Keep status. */ }
    throw new Error(detail);
  }
  return response.json();
}

export default function MockUploadPanel({account,onPublished,onRunningChange}:{account:Account;onPublished:(prefix:string)=>void;onRunningChange:(running:boolean)=>void}){
  const [selection,setSelection]=useState<LocalSelection|null>(null),[collectorName,setCollectorName]=useState('');
  const [uploaderName,setUploaderName]=useState(''),[note,setNote]=useState('');
  const [status,setStatus]=useState(''),[error,setError]=useState(''),[running,setRunning]=useState(false);
  async function pick(){
    setRunning(true);onRunningChange(true);setError('');
    try{
      const response=await authFetch('/api/mock-s3/uploads/pick',{method:'POST'});
      if(response.status===204){setStatus('已取消选择。');return;}
      const result=await responseJson<LocalSelection>(response);
      setSelection(result);setStatus(`已从本机 video 文件夹选择 ${result.files.length} 个 AVI。`);
    }catch(cause){setError(cause instanceof Error?cause.message:'无法选择本机视频。');setSelection(null);}
    finally{setRunning(false);onRunningChange(false);}
  }
  async function publish(){
    if(!selection)return;
    setRunning(true);onRunningChange(true);setError('');
    let timer:ReturnType<typeof setInterval>|undefined;
    let taskId='';
    try{
      const started=await responseJson<UploadStatus>(await authFetch('/api/mock-s3/uploads',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({collector_name:collectorName,uploader_name:uploaderName.trim()||null,note:note.trim()||null,
          selection_id:selection.selection_id})
      }));
      taskId=started.id;
      setStatus(`采集目录：${started.prefix}。本机开始直接读取并压缩 ${selection.files.length} 个 AVI。`);
      timer=setInterval(()=>{void authFetch(`/api/mock-s3/uploads/${started.id}`).then(responseJson<UploadStatus>)
        .then(value=>setStatus(`${value.detail} ${value.progress}%`)).catch(()=>{});},1000);
      const finished=await responseJson<UploadStatus>(await authFetch(`/api/mock-s3/uploads/${started.id}/finish`,{method:'POST'}));
      setStatus(`已发布 ${finished.prefix}。采集人：${finished.collector_name}；上传人：${finished.uploader_name}；操作账号：${finished.uploaded_by_username}。`);
      onPublished(finished.prefix);
    }catch(cause){
      if(taskId)void authFetch(`/api/mock-s3/uploads/${taskId}`,{method:'DELETE'}).catch(()=>{});
      setError(cause instanceof Error?cause.message:'上传失败。');
    }
    finally{if(timer)clearInterval(timer);setRunning(false);onRunningChange(false);}
  }
  return <section className="mock-upload-panel" aria-label="上传 video 文件夹中的 AVI 到 S3 mock">
    <h3>上传 AVI 视频到 S3 mock</h3>
    <p>在上传人电脑运行本机服务。点击选择后，在系统窗口进入采集设备的 video 文件夹并选中本次采集的 AVI。本机程序直接读取 U 盘、压缩为 270p MP4；原始 AVI 不经过浏览器，也不进入模拟对象存储。此处不选择身份 TXT。</p>
    <button className="secondary wide" disabled={running} onClick={()=>void pick()}>选择本机 video 文件夹中的 AVI</button>
    {selection&&<><label className="field-label">采集人姓名<input value={collectorName} disabled={running} maxLength={80} onChange={event=>setCollectorName(event.target.value)} required/></label>
      <label className="field-label">上传人（选填；默认 {account.display_name}）<input value={uploaderName} disabled={running} maxLength={80} onChange={event=>setUploaderName(event.target.value)}/></label>
      <label className="field-label">备注（选填）<textarea value={note} disabled={running} maxLength={500} rows={3} onChange={event=>setNote(event.target.value)}/></label>
      <p className="input-hint">已选 {selection.files.length} 个 AVI，采集日期 {selection.capture_date}；目标文件夹自动命名为“月日＋采集人”。实际操作账号会单独记录。</p>
      <button className="primary wide" disabled={running||!collectorName.trim()} onClick={()=>void publish()}>{running?'上传和压缩中…':'上传并压缩到 S3 mock'}</button></>}
    {status&&<p role="status">{status}</p>}{error&&<p role="alert" className="import-error">{error}</p>}
  </section>;
}
