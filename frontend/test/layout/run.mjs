import {createServer} from 'vite';
import {spawn} from 'node:child_process';
import {mkdtemp,rm,mkdir,writeFile,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {setTimeout as pause} from 'node:timers/promises';

const browser=process.env.LAYOUT_BROWSER;
if(!browser)throw new Error('Set LAYOUT_BROWSER to an installed Chrome/Edge executable. No browser is installed by this test.');
const root=resolve(import.meta.dirname,'../..'),temp=resolve(root,'../.tmp');
await mkdir(temp,{recursive:true});
const profile=await mkdtemp(resolve(temp,'layout-browser-'));
const server=await createServer({root,server:{host:'127.0.0.1',port:0},logLevel:'error'});
let child,socket;
try{
  await server.listen();
  child=spawn(browser,['--headless','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--no-proxy-server','--disable-background-networking',`--user-data-dir=${profile}`,'--remote-debugging-port=0','about:blank'],{stdio:'ignore'});
  child.on('error',error=>console.error(error.message));
  let port;
  for(let i=0;i<100;i++){
    try{port=(await readFile(resolve(profile,'DevToolsActivePort'),'utf8')).split('\n')[0];break;}catch{await pause(100);}
  }
  if(!port)throw new Error('Isolated browser did not start');
  const pages=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  socket=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
  let id=0;const pending=new Map();
  socket.onmessage=event=>{const message=JSON.parse(event.data);if(pending.has(message.id)){pending.get(message.id)(message);pending.delete(message.id);}};
  const call=async(method,params={})=>{
    const key=++id;const answer=new Promise(resolve=>pending.set(key,resolve));socket.send(JSON.stringify({id:key,method,params}));
    let timeout;
    try{
      const message=await Promise.race([answer,new Promise((_,reject)=>{timeout=setTimeout(()=>reject(new Error(`Browser command timed out: ${method}`)),10000);})]);
      if(message.error)throw new Error(JSON.stringify(message.error));return message.result;
    }finally{clearTimeout(timeout);pending.delete(key);}
  };
  await call('Emulation.setDeviceMetricsOverride',{width:1920,height:1200,deviceScaleFactor:Number(process.env.LAYOUT_SCALE??1),mobile:false});
  const address=server.httpServer.address();
  await call('Page.navigate',{url:`http://127.0.0.1:${address.port}/test/layout/index.html`});
  let results;
  for(let i=0;i<300;i++){
    const response=await call('Runtime.evaluate',{expression:'JSON.stringify({data:document.getElementById("layout-results")?.textContent,error:document.body?.dataset.layoutError})',returnByValue:true});
    const value=JSON.parse(response.result.value??'{}');
    if(value.error)throw new Error(value.error);
    if(value.data){results=JSON.parse(value.data);break;}
    await pause(100);
  }
  if(!results)throw new Error('Layout fixture did not complete');
  await writeFile(resolve(temp,`layout-${browser.includes('Edge')?'edge':'chrome'}-${process.env.LAYOUT_SCALE??1}.json`),JSON.stringify(results,null,2));
  const failures=results.filter(result=>result.errors.length);
  if(results.length!==26 || failures.length)throw new Error(JSON.stringify({count:results.length,failures},null,2));
  if(process.env.LAYOUT_SCREENSHOTS){
    for(const [width,height] of [[1280,800],[800,500]]){
      await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
      await call('Page.navigate',{url:`http://127.0.0.1:${address.port}/test/layout/index.html?fixture=1`});
      await pause(600);
      const shot=await call('Page.captureScreenshot',{format:'png'});
      await writeFile(resolve(temp,`layout-${width}x${height}.png`),Buffer.from(shot.data,'base64'));
    }
  }
  console.log(`PASS: ${results.length} real-component layout cases; page bounds, video/timeline gap, controls, and independent scrolling; DPR ${process.env.LAYOUT_SCALE??1}.`);
}finally{
  socket?.close();
  if(child && child.exitCode===null){const exited=new Promise(resolve=>child.once('exit',resolve));child.kill();await exited;}
  await server.close();await rm(profile,{recursive:true,force:true,maxRetries:3});
}
