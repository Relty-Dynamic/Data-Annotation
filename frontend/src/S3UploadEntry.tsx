import {useEffect,useState,useRef,type FormEvent} from 'react';
import {createPortal} from 'react-dom';
import {apiOrigin} from './apiOrigin';
import {authFetch} from './auth';
import {bridgeRequest,helperStorageKey,installCommands,takeHelperHandoff,type HelperStatus} from './uploadBridge';
import './s3Upload.css';
import {CheckCircle2,FolderOpen,Upload,RefreshCw,X} from 'lucide-react';
import SuccessDialog from './tasks/SuccessDialog';
import {claimUploadCompletion,uploadStateName} from './uploadFeedback';

let pairedHelper='';
const notifiedUploads=new Set<string>();
function readId(){try{return pairedHelper||localStorage.getItem(helperStorageKey)||'';}catch{return pairedHelper;}}
export function UploadDialog({onClose}:{onClose:()=>void}){
 const [helper,setHelper]=useState<HelperStatus|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[checking,setChecking]=useState(true),[copied,setCopied]=useState('');
 const [backendReady,setBackendReady]=useState<boolean|null>(null),[connectionError,setConnectionError]=useState('');
 const [collector,setCollector]=useState(''),[uploader,setUploader]=useState(''),[note,setNote]=useState('');
 const dialogRef=useRef<HTMLDialogElement>(null),mounted=useRef(true);
 const [success,setSuccess]=useState('');
 const [predictionChecking,setPredictionChecking]=useState(true);
 const [predictionReady,setPredictionReady]=useState(false),[predict,setPredict]=useState(false),[predictionError,setPredictionError]=useState('');
 const [chest,setChest]=useState(''),[thigh,setThigh]=useState('');
 useEffect(()=>{let alive=true;void authFetch('/api/prediction/capabilities').then(async response=>{if(!response.ok)throw new Error('预标注服务暂不可用，可仅上传素材。');const value=await response.json();if(alive){setPredictionReady(value.enabled===true);setPredict(value.enabled===true);}}).catch(cause=>{if(alive)setPredictionError(cause instanceof Error?cause.message:'预标注服务暂不可用。');}).finally(()=>{if(alive)setPredictionChecking(false);});return()=>{alive=false;};},[]);
 const commands=installCommands(apiOrigin||location.origin,typeof navigator==='undefined'?'':navigator.userAgent);
 async function check(){const id=readId();if(!id){setHelper(null);setChecking(false);return;}try{const value=await bridgeRequest<HelperStatus>('helpers/'+id);if(!mounted.current)return;setHelper(value);setConnectionError('');setChecking(false);}catch(cause){if(!mounted.current)return;setHelper(null);setChecking(false);setConnectionError(cause instanceof Error?cause.message:'组件检测失败。');}}
 useEffect(()=>{mounted.current=true;let alive=true,ready=false;void authFetch('/api/health').then(async response=>{if(!response.ok)throw new Error('无法连接公网上传服务。');const health=await response.json();if(!alive)return;ready=health.capabilities?.includes('s3-helper-bridge')===true;setBackendReady(ready);if(ready)void check();else setChecking(false);}).catch(()=>{if(alive){setBackendReady(false);setChecking(false);setConnectionError('无法连接公网上传服务，请检查网络后刷新。');}});const timer=setInterval(()=>{if(alive&&ready)void check();},2000);return()=>{alive=false;mounted.current=false;clearInterval(timer);};},[]);
 useEffect(()=>{const dialog=dialogRef.current!;dialog.showModal();return()=>dialog.close()},[]);
 useEffect(()=>{if(busy)return;const completion=claimUploadCompletion(helper,notifiedUploads);if(completion)setSuccess(completion.prefix)},[helper,busy]);
 async function command(action:'select'|'start',event?:FormEvent){event?.preventDefault();if(!helper)return;setBusy(true);setError('');try{setHelper(await bridgeRequest<HelperStatus>('helpers/'+helper.id+'/commands',{action,fields:action==='start'?{collector:collector.trim(),uploader:uploader.trim(),note:note.trim(),prediction:predict?'generate':'keep',chest_device:chest.trim(),thigh_device:thigh.trim()}:{}}));}catch(cause){setError(cause instanceof Error?cause.message:'操作未完成。');}finally{setBusy(false);}}
 async function copy(value:string,message='已复制，请在本机终端粘贴运行。'){try{await navigator.clipboard.writeText(value);setCopied(message);}catch{setCopied('请手动选中并复制命令。');}}
 const pending=busy||!!helper?.command||!!helper?.report.busy;
 const selection=helper?.report.selection,job=helper?.report.job;
 return createPortal(<>
 <dialog ref={dialogRef} className="s3-upload-dialog" aria-labelledby="s3-upload-title" aria-modal="true" data-s3-upload-dialog onCancel={event=>{event.preventDefault();onClose();}}>
  <header className="s3-upload-heading"><div><h2 id="s3-upload-title">上传采集文件夹</h2><p>选择文件夹，在本机压缩后上传到 S3。</p></div><button type="button" className="s3-upload-icon" aria-label="关闭上传" onClick={onClose}><X size={19}/></button></header>
  {checking?<p className="s3-upload-message" role="status">正在检测本机组件…</p>:backendReady===false?<p className="s3-upload-error" role="alert">上传服务暂不可用，请稍后刷新重试。</p>:!helper?.online?<section className="s3-upload-card s3-upload-install"><h3>连接本机上传组件</h3><p>首次使用或组件未启动时，复制对应命令到本机终端运行，并保持终端开启。</p><label>macOS<code>{commands.mac}</code><button type="button" className="s3-upload-secondary" onClick={()=>void copy(commands.mac)}>复制 macOS 命令</button></label><label>Windows PowerShell<code>{commands.windows}</code><button type="button" className="s3-upload-secondary" onClick={()=>void copy(commands.windows)}>复制 Windows 命令</button></label>{copied&&<p role="status">{copied}</p>}<button type="button" className="s3-upload-secondary" onClick={()=>void check()}><RefreshCw size={14}/>重新检测</button></section>:<>
   <section className="s3-upload-card"><div className="s3-upload-section-heading"><h3>采集素材</h3><span className="s3-upload-ready"><CheckCircle2 size={13}/>组件已连接</span></div>
    <button type="button" className="s3-upload-select" disabled={pending} onClick={()=>void command('select')}><FolderOpen size={19}/><span>{selection?selection.name:'选择本机采集文件夹'}<small>{pending&&helper.command?.action==='select'?'请在系统窗口选择文件夹…':selection?`${selection.capture_date} · ${selection.videos.length} 段视频 · ${selection.attachments.length} 个附属文件`:'选择包含视频及采集数据的文件夹'}</small></span></button>
    {selection&&<details className="s3-upload-files"><summary>文件清单{selection.ignored.length>0?` · ${selection.ignored.length} 项不参与上传`:''}</summary><pre>{['视频',...selection.videos,...(selection.attachments.length?['','附属数据',...selection.attachments]:[]),...(selection.ignored.length?['','不上传',...selection.ignored]:[])].join('\n')}</pre></details>}
   </section>
   {selection&&job?.state!=='ready'&&<form className="s3-upload-card s3-upload-form" onSubmit={event=>void command('start',event)}><h3>上传信息</h3><div className="s3-upload-fields"><label>采集人<input required maxLength={80} value={collector} disabled={pending} onChange={event=>setCollector(event.target.value)} placeholder="例如 lixin"/></label><label>上传人（选填）<input value={uploader} maxLength={80} disabled={pending} onChange={event=>setUploader(event.target.value)} placeholder="默认使用当前账号姓名"/></label><label className="s3-upload-field-wide">备注（选填）<textarea value={note} maxLength={500} disabled={pending} onChange={event=>setNote(event.target.value)}/></label></div>
    <fieldset disabled={pending}><legend>姿势预标注</legend><label><input type="checkbox" checked={predict} disabled={!predictionReady||pending} onChange={event=>setPredict(event.target.checked)}/>上传前生成姿势预标注</label>{predict?<><p>请选择包含原视频和 IMU 的完整采集目录，并确认设备佩戴位置。</p><div className="s3-upload-fields"><label>胸部 IMU 设备编号<input required maxLength={120} value={chest} onChange={event=>setChest(event.target.value)}/></label><label>大腿 IMU 设备编号<input required maxLength={120} value={thigh} onChange={event=>setThigh(event.target.value)}/></label></div></>:<p>{predictionChecking?'正在检测预标注服务…':predictionError||(!predictionReady?'预标注服务未启用；仅上传素材及已有预测。':'保留并上传已有预测，不重新计算。')}</p>}</fieldset>
    <div className="s3-upload-destination"><span>保存目录</span><code>daily/{selection.capture_date}/{selection.capture_date.slice(4)+collector.trim()}/</code></div>
    <footer><span>压缩缓存同步保留至桌面 dataMarkProject。</span><button className="s3-upload-open" disabled={pending||predictionChecking}><Upload size={15}/>{pending?'正在处理…':'压缩并上传'}</button></footer>
   </form>}
  </>}
  {job&&<section className="s3-upload-card s3-upload-progress" aria-live="polite"><div className="s3-upload-section-heading"><h3>{uploadStateName(job.state)}</h3><span>{Math.min(100,Math.max(0,Math.round(job.progress)))}%</span></div><progress aria-label="压缩及上传进度" max={100} value={job.progress}/><p>{job.state==='ready'?job.prefix:job.detail}</p>{job.state==='ready'&&<><button type="button" className="s3-upload-secondary" onClick={()=>void copy(job.prefix,'采集目录已复制。')}>复制采集目录</button>{copied&&<p role="status">{copied}</p>}{job.prediction&&<p>{job.prediction.needs_alignment_review?'已携带姿势预标注；校时需要人工复核。':'已携带姿势预标注。'}</p>}<p>可在“素材与质检”导入该采集目录。</p></>}</section>}
  {(error||connectionError||helper?.report.error)&&<p role="alert" className="s3-upload-error">{error||connectionError||helper?.report.error}</p>}
  {pending&&<p className="s3-upload-footnote">关闭窗口后任务继续运行，可再次打开查看进度。</p>}
 </dialog>
 {success&&<SuccessDialog title="上传成功" message={`采集目录：${success}。可在“素材与质检”导入该目录。`} onClose={()=>setSuccess('')}/>}
 </>,document.fullscreenElement??document.body);
}

