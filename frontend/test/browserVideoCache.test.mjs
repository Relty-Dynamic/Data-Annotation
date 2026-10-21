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
  assert.equal(requests(),4,'downloads can finish while an earlier file is being verified');assert.equal(desktopAssetFile('project_1',manifest.videos[0].url),undefined);
  const complete=fixture().manifest;await prepareBrowserVideos('project_1',complete,signal,()=>{});assert.equal(requests(),5,'all three verified files are reused and only the truncated file is downloaded');
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

test('batch ownership is durable before downloads and unchanged reuse performs no metadata writes',async t=>{
  const {root}=await setup(t),{manifest,bytes}=fixture(),signal=new AbortController().signal;
  const directory=await root.getDirectoryHandle(manifest.collection_name,{create:true});
  const writes=[],original=directory.getFileHandle.bind(directory);
  directory.getFileHandle=async(name,options)=>{
    const handle=await original(name,options),create=handle.createWritable;
    handle.createWritable=async()=>{writes.push(name);return create();};return handle;
  };
  t.mock.method(globalThis,'fetch',async url=>{
    const ref=[...directory.files].find(([name])=>name.startsWith('.datamark-ref.'));
    assert.ok(ref,'ownership must survive a crash before the first download finishes');
    const saved=JSON.parse(ref[1]);assert.equal(saved.state,'downloading');
    assert.ok(manifest.assets.every(item=>saved.files.some(record=>record.file_name===item.file_name&&record.owned)));
    return new Response(bytes,{headers:{'content-type':url.includes('thumbnail')||url.includes('storyboard')?'image/jpeg':'video/mp4'}});
  });
  let last;
  await prepareBrowserVideos('project_1',manifest,signal,()=>{},(_op,_ready,_total,metrics)=>last=metrics);
  assert.equal(writes.filter(name=>name.startsWith('.datamark-ref.')).length,2);
  assert.equal(last.readyBytes,16);assert.equal(last.downloadedBytes,16);assert.equal(last.checkedBytes,16);
  writes.length=0;
  await prepareBrowserVideos('project_1',manifest,signal,()=>{},(_op,_ready,_total,metrics)=>last=metrics);
  assert.deepEqual(writes,[],'a verified, unchanged collection needs no file or metadata rewrite');
  assert.equal(last.downloadedBytes,0);assert.equal(last.checkedBytes,16,'reuse still verifies every byte');
});

test('downloads continue while checks are busy and no more than two hashes run at once',async t=>{
  const {requests}=await setup(t),{manifest}=fixture();
  const digest=crypto.subtle.digest.bind(crypto.subtle);let release,active=0,peak=0;
  const gate=new Promise(resolve=>release=resolve);
  t.mock.method(crypto.subtle,'digest',async(algorithm,bytes)=>{
    if(bytes.byteLength!==4)return digest(algorithm,bytes);
    active++;peak=Math.max(peak,active);
    try{await gate;return await digest(algorithm,bytes);}finally{active--;}
  });
  const pending=prepareBrowserVideos('project_1',manifest,new AbortController().signal,()=>{});
  try{
    for(let tries=0;tries<100&&requests()<4;tries++)await new Promise(resolve=>setTimeout(resolve,1));
    assert.equal(requests(),4,'network slots are released before readback verification finishes');
    assert.equal(peak,2);
  }finally{release();await pending;}
});

test('same-size corrupted owned files are repaired but unregistered conflicting files are preserved',async t=>{
  const {root,requests}=await setup(t),{manifest}=fixture(),signal=new AbortController().signal;
  await prepareBrowserVideos('project_1',manifest,signal,()=>{});
  const directory=root.directories.get(manifest.collection_name),name=manifest.assets[0].file_name;
  directory.files.set(name,Buffer.from([9,9,9,9]));
  await prepareBrowserVideos('project_1',manifest,signal,()=>{});
  assert.equal(requests(),5,'full hash detects damage even when size is unchanged');
  await clearBrowserVideoCache('project_1');
  directory.files.set(name,Buffer.from('mine'));
  await assert.rejects(prepareBrowserVideos('project_1',manifest,signal,()=>{}),/不是已登记/);
  assert.equal(directory.files.get(name).toString(),'mine');
  assert.equal(desktopAssetFile('project_1',manifest.videos[0].url),undefined);
});

