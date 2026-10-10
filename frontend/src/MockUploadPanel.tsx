import {useState} from 'react';
import {authFetch} from './auth';

type UploadStatus = {id:string; prefix:string; state:string; progress:number; detail:string; received:number; total:number};
type SelectedFile = {file:File; path:string};

async function responseJson<T>(response:Response):Promise<T> {
  if(!response.ok){
    let detail=`请求失败 (${response.status})`;
    try{const value=await response.json();if(typeof value.detail==='string')detail=value.detail;}catch{ /* Keep status. */ }
    throw new Error(detail);
  }
  return response.json();
}

function selectedFiles(list:FileList):{person:string; files:SelectedFile[]} {
  const files=Array.from(list).filter(file=>file.name!=='.DS_Store'&&!file.webkitRelativePath.split('/').slice(1)
    .some(part=>part.toLowerCase()==='timeline'||part.toLowerCase()==='.datamark-cache'));
  if(!files.length)throw new Error('所选目录没有可上传的文件。');
  const roots=new Set(files.map(file=>file.webkitRelativePath.split('/')[0]));
  if(roots.size!==1)throw new Error('请一次选择一个完整采集目录。');
  return {person:[...roots][0],files:files.map(file=>({file,path:file.webkitRelativePath.split('/').slice(1).join('/')||file.name}))};
}

export default function MockUploadPanel({onPublished,onRunningChange}:{onPublished:(prefix:string)=>void;onRunningChange:(running:boolean)=>void}){
  const [files,setFiles]=useState<SelectedFile[]>([]),[person,setPerson]=useState('');
  const [status,setStatus]=useState(''),[error,setError]=useState(''),[running,setRunning]=useState(false);
  async function publish(){
    setRunning(true);onRunningChange(true);setError('');
    let timer:ReturnType<typeof setInterval>|undefined;
    let taskId='';
    try{
      const started=await responseJson<UploadStatus>(await authFetch('/api/mock-s3/uploads',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({person,files:files.map(item=>({path:item.path,size:item.file.size}))})
      }));
      taskId=started.id;
      setStatus(`采集目录：${started.prefix}。开始接收 ${files.length} 个文件。`);
      for(let index=0;index<files.length;index++){
        setStatus(`上传 ${index+1}/${files.length}：${files[index].path}`);
        await responseJson<UploadStatus>(await authFetch(`/api/mock-s3/uploads/${started.id}/files/${index}`,{
          method:'PUT',headers:{'Content-Type':'application/octet-stream'},body:files[index].file
        }));
      }
      setStatus('文件接收完成，正在压缩 FPV 视频；IMU 和 HEART 保留原格式。');
      timer=setInterval(()=>{void authFetch(`/api/mock-s3/uploads/${started.id}`).then(responseJson<UploadStatus>)
        .then(value=>setStatus(`${value.detail} ${value.progress}%`)).catch(()=>{});},1000);
      const finished=await responseJson<UploadStatus>(await authFetch(`/api/mock-s3/uploads/${started.id}/finish`,{method:'POST'}));
      setStatus(`已发布 ${finished.prefix}，可以选择此采集目录开始标注。`);
      onPublished(finished.prefix);
    }catch(cause){
      if(taskId)void authFetch(`/api/mock-s3/uploads/${taskId}`,{method:'DELETE'}).catch(()=>{});
      setError(cause instanceof Error?cause.message:'上传失败。');
    }
    finally{if(timer)clearInterval(timer);setRunning(false);onRunningChange(false);}
  }
  return <section className="mock-upload-panel" aria-label="上传本机采集目录到 S3 mock">
    <h3>上传本机采集目录到 S3 mock</h3>
    <p>选择完整采集目录。视频根据文件名中的采集日期归档，先压缩为 270p MP4 再放入 FPV；IMU、HEART 等非视频文件保留原格式和相对目录。已有 timeline 和缓存不会上传，原文件仍在你的电脑上。</p>
    <input ref={element=>{element?.setAttribute('webkitdirectory','');}} type="file" multiple disabled={running}
      aria-label="选择完整采集目录" onChange={event=>{try{const result=selectedFiles(event.target.files!);setPerson(result.person);setFiles(result.files);setError('');setStatus(`已选择 ${result.files.length} 个文件。`);}catch(cause){setError(cause instanceof Error?cause.message:'无法读取目录。');setFiles([]);}}}/>
    {files.length>0&&<><label className="field-label">个人文件夹名称<input value={person} disabled={running} maxLength={120} onChange={event=>setPerson(event.target.value)}/></label>
      <p className="input-hint">原目录：{files[0].file.webkitRelativePath.split('/')[0]} · {files.length} 个文件</p>
      <button className="primary wide" disabled={running||!person.trim()} onClick={()=>void publish()}>{running?'上传和压缩中…':'上传并压缩到 S3 mock'}</button></>}
    {status&&<p role="status">{status}</p>}{error&&<p role="alert" className="import-error">{error}</p>}
  </section>;
}
