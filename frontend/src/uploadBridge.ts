import {authFetch} from './auth.ts';
import {apiOrigin} from './apiOrigin.ts';

export const helperStorageKey='datamark.public-upload.helper.'+apiOrigin;
export type UploadReport={busy?:boolean;error?:string;selection?:{upload_kind?:'video'|'imu';name:string;capture_date:string;videos:string[];attachments:string[];ignored:string[]};job?:{upload_kind?:'video'|'imu';id?:string;state:string;prefix:string;progress:number;detail:string;clips_total?:number;clips_verified?:number;prediction_detail?:string;prediction?:{sha256:string;segments:number;needs_alignment_review:boolean}}};
export type HelperStatus={id:string;online:boolean;update_available?:boolean;platform:string;command:{id:string;action:string}|null;report:UploadReport};
export async function bridgeRequest<T>(path:string,body?:unknown):Promise<T>{
 const response=await authFetch('/api/r2/'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?undefined:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 let value;
 try{value=JSON.parse(await response.text());}catch{throw new Error(`上传服务响应异常（HTTP ${response.status}），请稍后重试。`);}
 if(!response.ok)throw new Error(typeof value?.detail==='string'?value.detail:'上传服务暂不可用。');
 return value as T;
}
export function uploadBrowser(userAgent:string){
 if(/Edg\//.test(userAgent))return 'edge';
 if(/Chrome\//.test(userAgent))return 'chrome';
 if(/Firefox\//.test(userAgent))return 'firefox';
 if(/Safari\//.test(userAgent))return 'safari';
 return '';
}
export function installCommands(origin:string,userAgent=''){
 const browser=uploadBrowser(userAgent),query=browser?'?browser='+browser:'';
 const url=new URL(origin);
 if(url.protocol!=='https:'||url.origin!==origin||url.username||url.password||!/^[-a-z0-9.]+$/.test(url.hostname))throw new Error('安装地址必须是平台的 HTTPS 地址。');
 return {mac:`curl --noproxy '*' -fsSL '${origin}/api/r2/uploader/install.sh${query}' | sh`,windows:`$dataMarkInstallScript = & curl.exe --noproxy '*' -fsSL '${origin}/api/r2/uploader/install.ps1${query}'; if ($LASTEXITCODE -ne 0) { throw '安装脚本下载失败，请检查直连网络。' }; & ([scriptblock]::Create(($dataMarkInstallScript -join [Environment]::NewLine)))`};
}
export function takeHelperHandoff(location:{hash:string;pathname:string;search:string},replace:(url:string)=>void){
 if(!location.hash.startsWith('#datamark-upload='))return null;
 const raw=location.hash.slice('#datamark-upload='.length);
 replace(location.pathname+location.search);
 const match=/^([a-f0-9]{32}):([A-Za-z0-9_-]{30,100})$/.exec(raw);
 if(!match)throw new Error('本机组件关联地址无效，请重新启动组件。');
 return {id:match[1],claim:match[2]};
}

export type UploaderPackages={ready:boolean;platforms:Record<string,{file:string;sha256:string;size:number;url:string}>};
export function offlineInstallCommands(origin:string,frontend:string,userAgent=''){
 installCommands(origin,userAgent);installCommands(frontend);
 const browser=uploadBrowser(userAgent),argument=browser?` --browser '${browser}'`:'';
 return {mac:`sh ./install.command --api '${origin}' --frontend '${frontend}'${argument}`,windows:`$DataMarkKit=(Get-Location).Path; & ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path $DataMarkKit 'install.ps1')))) -api '${origin}' -frontend '${frontend}'${argument.replace('--browser','-browser')}`};
}
