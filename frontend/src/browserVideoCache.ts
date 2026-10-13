import {apiUrl} from './apiOrigin.ts';
import type {SessionManifest, DesktopAsset} from './sessionAssets.ts';
import {fetchAsset, validAssetUrl} from './objectAsset.ts';
import {cacheDirectory,savedCacheIndex} from './cacheDirectory.ts';
import {fileSha256} from './fileHash.ts';
const ROOT='datamark-playback-v1';
const APPLICATION='DataMark-desktop-cache-v1';
const live=new Map<string,{urls:Map<string,string>;files:Map<string,File>}>();
const indexes=new Map<string,{collection:string;ref:string;root?:FileSystemDirectoryHandle}>();
function validPart(value:string){return !!value&&value.length<=240&&!/[<>:"/\\|?*\x00-\x1f\x7f]/.test(value)&&!/[. ]$/.test(value)&&!['.','..'].includes(value);}
function validAsset(item:DesktopAsset){return validPart(item.file_name)&&item.file_name.includes('.'+item.sha256+'.')&&/^[0-9a-f]{64}$/.test(item.sha256)&&Number.isSafeInteger(item.size)&&item.size>0&&['video/mp4','image/jpeg'].includes(item.media_type)&&validAssetUrl(item.url);}
async function jsonFile(directory:FileSystemDirectoryHandle,name:string,value:unknown){const writable=await(await directory.getFileHandle(name,{create:true})).createWritable();await writable.write(JSON.stringify(value));await writable.close();}
async function readJson(directory:FileSystemDirectoryHandle,name:string){return JSON.parse(await(await(await directory.getFileHandle(name)).getFile()).text());}
export function browserVideoUrl(projectId:string,url:string){return live.get(projectId)?.urls.get(url)??apiUrl(url);}
export function desktopAssetFile(projectId:string,url:string){return live.get(projectId)?.files.get(url);}
export function forgetBrowserVideoUrls(projectId:string){const old=live.get(projectId);live.delete(projectId);if(old)for(const url of old.urls.values())URL.revokeObjectURL(url);}
async function oldPreview(projectId:string,manifest:SessionManifest,url:string):Promise<File|undefined>{
  if(!navigator.storage?.getDirectory)return;
  const video=manifest.videos.find(item=>item.url===url||item.fast_url===url);if(!video)return;
  try{const root=await navigator.storage.getDirectory(),cache=await root.getDirectoryHandle(ROOT),project=await cache.getDirectoryHandle(projectId),version=await project.getDirectoryHandle(manifest.version);return await(await version.getFileHandle(`${video.id}-${video.url===url?'normal':'fast'}.mp4`)).getFile();}catch(error){if(!(error instanceof DOMException&&error.name==='NotFoundError'))throw error;}
}
async function prepare(projectId:string,manifest:SessionManifest,signal:AbortSignal,onProgress:(ready:number,total:number)=>void){
  if(!/^[a-zA-Z0-9_-]{1,80}$/.test(manifest.version)||(!validPart(manifest.collection_name??'')||(manifest.collection_name?.length??0)>120)||!manifest.assets?.length||manifest.assets.some(item=>!validAsset(item)))throw new Error('服务未提供可校验的桌面缓存清单，请更新服务后重新准备。');
  const required=manifest.videos.flatMap(video=>[video.url,video.fast_url,video.thumbnail_url,...video.storyboard.sheets]);
  if(required.some(url=>!manifest.assets!.some(item=>item.url===url)))throw new Error('桌面缓存清单缺少必需素材。');
  const root=await cacheDirectory();
  const directory=await root.getDirectoryHandle(manifest.collection_name!,{create:true});
  const identity=typeof location==='undefined'?'local':location.origin;
  const owner=await fileSha256(new Blob([identity+':'+projectId]));
  const ref='.datamark-ref.'+owner+'.json';
  const index={collection:manifest.collection_name!,ref,root};indexes.set(projectId,index);await savedCacheIndex(projectId,index);
  let previous:DesktopAsset[]=[];
  try{const old=await readJson(directory,ref);if(old.application!==APPLICATION)throw new Error('缓存引用文件归属无法确认。');previous=old.files;}catch(error){if(!(error instanceof DOMException&&error.name==='NotFoundError'))throw error;}
  const knownOwners=new Set(previous.filter(item=>item.owned).map(item=>item.file_name.toLowerCase()));
  for await(const [name] of (directory as unknown as {entries:()=>AsyncIterableIterator<[string,FileSystemHandle]>}).entries()){
    if(name.startsWith('.datamark-ref.')&&name!==ref){const other=await readJson(directory,name);if(other.application!==APPLICATION)throw new Error('缓存引用归属无法核验。');for(const item of other.files)if(item.owned)knownOwners.add(item.file_name.toLowerCase());}
  }
  const oldRecords=new Map(previous.map(item=>[item.file_name,item]));
  const records=[...new Map([...previous,...manifest.assets].map(item=>[item.file_name,{file_name:item.file_name,size:item.size,sha256:item.sha256,owned:oldRecords.get(item.file_name)?.owned??false}])).values()];
  await jsonFile(directory,ref,{application:APPLICATION,files:records,state:'downloading'});
  const urls=new Map<string,string>(),files=new Map<string,File>();onProgress(0,manifest.assets.length);
  try{for(const [i,item] of manifest.assets.entries()){
    signal.throwIfAborted();let file:File|undefined;let existed=false;
    try{file=await(await directory.getFileHandle(item.file_name)).getFile();existed=true;}catch(error){if(!(error instanceof DOMException&&error.name==='NotFoundError'))throw error;}
    if(file&&(file.size!==item.size||await fileSha256(file,signal)!==item.sha256))file=undefined;
    if(!file){
      if(existed){
        if(!knownOwners.has(item.file_name.toLowerCase()))throw new Error('同名文件不是已登记的缓存，已保留原文件，请选择其它缓存目录。');
        const damaged=await(await directory.getFileHandle(item.file_name)).getFile();
        const backup=await(await directory.getFileHandle('.damaged.'+crypto.randomUUID()+'.'+item.file_name,{create:true})).createWritable();await damaged.stream().pipeTo(backup,{signal});
      }
      const legacy=await oldPreview(projectId,manifest,item.url);
      let incoming:ReadableStream<Uint8Array>;
      if(legacy&&legacy.size===item.size&&await fileSha256(legacy,signal)===item.sha256)incoming=legacy.stream();
      else{const response=await fetchAsset(item.url,{cache:'no-store',signal});if(!response.ok||!response.body||!response.headers.get('content-type')?.startsWith(item.media_type))throw new Error('缓存素材下载失败，请重试。');incoming=response.body;}
      const handle=await directory.getFileHandle(item.file_name,{create:true});
      try{
        const writable=await handle.createWritable();let received=0;
        const bounded=new TransformStream<Uint8Array,Uint8Array>({transform(chunk,controller){received+=chunk.length;if(received>item.size)throw new Error('下载素材超过清单大小，已停止保存。');controller.enqueue(chunk);}});
        await incoming.pipeThrough(bounded).pipeTo(writable,{signal});file=await handle.getFile();if(file.size!==item.size||await fileSha256(file,signal)!==item.sha256)throw new Error('缓存文件大小或 SHA-256 不匹配，请重新准备。');}
      catch(error){await directory.removeEntry(item.file_name).catch(()=>{});if(error instanceof DOMException&&error.name==='QuotaExceededError')throw new Error('缓存目录所在磁盘空间不足，请释放空间或更换缓存位置。');throw error;}
    }
    const record=records.find(record=>record.file_name===item.file_name)!;record.owned=record.owned||!existed||knownOwners.has(item.file_name.toLowerCase());
    await jsonFile(directory,ref,{application:APPLICATION,files:records,state:'downloading'});
    files.set(item.url,file);urls.set(item.url,URL.createObjectURL(file));onProgress(i+1,manifest.assets.length);
  }
  signal.throwIfAborted();
  await jsonFile(directory,'manifest.'+manifest.version+'.json',{application:APPLICATION,collection_name:manifest.collection_name,profile:manifest.profile,files:records});
  await jsonFile(directory,ref,{application:APPLICATION,files:records,state:'ready'});
  forgetBrowserVideoUrls(projectId);live.set(projectId,{urls,files});
  }catch(error){for(const url of urls.values())URL.revokeObjectURL(url);throw error;}
}
export async function prepareBrowserVideos(projectId:string,manifest:SessionManifest,signal:AbortSignal,onProgress:(ready:number,total:number)=>void){
  const locks=(navigator as unknown as {locks?:LockManager}).locks;
  if(locks)return locks.request('datamark-desktop:'+manifest.collection_name,{signal},()=>prepare(projectId,manifest,signal,onProgress));
  return prepare(projectId,manifest,signal,onProgress);
}
export async function clearBrowserVideoCache(projectId:string){
  forgetBrowserVideoUrls(projectId);
  const index=indexes.get(projectId)??await savedCacheIndex(projectId) as {collection:string;ref:string;root?:FileSystemDirectoryHandle}|undefined;
  if(index){
    if(!validPart(index.collection)||!/^\.datamark-ref\.[0-9a-f]{64}\.json$/.test(index.ref))throw new Error('缓存索引无效，已停止清理。');
    const directory=await(index.root??await cacheDirectory()).getDirectoryHandle(index.collection);
    const own=await readJson(directory,index.ref);if(own.application!==APPLICATION)throw new Error('缓存归属无法核验。');
    const references=new Set<string>();
    for await(const [name] of (directory as unknown as {entries:()=>AsyncIterableIterator<[string,FileSystemHandle]>}).entries()){
      if(name.startsWith('.datamark-ref.')&&name!==index.ref){const other=await readJson(directory,name);if(other.application!==APPLICATION)throw new Error('缓存引用无法核验。');for(const record of other.files)references.add(record.file_name.toLowerCase());}
    }
    for(const item of own.files){
      if(!validPart(item.file_name)||!/^[0-9a-f]{64}$/.test(item.sha256)||!item.file_name.includes('.'+item.sha256+'.'))throw new Error('缓存文件清单无效。');
      if(!item.owned||references.has(item.file_name.toLowerCase()))continue;
      try{const file=await(await directory.getFileHandle(item.file_name)).getFile();if(await fileSha256(file)===item.sha256)await directory.removeEntry(item.file_name);}catch(error){if(!(error instanceof DOMException&&error.name==='NotFoundError'))throw error;}
    }
    await directory.removeEntry(index.ref);indexes.delete(projectId);await savedCacheIndex(projectId,null);
  }
  // Explicit cleanup also removes this project's legacy origin-private copy.
  if(navigator.storage?.getDirectory){try{const root=await navigator.storage.getDirectory(),old=await root.getDirectoryHandle(ROOT);await old.removeEntry(projectId,{recursive:true});}catch(error){if(!(error instanceof DOMException&&error.name==='NotFoundError'))throw error;}}
}
