import {authFetch} from './auth.ts';
import {apiOrigin} from './apiOrigin.ts';

export const helperStorageKey='datamark.public-upload.helper.'+apiOrigin;
export type UploadReport={busy?:boolean;error?:string;selection?:{name:string;capture_date:string;videos:string[];attachments:string[];ignored:string[]};job?:{id?:string;state:string;prefix:string;progress:number;detail:string}};
export type HelperStatus={id:string;online:boolean;platform:string;command:{id:string;action:string}|null;report:UploadReport};
export async function bridgeRequest<T>(path:string,body?:unknown):Promise<T>{
 const response=await authFetch('/api/r2/'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?undefined:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
 let value;
 try{value=JSON.parse(await response.text());}catch{throw new Error(`上传服务响应异常（HTTP ${response.status}），请稍后重试。`);}
 if(!response.ok)throw new Error(typeof value?.detail==='string'?value.detail:'上传服务暂不可用。');
 return value as T;
}
export function installCommands(origin:string){
 const url=new URL(origin);
 if(url.protocol!=='https:'||url.origin!==origin||url.username||url.password||!/^[-a-z0-9.]+$/.test(url.hostname))throw new Error('安装地址必须是平台的 HTTPS 地址。');
 return {mac:`curl -fsSL '${origin}/api/r2/uploader/install.sh' | sh`,windows:`irm '${origin}/api/r2/uploader/install.ps1' | iex`};
}
export function takeHelperHandoff(location:{hash:string;pathname:string;search:string},replace:(url:string)=>void){
 if(!location.hash.startsWith('#datamark-upload='))return null;
 const raw=location.hash.slice('#datamark-upload='.length);
 replace(location.pathname+location.search);
 const match=/^([a-f0-9]{32}):([A-Za-z0-9_-]{30,100})$/.exec(raw);
 if(!match)throw new Error('本机组件关联地址无效，请重新启动组件。');
 return {id:match[1],claim:match[2]};
}
