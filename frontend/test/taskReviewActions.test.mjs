import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import ts from 'typescript';
const require=createRequire(import.meta.url);
function nodes(node,type){if(!node||typeof node!=='object')return [];return [...(node.type===type?[node]:[]),...[node.props?.children].flat(Infinity).flatMap(child=>nodes(child,type))];}
function mount(){
 const initial={id:'task',status:'submitted',revision:3,annotator_ids:['a'],reviewer_id:'r',participants:[{id:'a',display_name:'标注员'}],submissions:[{id:'version',user_id:'a',annotations:{}}],rule:{id:'rule',axes:[]}};
 const slots=[],effects=[],requests=[],dirty=[];let cursor=0,tree,complete,fail=false;
 const hooks={
  useState(value){const i=cursor++;if(!(i in slots))slots[i]=value;return [slots[i],next=>{slots[i]=typeof next==='function'?next(slots[i]):next;}];},
  useRef(value){const i=cursor++;return slots[i]??(slots[i]={current:value});},
  useEffect(fn,deps){const i=cursor++;if(!slots[i]||!deps.every((d,j)=>Object.is(d,slots[i][j]))){slots[i]=deps;effects.push(fn);}},
 };
 const module={exports:{}};
 const code=ts.transpileModule(fs.readFileSync(new URL('../src/tasks/TaskWorkbenchReview.tsx',import.meta.url),'utf8'),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(code,{exports:module.exports,require(name){
  if(name==='react')return hooks;
  if(name==='react/jsx-runtime')return require(name);
  if(name==='./TaskReview')return {default:'TaskReview'};
  if(name==='./SuccessDialog')return {default:'SuccessDialog'};
  if(name==='./TaskOutputSync')return {default:'TaskOutputSync'};
  if(name==='./taskChoices')return {reviewStage:task=>task.status};
  if(name==='./TaskPanel')return {async taskRequest(path,method,body){
   requests.push({path,method,body});
   if(method==='POST'){
    await new Promise(resolve=>{complete=resolve;});
    if(fail)throw new Error('存储连接中断');
    return {...initial,revision:body.expected_revision+1,status:path.endsWith('start-review')?'reviewing':'rework'};
   }
   if(path==='/me')return {roles:['reviewer']};
   if(path.endsWith('/history'))return {submissions:[],finals:[]};
   return initial;
  }};
  throw new Error('Unexpected module '+name);
 }});
 const props={taskId:'task',user:{id:'r',role:'annotator'},time:0,onSeek(){},onPreview(){},onDirty:value=>dirty.push(value),onBusy(){}};
 function render(){cursor=0;tree=module.exports.default(props);for(const effect of effects.splice(0))effect();return tree;}
 async function settle(){for(let i=0;i<12;i++)await Promise.resolve();render();}
 render();
 return {requests,dirty,render,settle,get tree(){return tree;},finish(){complete();},setFail(){fail=true;}};
}
test('start-review applies its verified response immediately without reloading task, roles or history',async()=>{
 const app=mount();await app.settle();assert.equal(app.requests.length,2);
 const review=nodes(app.tree,'TaskReview')[0];review.props.onAction('start-review');
 assert.equal(nodes(app.render(),'TaskReview')[0].props.busy,true);
 review.props.onAction('start-review');assert.equal(app.requests.length,3);
 app.finish();await app.settle();
 const updated=nodes(app.tree,'TaskReview')[0];assert.equal(updated.props.task.status,'reviewing');assert.equal(updated.props.task.revision,4);
 assert.equal(updated.props.task.participants[0].display_name,'标注员');
 assert.equal(app.requests.length,3);assert.equal(nodes(app.tree,'SuccessDialog').length,0);
 updated.props.onLoadHistory();await app.settle();assert.equal(app.requests.length,4);assert.ok(app.requests.at(-1).path.endsWith('/history'));
});
test('successful review shows a modal and failed review preserves the form instance',async()=>{
 const app=mount();await app.settle();
 nodes(app.tree,'TaskReview')[0].props.onAction('review',{action:'return'});app.finish();await app.settle();
 assert.match(nodes(app.tree,'SuccessDialog')[0].props.message,/退回.*返修/);assert.equal(app.requests.length,3);
 const failed=mount();await failed.settle();const before=nodes(failed.tree,'TaskReview')[0];
 failed.setFail();before.props.onAction('review',{action:'return'});failed.finish();await failed.settle();
 assert.equal(nodes(failed.tree,'SuccessDialog').length,0);assert.equal(nodes(failed.tree,'TaskReview')[0].key,before.key);
 assert.equal(nodes(failed.tree,'TaskReview')[0].props.task.revision,3);
 assert.equal(failed.dirty.length,0);
});
