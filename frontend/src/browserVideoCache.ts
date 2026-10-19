import {apiUrl} from './apiOrigin.ts';
import type {SessionManifest, DesktopAsset} from './sessionAssets.ts';
import {fetchAsset, validAssetUrl} from './objectAsset.ts';
import {cacheDirectory,savedCacheIndex} from './cacheDirectory.ts';
import {fileSha256} from './fileHash.ts';
import {limitConcurrency} from './cacheScheduling.ts';
export type CacheMetrics = {
  totalBytes:number; readyBytes:number; downloadedBytes:number; checkedBytes:number;
  downloading:number; checking:number; saving:boolean; elapsedMs:number;
  downloadMs:number; checkMs:number; saveMs:number;
};
type CacheActivity = (operation:'check'|'reuse'|'download',ready:number,total:number,metrics:CacheMetrics)=>void;
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
async function prepare(projectId:string,manifest:SessionManifest,externalSignal:AbortSignal,onProgress:(ready:number,total:number)=>void,onActivity?:CacheActivity){
  if(!/^[a-zA-Z0-9_-]{1,80}$/.test(manifest.version)||(!validPart(manifest.collection_name??'')||(manifest.collection_name?.length??0)>120)||!manifest.assets?.length||manifest.assets.some(item=>!validAsset(item)))throw new Error('服务未提供可校验的桌面缓存清单，请更新服务后重新准备。');
  const assets=manifest.assets;
  if(new Set(assets.map(item=>item.file_name)).size!==assets.length)throw new Error('缓存清单包含重复文件名。');
  const required=manifest.videos.flatMap(video=>[video.url,video.fast_url,video.thumbnail_url,...video.storyboard.sheets]);
  if(required.some(url=>!assets!.some(item=>item.url===url)))throw new Error('桌面缓存清单缺少必需素材。');
  const root=await cacheDirectory();
  const directory=await root.getDirectoryHandle(manifest.collection_name!,{create:true});
  const identity=typeof location==='undefined'?'local':location.origin;
  const owner=await fileSha256(new Blob([identity+':'+projectId]));
  const ref='.datamark-ref.'+owner+'.json';
  const index={collection:manifest.collection_name!,ref,root};indexes.set(projectId,index);await savedCacheIndex(projectId,index);
  let previous:DesktopAsset[]=[]; let previousState='';
  try{const old=await readJson(directory,ref);if(old.application!==APPLICATION)throw new Error('缓存引用文件归属无法确认。');previous=old.files;previousState=old.state;}catch(error){if(!(error instanceof DOMException&&error.name==='NotFoundError'))throw error;}
  const knownOwners=new Set(previous.filter(item=>item.owned).map(item=>item.file_name.toLowerCase()));
  for await(const [name] of (directory as unknown as {entries:()=>AsyncIterableIterator<[string,FileSystemHandle]>}).entries()){
    if(name.startsWith('.datamark-ref.')&&name!==ref){const other=await readJson(directory,name);if(other.application!==APPLICATION)throw new Error('缓存引用归属无法核验。');for(const item of other.files)if(item.owned)knownOwners.add(item.file_name.toLowerCase());}
  }
  const oldRecords=new Map(previous.map(item=>[item.file_name,item]));
  const records=[...new Map([...previous,...assets].map(item=>[item.file_name,{file_name:item.file_name,size:item.size,sha256:item.sha256,owned:oldRecords.get(item.file_name)?.owned??false}])).values()];
  const urls=new Map<string,string>(),files=new Map<string,File>();onProgress(0,assets.length);
  const controller=new AbortController(),signal=controller.signal;
  const abort=()=>controller.abort(externalSignal.reason);
  externalSignal.addEventListener('abort',abort,{once:true});if(externalSignal.aborted)abort();
  let completed=0,next=0,failure:unknown;
  const started=performance.now();
  const metrics:CacheMetrics={totalBytes:assets.reduce((sum,item)=>sum+item.size,0),readyBytes:0,
    downloadedBytes:0,checkedBytes:0,downloading:0,checking:0,saving:false,elapsedMs:0,
    downloadMs:0,checkMs:0,saveMs:0};
  let lastReport=-Infinity;
  const report=(force=false)=>{
    const now=performance.now();if(!force&&now-lastReport<100)return;lastReport=now;
    onActivity?.(metrics.downloading?'download':metrics.checking||completed<assets.length?'check':'reuse',completed,assets.length,
      {...metrics,elapsedMs:now-started});
  };
  const heartbeat=setInterval(()=>report(),500);
  const download=limitConcurrency(3),check=limitConcurrency(2);
  const hash=(file:Blob)=>check(async()=>{
    const start=performance.now();metrics.checking++;report();
    try{return await fileSha256(file,signal,bytes=>{metrics.checkedBytes+=bytes;report();});}
    finally{metrics.checking--;metrics.checkMs+=performance.now()-start;report();}
  },signal);
  const recordByName=new Map(records.map(record=>[record.file_name,record]));
  const initialFiles=new Map<string,File>();
  let pending:Promise<void>|undefined;
  let persistence=Promise.resolve();
  const saveJson=(name:string,value:unknown)=>{
    const snapshot=JSON.parse(JSON.stringify(value));
    persistence=persistence.then(async()=>{
      const start=performance.now();metrics.saving=true;report();
      try{await jsonFile(directory,name,snapshot);}
      finally{metrics.saving=false;metrics.saveMs+=performance.now()-start;report();}
    });
    return persistence;
  };
  // Persist ownership reservations before creating media. One durable write
  // covers the batch, including cancellation/crash recovery of partial files.
  const ensurePending=()=>pending??=saveJson(ref,{application:APPLICATION,files:records,state:'downloading'});
  async function one(item:DesktopAsset){

    signal.throwIfAborted();let file=initialFiles.get(item.file_name);const existed=!!file;
    if(file&&(file.size!==item.size||await hash(file)!==item.sha256))file=undefined;
    if(!file){
      if(existed){
        if(!knownOwners.has(item.file_name.toLowerCase()))throw new Error('同名文件不是已登记的缓存，已保留原文件，请选择其它缓存目录。');
        const damaged=await(await directory.getFileHandle(item.file_name)).getFile();
        const backup=await(await directory.getFileHandle('.damaged.'+crypto.randomUUID()+'.'+item.file_name,{create:true})).createWritable();await damaged.stream().pipeTo(backup,{signal});
      }
      await ensurePending();
      const legacy=await oldPreview(projectId,manifest,item.url);
      const legacyValid=legacy&&legacy.size===item.size&&await hash(legacy)===item.sha256;
      if(!existed){
        // A file created outside this attempt after the reservation is not ours.
        try{
          await directory.getFileHandle(item.file_name);
          recordByName.get(item.file_name)!.owned=false;
          await saveJson(ref,{application:APPLICATION,files:records,state:'downloading'});
          throw new Error('缓存准备期间出现同名文件，已保留，请重新准备。');
        }
        catch(error){if(!(error instanceof DOMException&&error.name==='NotFoundError'))throw error;}
      }
      const handle=await directory.getFileHandle(item.file_name,{create:true});
      try{
        await download(async()=>{
          const start=performance.now();metrics.downloading++;report();
          try{
            let incoming:ReadableStream<Uint8Array>;
            if(legacyValid)incoming=legacy.stream();
            else{
              const response=await fetchAsset(item.url,{cache:'no-store',signal});
              if(!response.ok||!response.body||!response.headers.get('content-type')?.startsWith(item.media_type))throw new Error('缓存素材下载失败，请重试。');
              incoming=response.body;
            }
            const writable=await handle.createWritable();let received=0;
            const bounded=new TransformStream<Uint8Array,Uint8Array>({transform(chunk,controller){
              received+=chunk.length;if(received>item.size)throw new Error('下载素材超过清单大小，已停止保存。');
              if(!legacyValid)metrics.downloadedBytes+=chunk.length;report();controller.enqueue(chunk);
            }});
            await incoming.pipeThrough(bounded).pipeTo(writable,{signal});
          }finally{metrics.downloading--;metrics.downloadMs+=performance.now()-start;report();}
        },signal);
        file=await handle.getFile();
        if(file.size!==item.size||await hash(file)!==item.sha256)throw new Error('缓存文件大小或 SHA-256 不匹配，请重新准备。');
      }catch(error){await directory.removeEntry(item.file_name).catch(()=>{});if(error instanceof DOMException&&error.name==='QuotaExceededError')throw new Error('缓存目录所在磁盘空间不足，请释放空间或更换缓存位置。');throw error;}

    }
    files.set(item.url,file);urls.set(item.url,URL.createObjectURL(file));completed++;metrics.readyBytes+=item.size;
    onProgress(completed,assets.length);report(true);
  }
  try{
  report();
  // Read metadata once before reserving absent paths. Existing unowned files
  // never become owned merely because their content matches a download.
  let inspected=0;
  const inspections=await Promise.allSettled(Array.from({length:Math.min(3,assets.length)},async()=>{
    while(inspected<assets.length&&!signal.aborted){
      signal.throwIfAborted();const item=assets[inspected++];
      try{initialFiles.set(item.file_name,await(await directory.getFileHandle(item.file_name)).getFile());}
      catch(error){if(!(error instanceof DOMException&&error.name==='NotFoundError'))throw error;}
      const record=recordByName.get(item.file_name)!;
      record.owned=record.owned||!initialFiles.has(item.file_name)||knownOwners.has(item.file_name.toLowerCase());
    }
  }));
  const inspectionFailure=inspections.find(result=>result.status==='rejected');
  if(inspectionFailure?.status==='rejected')throw inspectionFailure.reason;
  signal.throwIfAborted();
  if(previousState!=='ready'||JSON.stringify(previous)!==JSON.stringify(records))await ensurePending();
  await Promise.all(Array.from({length:Math.min(6,assets.length)},async()=>{
    while(next<assets!.length&&!failure){
      const item=assets![next++];
      try{await one(item);}catch(error){if(!failure)failure=error;return;}
    }
  }));
  if(failure)throw failure;
  signal.throwIfAborted();
  const manifestName='manifest.'+manifest.version+'.json';
  const savedManifest={application:APPLICATION,collection_name:manifest.collection_name,profile:manifest.profile,files:records};
  let unchanged=false;
  try{unchanged=JSON.stringify(await readJson(directory,manifestName))===JSON.stringify(savedManifest);}
  catch(error){if(!(error instanceof DOMException&&error.name==='NotFoundError'))throw error;}
  if(!unchanged)await saveJson(manifestName,savedManifest);
  if(pending)await saveJson(ref,{application:APPLICATION,files:records,state:'ready'});
  signal.throwIfAborted();report(true);
  forgetBrowserVideoUrls(projectId);live.set(projectId,{urls,files});
  }catch(error){for(const url of urls.values())URL.revokeObjectURL(url);throw error;}
  finally{clearInterval(heartbeat);externalSignal.removeEventListener('abort',abort);}
}
export async function prepareBrowserVideos(projectId:string,manifest:SessionManifest,signal:AbortSignal,onProgress:(ready:number,total:number)=>void,onActivity?:CacheActivity){
  const locks=(navigator as unknown as {locks?:LockManager}).locks;
  if(locks)return locks.request('datamark-desktop:'+manifest.collection_name,{signal},()=>prepare(projectId,manifest,signal,onProgress,onActivity));
  return prepare(projectId,manifest,signal,onProgress,onActivity);
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
