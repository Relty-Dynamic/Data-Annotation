import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';
const require=createRequire(import.meta.url);
const code=ts.transpileModule(fs.readFileSync(new URL('../src/tasks/TaskOutputSync.tsx',import.meta.url),'utf8'),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
function mount(reply={state:'ready',detail:'采集目录已同步'},ok=true){
 const states=[],requests=[];let index=0;
 const exports={};
 vm.runInNewContext(code,{exports,require(name){
  if(name==='react')return {useEffect(){},useState(initial){const slot=index++;if(!(slot in states))states[slot]=initial;return [states[slot],value=>states[slot]=value];}};
  if(name==='react/jsx-runtime')return require(name);
  if(name==='../auth')return {async authFetch(path,options){requests.push({path,options});return {ok,status:ok?200:503,async text(){return typeof reply==='string'?reply:JSON.stringify(reply);}};}};
  throw new Error(name);
 }});
 const props={taskId:'task',status:{state:'error',detail:'任务已保存，S3 尚未同步'}};
 function render(){index=0;return exports.default(props);}
 function button(tree){return [tree.props.children].flat(Infinity).find(child=>child?.type==='button');}
 return {requests,render,button,states,async retry(){button(render()).props.onClick();for(let i=0;i<12;i++)await Promise.resolve();return render();}};
}
test('failed directory publication is explicit and retry targets the same task export',async()=>{
 const app=mount();const html=renderToStaticMarkup(app.render());assert.match(html,/任务已保存，S3 尚未同步/);assert.match(html,/同步采集结果目录/);
 const tree=await app.retry();assert.equal(app.requests.length,1);assert.equal(app.requests[0].path,'/api/tasks/items/task/outputs/retry');assert.equal(app.requests[0].options.method,'POST');
 assert.match(renderToStaticMarkup(tree),/采集目录已同步/);assert.equal(app.button(tree),undefined);
});
test('a pending or failed retry never claims publication success',async()=>{
 const app=mount({state:'error',detail:'同名文件已被外部修改'});const tree=await app.retry();assert.match(renderToStaticMarkup(tree),/同名文件已被外部修改/);assert.ok(app.button(tree));
 const failed=mount({detail:'服务暂不可用'},false);const result=await failed.retry();assert.match(renderToStaticMarkup(result),/服务暂不可用/);assert.ok(failed.button(result));
});
test('non JSON server errors and malformed success bodies have actionable messages',async()=>{
 const app=mount('Internal Server Error',false);assert.match(renderToStaticMarkup(await app.retry()),/结果目录服务响应异常（HTTP 503）/);
 const invalid=mount({detail:'missing state'});assert.match(renderToStaticMarkup(await invalid.retry()),/同步状态无效/);
});
