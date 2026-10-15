import type {TaskRole} from './types';

export function taskTabs(platformRole:string,roles:TaskRole[],canUpload:boolean):string[][]{
 const manager=platformRole==='admin'||roles.includes('leader');
 return [['tasks',manager?'任务列表':'我的任务'],
  ...(canUpload||manager?[['videos','素材与质检']]:[]),
  ...(manager?[['assign','任务分配']]:[]),
  ...(platformRole==='admin'||roles.length?[['rules','标注规则']]:[]),
  ...(manager?[['delivery','验收交付']]:[])];
}

export async function loadTaskPanelData(roles:TaskRole[],canUpload:boolean,read:(path:string)=>Promise<unknown>,receive:(path:string,value:unknown)=>void,sourceFailed:(error:unknown)=>void){
 const manager=roles.includes('leader');
 const paths=['/items',...(roles.length?['/rules']:[]),...(manager?['/users','/roles','/deliveries']:[]),
  ...(canUpload?['/sources']:[]),...(manager||canUpload?['/videos']:[])];
 const results=await Promise.allSettled(paths.map(async path=>{
  try{receive(path,await read(path))}
  catch(error){if(path==='/sources')sourceFailed(error);else throw error;}
 }));
 const failed=results.find(result=>result.status==='rejected');
 if(failed?.status==='rejected')throw failed.reason;
}
