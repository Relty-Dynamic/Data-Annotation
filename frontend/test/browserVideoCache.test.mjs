import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash,randomBytes} from 'node:crypto';
import {browserVideoUrl,clearBrowserVideoCache,forgetBrowserVideoUrls,prepareBrowserVideos,desktopAssetFile} from '../src/browserVideoCache.ts';
import {rememberCacheDirectory,forgetSelectedCacheDirectory} from '../src/cacheDirectory.ts';
import {fileSha256,streamingSha256} from '../src/fileHash.ts';
const missing=()=>new DOMException('Missing','NotFoundError');
class Directory{
  constructor(name='dataMarkProject'){this.name=name;this.directories=new Map();this.files=new Map();this.permission='granted';}
  async queryPermission(){return this.permission;}
  async getDirectoryHandle(name,options={}){if(!this.directories.has(name)){if(!options.create)throw missing();this.directories.set(name,new Directory(name));}return this.directories.get(name);}
  async getFileHandle(name,options={}){
    if(!this.files.has(name)&&!options.create)throw missing();
    const directory=this;
    return {getFile:async()=>new File([directory.files.get(name)??new Uint8Array()],name),createWritable:async()=>{
      const chunks=[];
      const stream=new WritableStream({write:chunk=>chunks.push(typeof chunk==='string'?Buffer.from(chunk):Buffer.from(chunk)),close:()=>directory.files.set(name,Buffer.concat(chunks))});
      stream.write=async value=>chunks.push(typeof value==='string'?Buffer.from(value):Buffer.from(value));
      stream.close=async()=>directory.files.set(name,Buffer.concat(chunks));return stream;
    }};
  }
  async removeEntry(name){if(this.files.has(name))this.files.delete(name);else if(this.directories.has(name))this.directories.delete(name);else throw missing();}
  async *entries(){for(const [name] of this.files)yield[name,{kind:'file'}];for(const[name]of this.directories)yield[name,{kind:'directory'}];}
}
const digest=bytes=>createHash('sha256').update(bytes).digest('hex');
function fixture(){
  const bytes=new Uint8Array([1,2,3,4]),sha=digest(bytes);
  const urls=['/api/session-media/p/v/a','/api/session-media/p/v/a?fast=true','/api/session-thumbnails/p/v/a','/api/session-storyboards/p/v/a/0'];
  const names=['normal.mp4','fast.mp4','thumbnail.jpg','storyboard-000.jpg'];
  const assets=urls.map((url,i)=>({url,file_name:'clip.'+sha+'.'+names[i],size:bytes.length,sha256:sha,media_type:i<2?'video/mp4':'image/jpeg'}));
  return{bytes,manifest:{collection_name:'1010zengl',version:'version1',assets,videos:[{id:'a',url:urls[0],fast_url:urls[1],thumbnail_url:urls[2],storyboard:{sheets:[urls[3]]}}]}};
}
async function setup(t){
  const root=new Directory();await rememberCacheDirectory(root);
  let requests=0;
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{}});
  t.mock.method(globalThis,'fetch',async url=>{requests++;return new Response(fixture().bytes,{headers:{'content-type':url.includes('thumbnail')||url.includes('storyboard')?'image/jpeg':'video/mp4'}});});
  t.after(()=>{forgetSelectedCacheDirectory();for(const id of ['project_1','project_2'])forgetBrowserVideoUrls(id);});
  return{root,requests:()=>requests};
}
test('streaming SHA-256 matches reference hashes across padding and stream boundaries',async()=>{
  for(const length of [0,1,55,56,63,64,65,128,65535,65536,65537,1024*1024+37]){
    const bytes=randomBytes(length);assert.equal(await streamingSha256(new Blob([bytes])),digest(bytes));assert.equal(await fileSha256(new Blob([bytes])),digest(bytes));
  }
});
test('all videos and images persist in the selected collection and reuse renewed URLs',async t=>{
  const {root,requests}=await setup(t),{manifest}=fixture(),signal=new AbortController().signal,progress=[];
  await prepareBrowserVideos('project_1',manifest,signal,(...args)=>progress.push(args));
  assert.equal(requests(),4);assert.deepEqual(progress.at(-1),[4,4]);
  assert.match(browserVideoUrl('project_1',manifest.videos[0].url),/^blob:/);
  assert.equal(desktopAssetFile('project_1',manifest.videos[0].thumbnail_url).size,4);
  assert.equal(root.directories.get('1010zengl').files.size,6);
  const renewed={...manifest,videos:manifest.videos.map(video=>({...video,url:video.url+'&renewed',fast_url:video.fast_url+'&renewed',thumbnail_url:video.thumbnail_url+'?renewed',storyboard:{sheets:video.storyboard.sheets.map(url=>url+'?renewed')}})),assets:manifest.assets.map((item,i)=>({...item,url:i<2?item.url+'&renewed':item.url+'?renewed'}))};
  forgetBrowserVideoUrls('project_1');await prepareBrowserVideos('project_1',renewed,signal,()=>{});assert.equal(requests(),4);
  await clearBrowserVideoCache('project_1');assert.equal(root.directories.get('1010zengl').files.size,1,'only historical manifest metadata remains');
  await prepareBrowserVideos('project_1',manifest,signal,()=>{});assert.equal(requests(),8);
});
test('shared files remain until both projects release them and personal files are preserved',async t=>{
  const {root,requests}=await setup(t),{manifest}=fixture(),signal=new AbortController().signal;
  await prepareBrowserVideos('project_1',manifest,signal,()=>{});await prepareBrowserVideos('project_2',manifest,signal,()=>{});assert.equal(requests(),4);
  const directory=root.directories.get('1010zengl');directory.files.set('notes.txt',Buffer.from('personal'));
  await clearBrowserVideoCache('project_1');assert.ok(manifest.assets.every(item=>directory.files.has(item.file_name)));
  await clearBrowserVideoCache('project_2');assert.ok(manifest.assets.every(item=>!directory.files.has(item.file_name)));assert.equal(directory.files.get('notes.txt').toString(),'personal');
});
test('corrupt registered cache is repaired with original changed bytes retained',async t=>{
  const {root,requests}=await setup(t),{manifest}=fixture(),signal=new AbortController().signal;
  await prepareBrowserVideos('project_1',manifest,signal,()=>{});const directory=root.directories.get('1010zengl');directory.files.set(manifest.assets[0].file_name,Buffer.from('bad'));
  forgetBrowserVideoUrls('project_1');await prepareBrowserVideos('project_1',manifest,signal,()=>{});assert.equal(requests(),5);assert.ok([...directory.files].some(([name,bytes])=>name.startsWith('.damaged.')&&bytes.toString()==='bad'));
});
test('missing authorization and malformed manifests never silently use private browser storage',async t=>{
  const {root,requests}=await setup(t),{manifest}=fixture(),signal=new AbortController().signal;
  root.permission='denied';await assert.rejects(prepareBrowserVideos('project_1',manifest,signal,()=>{}),/权限已失效/);assert.equal(requests(),0);
  root.permission='granted';await assert.rejects(prepareBrowserVideos('project_1',{...manifest,collection_name:'../escape'},signal,()=>{}),/清单/);
  await assert.rejects(prepareBrowserVideos('project_1',{...manifest,assets:manifest.assets.slice(1)},signal,()=>{}),/缺少/);
});
test('a truncated download fails readiness but completed files survive retry',async t=>{
  const {requests}=await setup(t),{manifest}=fixture(),signal=new AbortController().signal;
  manifest.assets[1]={...manifest.assets[1],size:8};await assert.rejects(prepareBrowserVideos('project_1',manifest,signal,()=>{}),/不匹配/);
  assert.equal(requests(),3,'three in-flight files may start before one fails');assert.equal(desktopAssetFile('project_1',manifest.videos[0].url),undefined);
  const complete=fixture().manifest;await prepareBrowserVideos('project_1',complete,signal,()=>{});assert.equal(requests(),5,'first completed file is reused');
});
test('matching files without application ownership survive explicit cleanup',async t=>{
  const {root,requests}=await setup(t),{manifest,bytes}=fixture(),signal=new AbortController().signal;
  const directory=await root.getDirectoryHandle(manifest.collection_name,{create:true});
  for(const item of manifest.assets)directory.files.set(item.file_name,Buffer.from(bytes));
  await prepareBrowserVideos('project_1',manifest,signal,()=>{});assert.equal(requests(),0);
  await clearBrowserVideoCache('project_1');assert.ok(manifest.assets.every(item=>directory.files.has(item.file_name)));
});


