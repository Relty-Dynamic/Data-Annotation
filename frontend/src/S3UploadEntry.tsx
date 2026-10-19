import {useEffect,useState,useRef,type FormEvent} from 'react';
import {createPortal} from 'react-dom';
import {apiOrigin} from './apiOrigin';
import {authFetch} from './auth';
import {bridgeRequest,helperStorageKey,installCommands,offlineInstallCommands,takeHelperHandoff,type HelperStatus,type UploaderPackages} from './uploadBridge';
import './s3Upload.css';
import {CheckCircle2,FolderOpen,Upload,RefreshCw,X,Film,Activity} from 'lucide-react';
import SuccessDialog from './tasks/SuccessDialog';
import {claimUploadCompletion,uploadStateName,predictionMessage,predictionFailureReason} from './uploadFeedback';

let pairedHelper='';
const notifiedUploads=new Set<string>();
function readId(){try{return pairedHelper||localStorage.getItem(helperStorageKey)||'';}catch{return pairedHelper;}}
export function UploadDialog({onClose}:{onClose:()=>void}){
 const [helper,setHelper]=useState<HelperStatus|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[checking,setChecking]=useState(true),[copied,setCopied]=useState('');
 const [backendReady,setBackendReady]=useState<boolean|null>(null),[connectionError,setConnectionError]=useState('');
 const [collector,setCollector]=useState(''),[uploader,setUploader]=useState(''),[note,setNote]=useState('');
 const dialogRef=useRef<HTMLDialogElement>(null),mounted=useRef(true);
 const [success,setSuccess]=useState('');
 const [packages,setPackages]=useState<UploaderPackages|null>(null),[packageError,setPackageError]=useState('');
 useEffect(()=>{let alive=true;void authFetch('/api/r2/uploader/packages').then(async response=>{if(!response.ok)throw new Error('完整安装包暂不可用，请稍后重试。');const value=await response.json();if(alive)setPackages(value);}).catch(cause=>{if(alive)setPackageError(cause.message);});return()=>{alive=false;};},[]);
 const [predictionChecking,setPredictionChecking]=useState(true);
 const [predictionReady,setPredictionReady]=useState(false),[predict,setPredict]=useState(false),[predictionError,setPredictionError]=useState('');
 const [chest,setChest]=useState(''),[thigh,setThigh]=useState('');
 const [mode,setMode]=useState<'video'|'imu'>('video'),[captureDate,setCaptureDate]=useState(''),[imuReady,setImuReady]=useState(false),[successKind,setSuccessKind]=useState('video');
 useEffect(()=>{let alive=true;void authFetch('/api/prediction/capabilities').then(async response=>{if(!response.ok)throw new Error('预标注服务暂不可用，可仅上传素材。');const value=await response.json();if(alive){setPredictionReady(value.enabled===true);setPredict(value.enabled===true);setClockGroupSupported(value.same_day_clock_group_supported===true);}}).catch(cause=>{if(alive)setPredictionError(cause instanceof Error?cause.message:'预标注服务暂不可用。');}).finally(()=>{if(alive)setPredictionChecking(false);});return()=>{alive=false;};},[]);
 const initialMode=useRef(false);
 const [continuousClocks,setContinuousClocks]=useState(false),[clockGroupSupported,setClockGroupSupported]=useState(false),[successPrediction,setSuccessPrediction]=useState('');
 const commands=installCommands(apiOrigin||location.origin,typeof navigator==='undefined'?'':navigator.userAgent);
 const offline=offlineInstallCommands(apiOrigin||location.origin,location.origin,typeof navigator==='undefined'?'':navigator.userAgent);
 async function check(){const id=readId();if(!id){setHelper(null);setChecking(false);return;}try{const value=await bridgeRequest<HelperStatus>('helpers/'+id);if(!mounted.current)return;setHelper(value);if(!initialMode.current){initialMode.current=true;setMode(value.report.selection?.upload_kind??value.report.job?.upload_kind??'video');}setConnectionError('');setChecking(false);}catch(cause){if(!mounted.current)return;setHelper(null);setChecking(false);setConnectionError(cause instanceof Error?cause.message:'组件检测失败。');}}
 useEffect(()=>{mounted.current=true;let alive=true,ready=false;void authFetch('/api/health').then(async response=>{if(!response.ok)throw new Error('无法连接公网上传服务。');const health=await response.json();if(!alive)return;ready=health.capabilities?.includes('s3-helper-bridge')===true;setBackendReady(ready);setImuReady(health.capabilities?.includes('s3-imu-upload-v1')===true);if(ready)void check();else setChecking(false);}).catch(()=>{if(alive){setBackendReady(false);setChecking(false);setConnectionError('无法连接公网上传服务，请检查网络后刷新。');}});const timer=setInterval(()=>{if(alive&&ready)void check();},2000);return()=>{alive=false;mounted.current=false;clearInterval(timer);};},[]);
 useEffect(()=>{const dialog=dialogRef.current!;dialog.showModal();return()=>dialog.close()},[]);
 useEffect(()=>{if(busy)return;const completion=claimUploadCompletion(helper,notifiedUploads);if(completion){setSuccess(completion.prefix);setSuccessKind(helper?.report.job?.upload_kind??'video');setSuccessPrediction(predictionMessage(helper?.report.job?.prediction));}},[helper,busy]);
 async function command(action:'select'|'start',event?:FormEvent){event?.preventDefault();if(!helper)return;setBusy(true);setError('');try{setHelper(await bridgeRequest<HelperStatus>('helpers/'+helper.id+'/commands',{action,fields:action==='start'?{collector:collector.trim(),uploader:uploader.trim(),note:note.trim(),prediction:mode==='video'&&predict?'generate':'keep',chest_device:chest.trim(),thigh_device:thigh.trim(),...(mode==='video'&&predict&&continuousClocks&&clockGroupSupported?{clock_group:'confirmed-current-collection'}:{}),...(mode==='imu'?{upload_kind:'imu',capture_date:captureDate.replaceAll('-','')}:{})}:mode==='imu'?{upload_kind:'imu'}:{}}));}catch(cause){setError(cause instanceof Error?cause.message:'操作未完成。');}finally{setBusy(false);}}
 async function copy(value:string,message='已复制，请在本机终端粘贴运行。'){try{await navigator.clipboard.writeText(value);setCopied(message);}catch{setCopied('请手动选中并复制命令。');}}
 const pending=busy||!!helper?.command||!!helper?.report.busy;
 const selected=helper?.report.selection;
 const selection=selected&&(selected.upload_kind??'video')===mode?selected:undefined;
 const currentJob=helper?.report.job,job=currentJob&&(currentJob.upload_kind??'video')===mode?currentJob:undefined;
 const date=mode==='imu'?captureDate.replaceAll('-',''):selection?.capture_date??'';
 return createPortal(<>
 <dialog ref={dialogRef} className="s3-upload-dialog" aria-labelledby="s3-upload-title" aria-modal="true" data-s3-upload-dialog onCancel={event=>{event.preventDefault();onClose();}}>
  <header className="s3-upload-heading"><div><span className="s3-upload-eyebrow">采集数据上传</span><h2 id="s3-upload-title">选择素材，开始上传</h2><p>FPV 在本机压缩；IMU 原样保存。上传完成后统一核验。</p></div><button type="button" className="s3-upload-icon" aria-label="关闭上传" onClick={onClose}><X size={19}/></button></header>
  {checking?<p className="s3-upload-message" role="status">正在检测本机组件…</p>:backendReady===false?<p className="s3-upload-error" role="alert">上传服务暂不可用，请稍后刷新重试。</p>:(!helper?.online||helper.update_available&&!pending)?<section className="s3-upload-card s3-upload-install"><h3>{helper?.update_available?'更新本机上传组件':'连接本机上传组件'}</h3><p>{helper?.update_available?'当前组件有新版本，请先关闭旧组件终端，再运行下方命令更新。':''}首次使用或组件未启动时，展开对应系统并复制命令到终端运行。依赖由 DataMark 统一下载，无需 VPN；运行后保持终端开启。</p><details open={navigator.userAgent.includes('Mac')}><summary>macOS 安装／启动</summary><label><code>{commands.mac}</code><button type="button" className="s3-upload-secondary" onClick={()=>void copy(commands.mac)}>复制 macOS 命令</button></label></details><details open={navigator.userAgent.includes('Windows')}><summary>Windows PowerShell 安装／启动</summary><label><code>{commands.windows}</code><button type="button" className="s3-upload-secondary" onClick={()=>void copy(commands.windows)}>复制 Windows 命令</button></label></details>{packageError&&<p role="alert">{packageError}</p>}{packages&&!packages.ready&&<p role="status">部分系统的完整安装包尚未准备好，请联系管理员。</p>}{packages&&Object.keys(packages.platforms).length>0&&<details><summary>下载完整离线安装包</summary><p>可在其他电脑下载后拷贝使用。按系统选择，解压后在该目录打开终端，执行下方命令；安装无需联网，上传仍需连接平台。</p>{Object.entries(packages.platforms).map(([target,item])=><p key={target}><a href={item.url} download={item.file}>{({'macos-arm64':'Mac Apple Silicon','macos-x64':'Mac Intel','windows-x64':'Windows x64'} as Record<string,string>)[target]??target} · {Math.ceil(item.size/1024/1024)} MB</a> · <a href={item.url+'.sha256'}>校验值</a></p>)}<label>macOS<code>{offline.mac}</code><button type="button" className="s3-upload-secondary" onClick={()=>void copy(offline.mac)}>复制离线安装命令</button></label><label>Windows PowerShell<code>{offline.windows}</code><button type="button" className="s3-upload-secondary" onClick={()=>void copy(offline.windows)}>复制离线安装命令</button></label></details>}{copied&&<p role="status">{copied}</p>}<button type="button" className="s3-upload-secondary" onClick={()=>void check()}><RefreshCw size={14}/>重新检测</button></section>:<>
   <div className="s3-upload-modes" role="group" aria-label="上传素材类型">
    <button type="button" aria-pressed={mode==='video'} disabled={pending} onClick={()=>setMode('video')}><Film size={21}/><span><strong>FPV / 完整采集</strong><small>仅视频，或视频与 IMU 一起上传</small></span></button>
    <button type="button" aria-pressed={mode==='imu'} disabled={pending||!imuReady} onClick={()=>setMode('imu')}><Activity size={21}/><span><strong>仅 IMU</strong><small>{imuReady?'独立上传，无需视频':'上传服务更新后可用'}</small></span></button>
   </div>
   <div className={'s3-upload-workspace'+(selection&&job?.state!=='ready'?' has-selection':'')}>
   <section className="s3-upload-card s3-upload-materials"><div className="s3-upload-section-heading"><h3><span className="s3-upload-step">1</span>选择素材</h3><span className="s3-upload-ready"><CheckCircle2 size={13}/>组件已连接</span></div>
    <button type="button" className="s3-upload-select" disabled={pending} onClick={()=>void command('select')}><FolderOpen size={19}/><span>{selection?selection.name:mode==='imu'?'选择本机 IMU 文件夹':'选择本机采集文件夹'}<small>{pending&&helper.command?.action==='select'?'请在系统窗口选择文件夹…':selection?mode==='imu'?`${selection.attachments.length} 个 IMU 文件`:`${selection.capture_date} · ${selection.videos.length} 段视频 · ${selection.attachments.length} 个附属文件`:mode==='imu'?'支持直接选择 IMU 文件夹，或包含 IMU 的采集根目录':'选择 FPV、video 或完整采集目录'}</small></span></button>
    {selection&&<details className="s3-upload-files"><summary>文件清单{selection.ignored.length>0?` · ${selection.ignored.length} 项不参与上传`:''}</summary><pre>{[...(selection.videos.length?['视频',...selection.videos]:[]),...(selection.attachments.length?['',mode==='imu'?'IMU 数据':'附属数据',...selection.attachments]:[]),...(selection.ignored.length?['','不上传',...selection.ignored]:[])].join('\n')}</pre></details>}
    <p className="s3-upload-hint">{mode==='imu'?'IMU 文件按原始内容保存，不压缩、不生成姿势预标注。':'只传 FPV 时选择视频文件夹并取消预处理；两类一起传时选择采集父文件夹。'}</p>
   </section>
   {selection&&job?.state!=='ready'&&<form className="s3-upload-card s3-upload-form" onSubmit={event=>void command('start',event)}><h3><span className="s3-upload-step">2</span>填写采集信息</h3><div className="s3-upload-fields">{mode==='imu'&&<label className="s3-upload-field-wide">采集日期<input required type="date" value={captureDate} disabled={pending} onChange={event=>setCaptureDate(event.target.value)}/><small>填写数据实际采集日期，不按上传时间归档。</small></label>}<label>采集人<input required maxLength={80} value={collector} disabled={pending} onChange={event=>setCollector(event.target.value)} placeholder="例如 lixin"/></label><label>上传人（选填）<input value={uploader} maxLength={80} disabled={pending} onChange={event=>setUploader(event.target.value)} placeholder="默认使用当前账号姓名"/></label><details className="s3-upload-field-wide s3-upload-optional"><summary>添加备注（选填）</summary><label>备注<textarea value={note} maxLength={500} disabled={pending} onChange={event=>setNote(event.target.value)}/></label></details></div>
    {mode==='video'&&<fieldset className="s3-upload-prediction" disabled={pending}><legend>可选预处理</legend><label className="s3-upload-toggle"><input type="checkbox" checked={predict} disabled={!predictionReady||pending} onChange={event=>setPredict(event.target.checked)}/>上传前生成姿势预标注</label>{predict?<><p>请选择包含原视频和 IMU 的完整采集目录，并确认设备佩戴位置。</p><div className="s3-upload-fields"><label>胸部 IMU 设备编号<input required maxLength={120} value={chest} onChange={event=>setChest(event.target.value)}/></label><label>大腿 IMU 设备编号<input required maxLength={120} value={thigh} onChange={event=>setThigh(event.target.value)}/></label></div><label className="s3-upload-toggle"><input type="checkbox" checked={continuousClocks} disabled={!clockGroupSupported||pending} onChange={event=>setContinuousClocks(event.target.checked)}/>确认本次胸部设备与相机的时钟连续，允许短记录参考同日其他记录校时</label><p>{clockGroupSupported?'仅在本次所有记录期间均未重启、换机或调时后勾选；借用的校时结果仍需人工复核。':'当前算法服务尚不支持跨记录校时，各记录独立处理。'}</p></>:<p>{predictionChecking?'正在检测预标注服务…':predictionError||(!predictionReady?'预标注服务未启用；仅上传素材及已有预测。':'保留并上传已有预测，不重新计算。')}</p>}</fieldset>}
    <div className="s3-upload-destination"><span>保存目录</span><code>{date&&collector.trim()?`daily/${date}/${date.slice(4)+collector.trim()}/`:'填写采集信息后显示'}</code></div>
    <footer><span>{mode==='imu'?'同一采集日期和采集人归入同一目录，可补齐 FPV 或 IMU。':'压缩缓存保留至桌面 dataMarkProject。'}</span><button className="s3-upload-open" disabled={pending||(mode==='video'&&predictionChecking)}><Upload size={15}/>{pending?'正在处理…':mode==='imu'?'上传 IMU':'压缩并上传'}</button></footer>
   </form>}
   </div>
  </>}
  {job&&<section className="s3-upload-card s3-upload-progress" aria-live="polite"><div className="s3-upload-section-heading"><h3>{uploadStateName(job.state)}</h3><span>{Math.min(100,Math.max(0,Math.round(job.progress)))}%</span></div><progress aria-label="素材上传进度" max={100} value={job.progress}/>{job.clips_total!==undefined&&<p>整组视频已上传核验：{job.clips_verified??0}/{job.clips_total} 段</p>}{job.prediction_detail&&job.state!=='ready'&&<p>{job.prediction_detail}</p>}<p>{job.state==='ready'?job.prefix:job.detail}</p>{job.state==='ready'&&<><button type="button" className="s3-upload-secondary" onClick={()=>void copy(job.prefix,'采集目录已复制。')}>复制采集目录</button>{copied&&<p role="status">{copied}</p>}{job.prediction&&<><p>{predictionMessage(job.prediction)}</p>{!!job.prediction.excluded_records?.length&&<details><summary>未完成预标注的记录（{job.prediction.excluded_records.length}）</summary><ul>{job.prediction.excluded_records.map(row=><li key={row.record_id}>{row.path}：{predictionFailureReason(row.reason)}</li>)}</ul></details>}{job.prediction.job_id&&<details><summary>预标注诊断编号</summary><code>{job.prediction.job_id}</code></details>}</>}<p>{job.upload_kind==='imu'?'IMU 已保存；仅含 IMU 的目录不进入视频标注。':'可在“素材与质检”导入该采集目录。'}</p></>}</section>}
  {(error||connectionError||helper?.report.error)&&<p role="alert" className="s3-upload-error">{error||connectionError||helper?.report.error}</p>}
  {pending&&<p className="s3-upload-footnote">关闭窗口后任务继续运行，可再次打开查看进度。</p>}
 </dialog>
 {success&&<SuccessDialog title="上传成功" message={`采集目录：${success}。${successKind==='imu'?'IMU 已原样保存。':'可在“素材与质检”导入该目录。'+successPrediction}`} onClose={()=>setSuccess('')}/>}
 </>,document.fullscreenElement??document.body);
}

