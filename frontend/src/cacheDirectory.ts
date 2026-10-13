type Directory = FileSystemDirectoryHandle & {
  queryPermission?: (options:{mode:'readwrite'})=>Promise<PermissionState>;
  requestPermission?: (options:{mode:'readwrite'})=>Promise<PermissionState>;
};
let selected: Directory | null = null;
const DB='datamark-desktop-cache-settings-v1';
async function setting(key:string, value?:unknown):Promise<unknown> {
  if(typeof indexedDB==='undefined')return undefined;
  const db=await new Promise<IDBDatabase>((resolve,reject)=>{
    const request=indexedDB.open(DB,1);
    request.onupgradeneeded=()=>request.result.createObjectStore('settings');
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
  });
  try{return await new Promise((resolve,reject)=>{
    const transaction=db.transaction('settings',value===undefined?'readonly':'readwrite');
    const store=transaction.objectStore('settings'),request=value===undefined?store.get(key):store.put(value,key);
    transaction.oncomplete=()=>resolve(request.result);transaction.onerror=()=>reject(transaction.error);transaction.onabort=()=>reject(transaction.error);
  });}finally{db.close();}
}
export async function rememberCacheDirectory(directory:Directory){await setting('directory',directory);selected=directory;}
export async function chooseCacheDirectory():Promise<string>{
  const picker=(globalThis as unknown as {showDirectoryPicker?: (options:unknown)=>Promise<Directory>}).showDirectoryPicker;
  if(!picker)throw new Error('此浏览器不支持保存到指定目录，请使用支持目录读写的 Chrome / Edge，或使用本机服务。');
  let directory=await picker({id:'datamark-cache',startIn:'desktop',mode:'readwrite'});
  if(['Desktop','桌面'].includes(directory.name))directory=await directory.getDirectoryHandle('dataMarkProject',{create:true}) as Directory;
  await rememberCacheDirectory(directory);return directory.name;
}
export async function cacheDirectory():Promise<Directory>{
  if(!selected)selected=await setting('directory') as Directory|null;
  if(!selected)throw new Error('请点击“缓存位置”，选择并授权桌面的 dataMarkProject 文件夹，再重试准备。');
  if(selected.queryPermission&&await selected.queryPermission({mode:'readwrite'})!=='granted')throw new Error('缓存文件夹权限已失效，请点击“缓存位置”重新授权，再重试。');
  return selected;
}
export async function savedCacheIndex(projectId:string,value?:unknown){return setting('project:'+projectId,value);}
export function forgetSelectedCacheDirectory(){selected=null;}