test('another account task reuses uploader desktop files with no media download or rewrite',async t=>{
  const {root,requests}=await setup(t),{manifest,bytes}=fixture(),signal=new AbortController().signal;
  const directory=await root.getDirectoryHandle(manifest.collection_name,{create:true});
  for(const item of manifest.assets)directory.files.set(item.file_name,Buffer.from(bytes));
  directory.files.set('.datamark-ref.uploader.json',Buffer.from(JSON.stringify({application:'DataMark-desktop-cache-v1',files:manifest.assets.map(item=>({...item,owned:true}))})));
  const original=new Map(directory.files),activity=[];
  const originalHandle=directory.getFileHandle.bind(directory);
  directory.getFileHandle=async(name,options)=>{
    const handle=await originalHandle(name,options);
    if(manifest.assets.some(item=>item.file_name===name))handle.createWritable=async()=>{throw new Error('Existing media must not be rewritten');};
    return handle;
  };
  await prepareBrowserVideos('project_2',manifest,signal,()=>{},(operation,ready,total)=>activity.push({operation,ready,total}));
  assert.equal(requests(),0);
  assert.equal(activity.filter(item=>item.operation==='check').length,4);
  assert.deepEqual(activity.at(-1),{operation:'reuse',ready:4,total:4});
  assert.ok(!activity.some(item=>item.operation==='download'));
  for(const [name,content] of original)assert.deepEqual(directory.files.get(name),content);
  assert.equal(desktopAssetFile('project_2',manifest.videos[0].url).size,bytes.length);
});


