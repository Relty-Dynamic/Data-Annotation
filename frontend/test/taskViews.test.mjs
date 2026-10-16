import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';
const require=createRequire(import.meta.url),cache=new Map();
function load(url){
 const filename=fileURLToPath(url);if(cache.has(filename))return cache.get(filename);
 const exports={};cache.set(filename,exports);
 const code=ts.transpileModule(fs.readFileSync(filename,'utf8'),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(code,{exports,require(name){if(!name.startsWith('.'))return require(name);const base=path.resolve(path.dirname(filename),name);const candidate=[base,base+'.tsx',base+'.ts'].find(file=>fs.existsSync(file)&&fs.statSync(file).isFile());assert.ok(candidate,`Cannot resolve ${name}`);return load(pathToFileURL(candidate));}});return exports;
}
const TaskList=load(new URL('../src/tasks/TaskList.tsx',import.meta.url)).default;
const TaskDelivery=load(new URL('../src/tasks/TaskDelivery.tsx',import.meta.url)).default;
const {TaskStatus}=load(new URL('../src/tasks/TaskPresentation.tsx',import.meta.url));
const item=(id,status='annotating',extra={})=>({id,video_id:`video-${id}`,status,deadline:'2026-10-20',annotator_ids:['me'],...extra});
const listProps={items:[item('one'),item('two','deleted')],userId:'me',leader:true,busy:false,filtered:false,onView(){},onOpen(){},onDelete(){}};
function findAll(node,predicate){if(!node||typeof node!=='object')return [];return [...(predicate(node)?[node]:[]),...[node.props?.children].flat(Infinity).flatMap(child=>findAll(child,predicate))];}
const buttonText=node=>[node.props.children].flat(Infinity).filter(value=>typeof value==='string').join('');
test('task rows preserve selected record, ownership actions and deleted-task restrictions',()=>{
 const html=renderToStaticMarkup(createElement(TaskList,{...listProps,selectedId:'one'}));
 assert.match(html,/data-selected="true"/);assert.equal((html.match(/打开标注工作台/g)??[]).length,1);assert.equal((html.match(/删除任务/g)??[]).length,1);assert.equal((html.match(/查看任务/g)??[]).length,2);
 const reader=renderToStaticMarkup(createElement(TaskList,{...listProps,userId:'other',leader:false}));
 assert.doesNotMatch(reader,/打开标注工作台|删除任务/);
});
test('rearranged task actions still target the correct record',()=>{
 const calls=[];const tree=TaskList({...listProps,onView:task=>calls.push(['view',task.id]),onOpen:id=>calls.push(['open',id]),onDelete:task=>calls.push(['delete',task.id])});
 const buttons=findAll(tree,node=>node.type==='button');for(const label of ['查看任务','打开标注工作台','删除任务'])buttons.find(node=>buttonText(node)===label).props.onClick();
 assert.deepEqual(calls,[['view','one'],['open','one'],['delete','one']]);
});
test('task status remains readable without color and filtered empty lists have a clear message',()=>{
 assert.match(renderToStaticMarkup(createElement(TaskStatus,{status:'adjudicating'})),/待裁定/);
 assert.match(renderToStaticMarkup(createElement(TaskStatus,{status:'future-status'})),/future-status/);
 const html=renderToStaticMarkup(createElement(TaskList,{...listProps,items:[],filtered:true}));assert.match(html,/没有匹配的任务/);assert.match(html,/调整筛选条件/);
});
test('delivery selects only accepted tasks and keeps task field values and downloads',()=>{
 const calls=[];const props={items:[item('accepted','accepted'),item('pending','pending'),item('delivered','delivered')],deliveries:[{id:'batch-one',task_ids:['accepted']}],busy:false,onCreate(){},onDownload:id=>calls.push(id)};
 const html=renderToStaticMarkup(createElement(TaskDelivery,props));assert.match(html,/name="tasks" value="accepted"/);assert.doesNotMatch(html,/value="pending"|value="delivered"/);assert.match(html,/1 个任务/);
 const tree=TaskDelivery(props),button=findAll(tree,node=>node.type==='button').find(node=>buttonText(node)==='下载 CSV');assert.ok(button);button.props.onClick();assert.deepEqual(calls,['batch-one']);
});
test('delivery shows honest empty states and disables creation when nothing is eligible',()=>{
 const props={items:[],deliveries:[],busy:false,onCreate(){},onDownload(){}};
 const html=renderToStaticMarkup(createElement(TaskDelivery,props));assert.match(html,/暂无可交付任务/);assert.match(html,/暂无交付记录/);assert.match(html,/<button[^>]*disabled[^>]*>生成并核验交付批次/);
 const tree=TaskDelivery({...props,items:[item('one','accepted')],deliveries:[{id:'batch'}],busy:true});
 assert.ok(findAll(tree,node=>node.type==='button').every(button=>button.props.disabled));assert.ok(findAll(tree,node=>node.type==='input').every(input=>input.props.disabled));
});


test('view task and open review workbench remain separate actions',()=>{
 const calls=[];const tree=TaskList({...listProps,items:[item('one','submitted')],isReview:()=>true,onView:task=>calls.push(['details',task.id]),onReview:task=>calls.push(['review',task.id])});
 const buttons=findAll(tree,node=>node.type==='button');buttons.find(node=>buttonText(node)==='查看任务').props.onClick();assert.deepEqual(calls,[['details','one']]);
 buttons.find(node=>buttonText(node)==='打开复核工作台').props.onClick();assert.deepEqual(calls,[['details','one'],['review','one']]);
});
test('task details are a dialog with metadata, audit and independently gated workspace entries',()=>{
 const Detail=load(new URL('../src/tasks/TaskDetailDialog.tsx',import.meta.url)).default;
 const task=item('one','submitted',{rule:{name:'姿势规则',axes:[]},reviewer_id:'reviewer',video:{video_name:'clip.mp4',collection_name:'1001lixin'},history:{submissions:[],finals:[]}});
 const props={task,busy:false,error:'',onClose(){},accountName:id=>'姓名-'+id,canAnnotate:false,canReview:true,onAnnotate(){},onReview(){},audit:['已创建任务']};
 const html=renderToStaticMarkup(createElement(Detail,props,createElement('form',{'aria-label':'待分配任务'},'分配设置')));
 assert.match(html,/<dialog/);assert.match(html,/任务详情/);assert.match(html,/1001lixin/);assert.match(html,/姓名-reviewer/);assert.match(html,/已创建任务/);assert.match(html,/打开复核工作台/);assert.doesNotMatch(html,/打开标注工作台/);assert.match(html,/待分配任务/);
 const blocked=renderToStaticMarkup(createElement(Detail,{...props,canReview:false,error:'版本冲突'}));assert.doesNotMatch(blocked,/打开复核工作台/);assert.match(blocked,/role="alert"/);assert.match(blocked,/版本冲突/);
});


test('completed review is labeled waiting acceptance rather than still reviewing',()=>{
 const html=renderToStaticMarkup(createElement(TaskStatus,{status:'reviewing',reviewComplete:true}));
 assert.match(html,/待验收/);assert.match(html,/awaiting_acceptance/);assert.doesNotMatch(html,/>复核中</);
});


test('waiting acceptance rows offer managers acceptance even for their own annotations',()=>{
 const task=item('reviewed','reviewing',{review_complete:true});
 const props={...listProps,items:[task],isReview:()=>true,onReview(){}};
 const self=renderToStaticMarkup(createElement(TaskList,props));
 assert.match(self,/待验收/);assert.doesNotMatch(self,/本人参与标注/);assert.match(self,/验收任务/);
 const manager=renderToStaticMarkup(createElement(TaskList,{...props,userId:'other-admin'}));
 assert.match(manager,/验收任务/);assert.doesNotMatch(manager,/本人参与标注/);
});
