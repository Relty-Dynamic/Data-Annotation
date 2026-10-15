import {useEffect,useState,useRef,type FormEvent} from 'react';
import {createPortal} from 'react-dom';
import {apiOrigin} from './apiOrigin';
import {authFetch} from './auth';
import {bridgeRequest,helperStorageKey,installCommands,takeHelperHandoff,type HelperStatus} from './uploadBridge';
import './s3Upload.css';

let pairedHelper='';
function readId(){try{return pairedHelper||localStorage.getItem(helperStorageKey)||'';}catch{return pairedHelper;}}
function UploadDialog({onClose}:{onClose:()=>void}){
 const [helper,setHelper]=useState<HelperStatus|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[checking,setChecking]=useState(true),[copied,setCopied]=useState('');
 const [backendReady,setBackendReady]=useState<boolean|null>(null),[connectionError,setConnectionError]=useState('');
 const [collector,setCollector]=useState(''),[uploader,setUploader]=useState(''),[note,setNote]=useState('');
 const dialogRef=useRef<HTMLElement>(null),closeRef=useRef(onClose);closeRef.current=onClose;
 const commands=installCommands(apiOrigin||location.origin);
 async function check(){const id=readId();if(!id){setHelper(null);setChecking(false);return;}try{setHelper(await bridgeRequest<HelperStatus>('helpers/'+id));setConnectionError('');setChecking(false);}catch(cause){setHelper(null);setChecking(false);setConnectionError(cause instanceof Error?cause.message:'组件检测失败。');}}
 useEffect(()=>{let alive=true,ready=false;void authFetch('/api/health').then(async response=>{if(!response.ok)throw new Error('无法连接公网上传服务。');const health=await response.json();if(!alive)return;ready=health.capabilities?.includes('s3-helper-bridge')===true;setBackendReady(ready);if(ready)void check();else setChecking(false);}).catch(()=>{if(alive){setBackendReady(false);setChecking(false);setConnectionError('无法连接公网上传服务，请检查网络后刷新。');}});const timer=setInterval(()=>{if(alive&&ready)void check();},2000);return()=>{alive=false;clearInterval(timer);};},[]);
 useEffect(()=>{const previous=document.activeElement,dialog=dialogRef.current;dialog?.focus();const key=(event:KeyboardEvent)=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeRef.current();return;}if(event.key!=='Tab'||!dialog)return;const controls=Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),summary'));const first=controls[0],last=controls[controls.length-1];if(!first){event.preventDefault();return;}if(event.shiftKey&&(document.activeElement===first||document.activeElement===dialog)){event.preventDefault();last.focus();}else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===dialog)){event.preventDefault();first.focus();}};window.addEventListener('keydown',key,true);return()=>{window.removeEventListener('keydown',key,true);if(previous instanceof HTMLElement&&previous.isConnected)previous.focus({preventScroll:true});};},[]);
 async function command(action:'select'|'start',event?:FormEvent){event?.preventDefault();if(!helper)return;setBusy(true);setError('');try{setHelper(await bridgeRequest<HelperStatus>('helpers/'+helper.id+'/commands',{action,fields:action==='start'?{collector:collector.trim(),uploader:uploader.trim(),note:note.trim()}:{}}));}catch(cause){setError(cause instanceof Error?cause.message:'操作未完成。');}finally{setBusy(false);}}
 async function copy(value:string){try{await navigator.clipboard.writeText(value);setCopied('已复制，请在本机终端粘贴运行。');}catch{setCopied('请手动选中并复制命令。');}}
 const pending=busy||!!helper?.command||!!helper?.report.busy;
 const selection=helper?.report.selection,job=helper?.report.job;
 return createPortal(<div className="s3-upload-backdrop" onClick={onClose}><section ref={dialogRef} tabIndex={-1} className="s3-upload-dialog" role="dialog" aria-modal="true" aria-labelledby="s3-upload-title" data-s3-upload-dialog onClick={event=>event.stopPropagation()}>
  <header><div><small>DataMark · 公网 S3</small><h2 id="s3-upload-title">上传采集文件夹</h2></div><button aria-label="关闭上传" onClick={onClose}>×</button></header>
  <p>在本机压缩后直传 S3。原视频保留原处，压缩缓存同时保留到桌面 dataMarkProject。</p>
  {checking?<p role="status">正在检测本机压缩组件…</p>:backendReady===false?<p role="alert">公网后端尚未就绪或尚未更新到同页上传版本，请稍后刷新页面。</p>:!helper?.online?<section className="s3-upload-install"><h3>未检测到正在运行的本机组件</h3><p>组件可能尚未安装、尚未启动或网络中断。首次使用或组件已关闭时，复制对应命令到这台电脑的终端运行。命令会安装／更新 Python、依赖和 FFmpeg，并启动组件；不需要管理员权限，不修改系统 PATH。</p><label>macOS<code>{commands.mac}</code><button onClick={()=>void copy(commands.mac)}>复制 macOS 命令</button></label><label>Windows PowerShell<code>{commands.windows}</code><button onClick={()=>void copy(commands.windows)}>复制 Windows 命令</button></label><p role="status">{copied}</p><p>安装完成会打开原 DataMark 页面，自动关联当前登录账号。请保持组件终端开启；无需进入另一套上传页面或重新输入密码。</p><button onClick={()=>void check()}>重新检测</button></section>:<>
   <p className="s3-upload-ready">本机组件已连接 · {helper.platform==='darwin'?'macOS':'Windows'}</p>
   <button className="s3-upload-open" disabled={pending} onClick={()=>void command('select')}>{pending&&helper.command?.action==='select'?'请在系统窗口选择文件夹…':'选择本机采集文件夹'}</button>
   {selection&&<><p>{selection.name} · {selection.capture_date} · {selection.videos.length} 个视频 · {selection.attachments.length} 个附属文件</p><details><summary>查看上传清单与排除项</summary><pre>{['FPV（压缩后上传）',...selection.videos,'','IMU／HEART／prediction（原样上传）',...selection.attachments,'','不上传',...selection.ignored].join('\n')}</pre></details><form onSubmit={event=>void command('start',event)}><label>采集人<input required maxLength={80} value={collector} disabled={pending} onChange={event=>setCollector(event.target.value)} placeholder="例如 lixin"/></label><label>上传人（选填）<input value={uploader} maxLength={80} disabled={pending} onChange={event=>setUploader(event.target.value)} placeholder="默认使用当前登录账号姓名"/></label><label>备注（选填）<textarea value={note} maxLength={500} disabled={pending} onChange={event=>setNote(event.target.value)}/></label><p>S3 目标：daily/{selection.capture_date}/{selection.capture_date.slice(4)+collector.trim()}/</p><button className="s3-upload-open" disabled={pending}>{pending?'正在处理…':'压缩并上传'}</button></form></>}
  </>}
  {job&&<section aria-live="polite"><h3>上传进度</h3><p>{job.prefix}</p><progress max={100} value={job.progress}/><p>{job.detail}</p>{job.state==='ready'&&<p>上传完成。现在可以导入此 S3 采集目录，登记、质检后分配任务。</p>}</section>}
  {(error||connectionError||helper?.report.error)&&<p role="alert" className="import-error">{error||connectionError||helper?.report.error}</p>}
  <p>关闭此弹窗不会取消本机任务；再次点击上传可查看进度。timeline 由标注输出生成，已有本机时间轴不上传。</p>
 </section></div>,document.body);
}

