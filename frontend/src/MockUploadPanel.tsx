import {useState} from 'react';
import {authFetch, type Account} from './auth';

type UploadStatus = {id:string; prefix:string; state:string; progress:number; detail:string; received:number; total:number; collector_name:string; uploader_name:string; uploaded_by_username:string};
type SelectedFile = {file:File; path:string};

async function responseJson<T>(response:Response):Promise<T> {
  if(!response.ok){
    let detail=`请求失败 (${response.status})`;
    try{const value=await response.json();if(typeof value.detail==='string')detail=value.detail;}catch{ /* Keep status. */ }
    throw new Error(detail);
  }
  return response.json();
}

function selectedVideos(list:FileList):SelectedFile[] {
  const files=Array.from(list);
  if(!files.length)throw new Error('请从 video 文件夹选择 AVI 视频。');
  if(files.some(file=>!file.name.toLowerCase().endsWith('.avi')))throw new Error('这一步只选择 AVI 视频；不要选择身份 TXT 或其他文件。');
  return files.map(file=>({file,path:file.name}));
}

export default function MockUploadPanel({account,onPublished,onRunningChange}:{account:Account;onPublished:(prefix:string)=>void;onRunningChange:(running:boolean)=>void}){
  const [files,setFiles]=useState<SelectedFile[]>([]),[collectorName,setCollectorName]=useState('');
  const [uploaderName,setUploaderName]=useState(''),[note,setNote]=useState('');
  const [status,setStatus]=useState(''),[error,setError]=useState(''),[running,setRunning]=useState(false);
  async function publish(){
    setRunning(true);onRunningChange(true);setError('');
    let timer:ReturnType<typeof setInterval>|undefined;
    let taskId='';
    try{
      const started=await responseJson<UploadStatus>(await authFetch('/api/mock-s3/uploads',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({collector_name:collectorName,uploader_name:uploaderName.trim()||null,note:note.trim()||null,
          files:files.map(item=>({path:item.path,size:item.file.size}))})
      }));
      taskId=started.id;
      setStatus(`采集目录：${started.prefix}。开始接收 ${files.length} 个文件。`);
      for(let index=0;index<files.length;index++){
        setStatus(`上传 ${index+1}/${files.length}：${files[index].path}`);
        await responseJson<UploadStatus>(await authFetch(`/api/mock-s3/uploads/${started.id}/files/${index}`,{
          method:'PUT',headers:{'Content-Type':'application/octet-stream'},body:files[index].file
        }));
      }
      setStatus('文件接收完成，正在压缩 AVI 视频。');
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
    <p>在文件选择窗口进入采集设备的 video 文件夹，选中本次采集的全部 AVI。可直接从 U 盘选择；程序会先暂存到本机，再压缩为 270p MP4。此处不选择身份 TXT。</p>
    <input type="file" accept=".avi" multiple disabled={running} aria-label="选择 video 文件夹中的 AVI"
      onChange={event=>{try{const result=selectedVideos(event.target.files!);setFiles(result);setError('');setStatus(`已选择 ${result.length} 个 AVI 视频。`);}catch(cause){setError(cause instanceof Error?cause.message:'无法读取视频。');setFiles([]);}}}/>
    {files.length>0&&<><label className="field-label">采集人姓名<input value={collectorName} disabled={running} maxLength={80} onChange={event=>setCollectorName(event.target.value)} required/></label>
      <label className="field-label">上传人（选填；默认 {account.display_name}）<input value={uploaderName} disabled={running} maxLength={80} onChange={event=>setUploaderName(event.target.value)}/></label>
      <label className="field-label">备注（选填）<textarea value={note} disabled={running} maxLength={500} rows={3} onChange={event=>setNote(event.target.value)}/></label>
      <p className="input-hint">已选 {files.length} 个 AVI；采集日期从视频文件名读取，目标文件夹自动命名为“月日＋采集人”。实际操作账号会单独记录。</p>
      <button className="primary wide" disabled={running||!collectorName.trim()} onClick={()=>void publish()}>{running?'上传和压缩中…':'上传并压缩到 S3 mock'}</button></>}
    {status&&<p role="status">{status}</p>}{error&&<p role="alert" className="import-error">{error}</p>}
  </section>;
}
