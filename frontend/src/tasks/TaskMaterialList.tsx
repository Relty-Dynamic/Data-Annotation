import {ChevronDown,FolderOpen,Video} from 'lucide-react';
import type {TaskSource} from './collections';
import type {TaskVideo} from './types';
const qualityNames:Record<string,string>={unknown:'未质检',passed:'合格',failed:'不合格'};
type Collection={id:string;name:string;videos:TaskVideo[]};
export default function TaskMaterialList({collections,sources,userId,canUpload,busy,onQuality}:{collections:Collection[];sources:TaskSource[];userId:string;canUpload:boolean;busy:boolean;onQuality:(id:string,quality:string)=>void}){
 const names=new Map(sources.map(source=>[`${source.source.backend}:${source.source.key}`,source.name]));
 return <section className="task-material-library" aria-label="已登记采集文件夹">
  <header className="task-material-section-heading"><div><h3>已登记采集文件夹</h3><p>按文件夹查看素材，展开后可逐段检查质检结论。</p></div><span className="task-material-count">{collections.length} 个文件夹</span></header>
  {!collections.length&&<div className="task-material-empty"><FolderOpen size={28} aria-hidden="true"/><strong>还没有登记的素材</strong><p>{canUpload?'导入采集目录后，在上方填写信息并登记文件夹。':'上传人员登记素材后，会显示在这里。'}</p></div>}
  <div className="task-material-folders">{collections.map(collection=>{
   const passed=collection.videos.filter(video=>video.quality==='passed').length;
   const unknown=collection.videos.filter(video=>video.quality==='unknown').length;
   const failed=collection.videos.filter(video=>video.quality==='failed').length;
   return <details className="task-material-folder" key={collection.id}>
    <summary><span className="task-material-folder-icon"><FolderOpen size={19} aria-hidden="true"/></span><span className="task-material-folder-name"><strong>{collection.name}</strong><span>{collection.videos.length} 段视频</span></span><span className="task-material-statuses">{passed>0&&<span className="task-quality-badge" data-quality="passed">合格 {passed}</span>}{unknown>0&&<span className="task-quality-badge" data-quality="unknown">待质检 {unknown}</span>}{failed>0&&<span className="task-quality-badge" data-quality="failed">不合格 {failed}</span>}</span><ChevronDown className="task-material-chevron" size={16} aria-hidden="true"/></summary>
    <div className="task-material-video-list">{collection.videos.map(video=><article className="task-material-video" key={video.id}>
     <Video size={16} aria-hidden="true"/><div className="task-material-video-info"><strong>{video.video_name??names.get(`${video.source.backend}:${video.source.key}`)??video.id}</strong><span>{video.scene} · {video.date} · {Math.round(video.duration_ms/1000)} 秒</span></div>
     {canUpload&&video.uploader_id===userId?<label className="task-material-quality">质检结论<select aria-label={`${video.id}质检结论`} value={video.quality} disabled={busy} onChange={event=>onQuality(video.id,event.target.value)}><option value="unknown">未质检</option><option value="passed">合格</option><option value="failed">不合格</option></select></label>:<span className="task-quality-badge" data-quality={video.quality}>{qualityNames[video.quality]??video.quality}</span>}
    </article>)}</div>
   </details>;
  })}</div>
 </section>;
}