test('cache hash cancellation is respected before and after native digest',async t=>{
 const controller=new AbortController();controller.abort();
 await assert.rejects(fileSha256(new Blob(['data']),controller.signal),{name:'AbortError'});
 const later=new AbortController();
 t.mock.method(crypto.subtle,'digest',async()=>{later.abort();return new ArrayBuffer(32);});
 await assert.rejects(fileSha256(new Blob(['data']),later.signal),{name:'AbortError'});
});

test('browser preparation overlaps requests but never exceeds three downloads',async t=>{
 const {root}=await setup(t),{manifest,bytes}=fixture();let active=0,peak=0,count=0;
 t.mock.method(globalThis,'fetch',async url=>{active++;peak=Math.max(peak,active);count++;try{await new Promise(resolve=>setTimeout(resolve,10));return new Response(bytes,{headers:{'content-type':url.includes('thumbnail')||url.includes('storyboard')?'image/jpeg':'video/mp4'}});}finally{active--;}});
 const progress=[];await prepareBrowserVideos('project_1',manifest,new AbortController().signal,(ready,total)=>progress.push([ready,total]));
 assert.equal(count,4);assert.equal(peak,3);assert.deepEqual(progress.at(-1),[4,4]);
 assert.ok(manifest.assets.every(item=>root.directories.get(manifest.collection_name).files.has(item.file_name)));
});

test('cancelling parallel preparation settles all requests and never marks the project ready',async t=>{
 const {root}=await setup(t),{manifest}=fixture(),controller=new AbortController();let entered=0,settled=0,notify;
 const started=new Promise(resolve=>{notify=resolve;});
 t.mock.method(globalThis,'fetch',async(url,options)=>new Promise((resolve,reject)=>{
  entered++;if(entered===3)notify();
  const abort=()=>{settled++;reject(options.signal.reason);};
  if(options.signal.aborted)abort();else options.signal.addEventListener('abort',abort,{once:true});
 }));
 const pending=prepareBrowserVideos('project_1',manifest,controller.signal,()=>{});
 await started;controller.abort();await assert.rejects(pending,{name:'AbortError'});
 assert.equal(entered,3);assert.equal(settled,3);assert.equal(desktopAssetFile('project_1',manifest.videos[0].url),undefined);
 const directory=root.directories.get(manifest.collection_name);
 for(const[name,bytes]of directory.files)if(name.startsWith('.datamark-ref.'))assert.equal(JSON.parse(bytes.toString()).state,'downloading');
});