export default function S3UploadEntry(){
 const [open,setOpen]=useState(false);
 return <section className="s3-upload-entry"><h3>上传到 S3</h3><p>选择本机采集文件夹，自动压缩视频并上传 FPV、IMU、HEART 和 prediction。</p><button className="s3-upload-open" onClick={()=>setOpen(true)}>上传采集文件夹</button>{open&&<UploadDialog onClose={()=>setOpen(false)}/>}</section>;
}

export function S3HelperHandoff({canUpload}:{canUpload:boolean}){
 const [initial]=useState(()=>{try{return {handoff:takeHelperHandoff(location,url=>history.replaceState(history.state,'',url)),error:''};}catch(cause){return {handoff:null,error:cause instanceof Error?cause.message:'组件关联失败。'};}});
 const [open,setOpen]=useState(false),[error,setError]=useState(initial.error),started=useRef(false);
 useEffect(()=>{if(!canUpload||!initial.handoff||started.current)return;started.current=true;
  void bridgeRequest<HelperStatus>('helpers/pair',initial.handoff).then(value=>{pairedHelper=value.id;try{localStorage.setItem(helperStorageKey,value.id);}catch{}setOpen(true);}).catch(cause=>setError(cause instanceof Error?cause.message:'组件关联失败。'));
 },[canUpload,initial]);
 return <>{error&&<p role="alert" className="s3-upload-handoff-error">{error}</p>}{open&&<UploadDialog onClose={()=>setOpen(false)}/>}</>;
}