export default function S3UploadEntry(){
 const [open,setOpen]=useState(false);
 return <section className="s3-upload-entry"><div><h3>上传到 S3</h3><p>本机压缩后上传采集数据。</p></div><button className="s3-upload-open primary" onClick={()=>setOpen(true)}><Upload size={15}/>上传采集文件夹</button>{open&&<UploadDialog onClose={()=>setOpen(false)}/>}</section>;
}

export function S3HelperHandoff({canUpload}:{canUpload:boolean}){
 const [initial]=useState(()=>{try{return {handoff:takeHelperHandoff(location,url=>history.replaceState(history.state,'',url)),error:''};}catch(cause){return {handoff:null,error:cause instanceof Error?cause.message:'组件关联失败。'};}});
 const [open,setOpen]=useState(false),[error,setError]=useState(initial.error),started=useRef(false);
 useEffect(()=>{if(!canUpload||!initial.handoff||started.current)return;started.current=true;
  void bridgeRequest<HelperStatus>('helpers/pair',initial.handoff).then(value=>{pairedHelper=value.id;try{localStorage.setItem(helperStorageKey,value.id);}catch{}setOpen(true);}).catch(cause=>setError(cause instanceof Error?cause.message:'组件关联失败。'));
 },[canUpload,initial]);
 return <>{error&&<p role="alert" className="s3-upload-handoff-error">{error}</p>}{open&&<UploadDialog onClose={()=>setOpen(false)}/>}</>;
}