test('large-file worker cancellation terminates work and worker errors never bypass verification',async t=>{
  const original=globalThis.Worker;const workers=[];
  class FakeWorker{
    constructor(){workers.push(this);}
    postMessage(blob){this.blob=blob;}
    terminate(){this.terminated=true;}
  }
  globalThis.Worker=FakeWorker;t.after(()=>{if(original)globalThis.Worker=original;else delete globalThis.Worker;});
  const blob=new Blob([new Uint8Array(64*1024*1024+1)]),controller=new AbortController();
  const pending=fileSha256(blob,controller.signal);controller.abort();
  await assert.rejects(pending,{name:'AbortError'});assert.ok(workers[0].terminated);
  const failed=fileSha256(blob);workers[1].onerror();
  await assert.rejects(failed,/校验线程失败/);assert.ok(workers[1].terminated);
});

test('a failed metadata save starts no downloads and retry remains safe',async t=>{
  const {root,requests}=await setup(t),{manifest}=fixture();
  const directory=await root.getDirectoryHandle(manifest.collection_name,{create:true});
  const original=directory.getFileHandle.bind(directory);let fail=true;
  directory.getFileHandle=async(name,options)=>{
    const handle=await original(name,options);
    if(fail&&name.startsWith('.datamark-ref.'))handle.createWritable=async()=>{throw new DOMException('Full','QuotaExceededError');};
    return handle;
  };
  await assert.rejects(prepareBrowserVideos('project_1',manifest,new AbortController().signal,()=>{}),{name:'QuotaExceededError'});
  assert.equal(requests(),0);assert.equal(desktopAssetFile('project_1',manifest.videos[0].url),undefined);
  fail=false;await prepareBrowserVideos('project_1',manifest,new AbortController().signal,()=>{});
  assert.equal(requests(),4);
});

test('progress describes concurrent download and checking rather than replacing one with the other',async()=>{
  const {cacheProgressText}=await import('../src/cacheProgress.ts');
  const text=cacheProgressText(2,4,{totalBytes:10485760,readyBytes:5242880,downloadedBytes:7340032,
    checkedBytes:5242880,downloading:2,checking:1,saving:true,elapsedMs:2345,downloadMs:1000,checkMs:1000,saveMs:30});
  assert.match(text,/2\/4/);assert.match(text,/5.0 MB\/10.0 MB/);
  assert.match(text,/2 路处理中/);assert.match(text,/1 路处理中/);
  assert.match(text,/正在保存登记/);assert.match(text,/已用时 2 秒/);
});

test('cancelling metadata inspection waits for outstanding reads and writes no reservations',async t=>{
  const {root,requests}=await setup(t),{manifest}=fixture(),controller=new AbortController();
  const directory=await root.getDirectoryHandle(manifest.collection_name,{create:true});
  const original=directory.getFileHandle.bind(directory);let enter,release;
  const entered=new Promise(resolve=>enter=resolve),gate=new Promise(resolve=>release=resolve);
  directory.getFileHandle=async(name,options)=>{
    if(name===manifest.assets[0].file_name){enter();await gate;}
    return original(name,options);
  };
  let settled=false;
  const pending=prepareBrowserVideos('project_1',manifest,controller.signal,()=>{});
  pending.then(()=>settled=true,()=>settled=true);
  await entered;controller.abort();await new Promise(resolve=>setTimeout(resolve,1));
  assert.equal(settled,false,'outstanding filesystem reads must settle before returning');
  release();await assert.rejects(pending,{name:'AbortError'});
  assert.equal(requests(),0);assert.equal(directory.files.size,0);
});


test('normal-only cache downloads one video, reuses it, and keeps historical fast files',async t=>{
  const {root,requests}=await setup(t),{manifest}=fixture(),signal=new AbortController().signal;
  await prepareBrowserVideos('project_1',manifest,signal,()=>{});
  assert.equal(requests(),4);
  const oldFast=manifest.assets[1].file_name;
  const video={...manifest.videos[0],playback_mode:'normal-only'};delete video.fast_url;
  const next={...manifest,version:'normal-only',videos:[video],assets:manifest.assets.filter(item=>item.url!==manifest.videos[0].fast_url)};
  await prepareBrowserVideos('project_1',next,signal,()=>{});
  assert.equal(requests(),4,'all normal assets are reused');
  assert.ok(root.directories.get('1010zengl').files.has(oldFast),'old fast bytes are not removed by preparation');
  await prepareBrowserVideos('project_2',{...next,collection_name:'new-capture'},signal,()=>{});
  assert.equal(requests(),7,'fresh capture fetches one MP4 and two images');
  assert.equal([...root.directories.get('new-capture').files.keys()].filter(name=>name.endsWith('.mp4')).length,1);
});