export default function S3UploadEntry(){
 const [open,setOpen]=useState(false);
 return <section className="s3-upload-entry"><div><h3>上传到 S3</h3><p>支持 FPV、IMU 分开或一起上传。</p></div><button className="s3-upload-open primary" onClick={()=>setOpen(true)}><Upload size={15}/>上传采集文件夹</button>{open&&<UploadDialog onClose={()=>setOpen(false)}/>}</section>;
}

export function S3HelperHandoff({canUpload}:{canUpload:boolean}){
 const [initial]=useState(()=>{try{return {handoff:takeHelperHandoff(location,url=>history.replaceState(history.state,'',url)),error:''};}catch(cause){return {handoff:null,error:cause instanceof Error?cause.message:'组件关联失败。'};}});
 const [open,setOpen]=useState(false),[error,setError]=useState(initial.error),started=useRef(false);
 useEffect(()=>{if(!canUpload||!initial.handoff||started.current)return;started.current=true;
  void bridgeRequest<HelperStatus>('helpers/pair',initial.handoff).then(value=>{pairedHelper=value.id;try{localStorage.setItem(helperStorageKey,value.id);}catch{}setOpen(true);}).catch(cause=>setError(cause instanceof Error?cause.message:'组件关联失败。'));
 },[canUpload,initial]);
 return <>{error&&<p role="alert" className="s3-upload-handoff-error">{error}</p>}{open&&<UploadDialog onClose={()=>setOpen(false)}/>}</>;
}
