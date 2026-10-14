import {useEffect, useState, type FormEvent} from 'react';
import {createPortal} from 'react-dom';
import ClaimProjectDialog, {type ClaimInspection, type ClaimMode} from '../ClaimProjectDialog';
import {taskRequest} from './TaskPanel';

type Source = {id:string};
type Capabilities = {backend:string;can_pick:boolean;can_browse:boolean};
type Listing = {path:string;parent:string|null;entries:Array<{name:string;path:string;kind:string}>;page?:number;has_more?:boolean;can_open?:boolean};
export default function TaskSourceImport({disabled,onImported}:{disabled:boolean;onImported:(sources:Source[])=>Promise<void>}) {
  const [capabilities,setCapabilities]=useState<Capabilities|null>(null);
  const [path,setPath]=useState(''),[name,setName]=useState(''),[paths,setPaths]=useState<string[]>([]);
  const [listing,setListing]=useState<Listing|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const [claim,setClaim]=useState<ClaimInspection|null>(null),[mode,setMode]=useState<ClaimMode>('keep'),[step,setStep]=useState<1|2>(1);
  useEffect(()=>{let alive=true;taskRequest<Capabilities>('/sources/capabilities').then(value=>{if(alive)setCapabilities(value);}).catch(cause=>{if(alive)setError(cause instanceof Error?cause.message:'素材导入服务不可用。');});return()=>{alive=false;}},[]);
  async function run(action:()=>Promise<void>) {setBusy(true);setError('');setNotice('');try{await action();}catch(cause){setError(cause instanceof Error?cause.message:'导入未成功。');}finally{setBusy(false);}}
  async function browse(location?:string,page=0) {await run(async()=>{const query=new URLSearchParams({page:String(page)});if(location)query.set('path',location);const result=await taskRequest<Listing>('/sources/browse?'+query);setListing(result);setPath(result.path);setPaths([]);});}
  async function pick(kind:'files'|'directory') {await run(async()=>{const result=await taskRequest<{paths:string[]}>('/sources/pick','POST',{kind});if(!result.paths.length)return;setListing(null);if(kind==='directory'){setPath(result.paths[0]);setPaths([]);}else{setPaths(result.paths);setPath('');}});}
  async function perform(selectedClaim?:ClaimInspection) {
    const body=paths.length?{paths,name:name.trim()}:{path:selectedClaim?.path??path.trim(),name:name.trim(),...(selectedClaim?{claim_mode:mode,claim_token:selectedClaim.claim_token,takeover_confirmations:selectedClaim.requires_takeover?2:0}:{})};
    const result=await taskRequest<{collection:string;sources:Source[]}>('/sources/import','POST',body);
    setClaim(null);await onImported(result.sources);
    setNotice(`已导入「${result.collection}」的 ${result.sources.length} 段视频。请登记采集信息和质检结论后分配任务。`);
    setPaths([]);setPath('');setListing(null);
  }
  function submit(event:FormEvent) {event.preventDefault();void run(async()=>{
    if(capabilities?.backend==='nas'&&!paths.length){const inspected=await taskRequest<ClaimInspection>('/sources/inspect','POST',{path:path.trim()});if(inspected.requires_choice){setClaim(inspected);setMode(inspected.has_json?'continue':'keep');setStep(1);return;}}
    await perform();
  });}
  return <section className="task-source-import"><h3>导入视频</h3><p>选择原视频或采集目录，原视频保留原处。导入后再登记视频编号、采集信息和质检结论。</p>
    <form onSubmit={submit}>
      <label>{capabilities?.backend==='s3-mock'?'模拟 S3 目录':'采集目录或视频完整路径'}<input value={path} disabled={disabled||busy||!capabilities} onChange={event=>{setPath(event.target.value);setPaths([]);}} placeholder={capabilities?.backend==='s3-mock'?'选择模拟 S3 项目目录':'填写当前服务可访问的原视频路径'}/></label>
      <label>采集名称（选填）<input value={name} maxLength={80} disabled={disabled||busy||!capabilities} onChange={event=>setName(event.target.value)} placeholder="默认使用采集目录名"/></label>
      {paths.length>0&&<p>已选择 {paths.length} 个原视频：{paths.map(value=>value.split(/[\\/]/).at(-1)).join('、')}</p>}
      <div className="task-source-actions">{capabilities?.can_pick&&<><button type="button" disabled={disabled||busy} onClick={()=>void pick('files')}>选择视频（可多选）</button><button type="button" disabled={disabled||busy} onClick={()=>void pick('directory')}>选择采集目录</button></>}{capabilities?.can_browse&&<button type="button" disabled={disabled||busy} onClick={()=>void browse()}>浏览采集目录</button>}</div>
      {listing&&<div className="task-source-listing"><p>{listing.path}</p>{listing.parent!==null&&<button type="button" disabled={disabled||busy} onClick={()=>void browse(listing.parent??undefined)}>上一级</button>}<div>{listing.entries.map(entry=>entry.kind==='directory'?<button type="button" key={entry.path} disabled={disabled||busy} onClick={()=>void browse(entry.path)}>{entry.name} /</button>:<span key={entry.path}>{entry.name}</span>)}</div>{listing.has_more&&<button type="button" disabled={disabled||busy} onClick={()=>void browse(listing.path,(listing.page??0)+1)}>下一页</button>}</div>}
      {error&&<p className="task-error" role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
      <button disabled={disabled||busy||!capabilities||(!paths.length&&!path.trim())}>{busy?'正在导入…':'导入视频'}</button>
    </form>
    {claim&&createPortal(<div className="task-source-claim"><ClaimProjectDialog claim={claim} mode={mode} step={step} busy={busy} error={error} onMode={setMode} onBack={()=>setStep(1)} onCancel={()=>setClaim(null)} onNext={()=>{if(claim.requires_takeover&&step===1){setStep(2);return;}void run(()=>perform(claim));}}/></div>,document.body)}
  </section>;
}
