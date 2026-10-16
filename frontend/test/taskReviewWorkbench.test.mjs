import {formatOperationTime} from '../src/operationTime.ts';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';
const require=createRequire(import.meta.url);
function loadReview(form=null){
 const exports={};
 const code=ts.transpileModule(fs.readFileSync(new URL('../src/tasks/TaskReview.tsx',import.meta.url),'utf8'),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(code,{exports,require(name){
  if(name==='react')return {useEffect(){},useRef:()=>({current:form}),useState:initial=>[initial,()=>{}]};
  if(name==='react/jsx-runtime')return require(name);
  if(name==='./permissions')return {canReviewTask:(user,roles,task)=>user.role==='admin'||!task.annotator_ids.includes(user.id)&&task.reviewer_id===user.id};
  if(name==='../operationTime')return {formatOperationTime};
  if(name==='./TaskPresentation')return {TaskStatus:()=>null};
  if(name==='../domain')return {formatTime:value=>String(value),parseTime:value=>Number(value)};
  if(name==='./types')return {composeFinal:()=>({annotations:{},contributors:{}}),issueRange:()=>null};
  if(name==='../Timeline')return {default:()=>{throw new Error('review sidebar must use the main workbench timeline')}};
  return {};
 },FormData:class{constructor(values){this.values=values}get(name){return this.values[name]??null}getAll(name){return this.values[name]??[]}},crypto:{randomUUID:()=> 'fixture'}});
 return exports.default;
}
const Review=loadReview();
const version=id=>({id,user_id:id,annotations:{posture:[{id:'segment-'+id,label:'坐',start_ms:0,end_ms:1000,lane:0}]}});
const task={id:'task',video_id:'video',status:'reviewing',revision:3,annotator_ids:['a','b'],reviewer_id:'reviewer',deadline:'2026-10-20',rule:{id:'rule',name:'姿势',axes:[{id:'posture',name:'姿势',mode:'state',labels:['坐']}]},submissions:[version('a'),version('b')],history:{submissions:[version('history')],finals:[]}};
const props={task,rules:[],user:{id:'reviewer',role:'annotator'},roles:['reviewer'],busy:false,onAction(){},workbench:{time:500,onSeek(){},onPreview(){},versionId:'a'}};
function findAll(node,predicate){if(!node||typeof node!=='object')return [];return [...(predicate(node)?[node]:[]),...[node.props?.children].flat(Infinity).flatMap(child=>findAll(child,predicate))];}
test('review runs in the main workbench with version selectors and no second video or timeline',()=>{
 const html=renderToStaticMarkup(createElement(Review,props));assert.doesNotMatch(html,/<video/);assert.match(html,/<h4>提交版本<\/h4>/);assert.match(html,/提交复核结论/);assert.match(html,/在主时间轴查看此版本/);
});
test('switching current and historical submissions previews exact versions without mutating annotations',()=>{
 const previews=[];const before=JSON.stringify(task);
 const tree=Review({...props,workbench:{...props.workbench,onPreview:version=>previews.push(version.id)}});
 const buttons=findAll(tree,node=>node.type==='button');
 buttons.filter(button=>'aria-pressed' in button.props).slice(0,2).forEach(button=>button.props.onClick());
 buttons.find(button=>button.props.children==='在主时间轴查看此版本').props.onClick();
 assert.deepEqual(previews,['a','b','history']);assert.equal(JSON.stringify(task),before);
});
test('review conclusion continues using the existing review endpoint and form fields',()=>{
 const calls=[];const tree=Review({...props,onAction:(endpoint,body)=>calls.push({endpoint,body})});
 const form=findAll(tree,node=>node.type==='form')[0];form.props.onSubmit({preventDefault(){},currentTarget:{action:'return',axis:'posture',start:'0',end:'1000',reason:'请复查姿势',return_to:['a']}});
 assert.equal(calls[0].endpoint,'review');assert.equal(calls[0].body.action,'return');assert.equal(calls[0].body.issues[0].reason,'请复查姿势');assert.deepEqual([...calls[0].body.return_to],['a']);
});


test('review range shortcuts use current playback and the selected annotation without typing times',()=>{
 const fields=new Map(['start','end','axis'].map(name=>[name,{value:''}]));
 const ReviewWithForm=loadReview({elements:{namedItem:name=>fields.get(name)}});let dirty=0;
 const tree=ReviewWithForm({...props,workbench:{...props.workbench,selection:{axis:'posture',start_ms:123,end_ms:456},onDirty:()=>dirty++}});
 const buttons=findAll(tree,node=>node.type==='button');
 buttons.find(button=>button.props.children==='当前位置设为起点').props.onClick();assert.equal(fields.get('start').value,'500');
 buttons.find(button=>button.props.children==='当前位置设为终点').props.onClick();assert.equal(fields.get('end').value,'500');
 buttons.find(button=>button.props.children==='使用选中色块范围').props.onClick();assert.equal(fields.get('start').value,'123');assert.equal(fields.get('end').value,'456');assert.equal(fields.get('axis').value,'posture');assert.equal(dirty,3);
});
test('adjudication offers return rather than an invalid pass, and completed reviews cannot resubmit',()=>{
 const html=renderToStaticMarkup(createElement(Review,{...props,task:{...task,status:'adjudicating'}}));
 assert.doesNotMatch(html,/<option value="pass"/);assert.match(html,/<option value="return" selected/);
 const completed=renderToStaticMarkup(createElement(Review,{...props,roles:['reviewer','leader'],task:{...task,review_complete:true}}));
 assert.doesNotMatch(completed,/提交复核结论/);assert.match(completed,/验收并锁定/);
 const pending=renderToStaticMarkup(createElement(Review,{...props,roles:['reviewer','leader']}));assert.doesNotMatch(pending,/验收并锁定/);
});

test('review sidebar omits repeated task metadata while keeping versions and decisions',()=>{
 const html=renderToStaticMarkup(createElement(Review,props));
 assert.doesNotMatch(html,/任务版本|截止|采集员：|标注员：|复核员：/);
 assert.match(html,/提交版本/);assert.match(html,/提交复核结论/);assert.match(html,/历史版本/);
});


test('start review is prominent, explains the workflow and disables repeated clicks while pending',()=>{
 const submitted={...props,task:{...task,status:'submitted'}};
 const html=renderToStaticMarkup(createElement(Review,submitted));
 assert.match(html,/primary task-review-start/);assert.match(html,/准备检查标注结果/);assert.match(html,/通过或退回返修/);
 const pending=renderToStaticMarkup(createElement(Review,{...submitted,busy:true}));
 assert.match(pending,/aria-busy="true"/);assert.match(pending,/正在开始复核…/);
 const calls=[];const tree=Review({...submitted,onAction:endpoint=>calls.push(endpoint)});
 findAll(tree,node=>node.type==='button'&&node.props.className==='primary task-review-start')[0].props.onClick();
 assert.deepEqual(calls,['start-review']);
});
test('history is fetched only when expanded and the decision form explains return steps',()=>{
 let requests=0;
 const tree=Review({...props,task:{...task,history:undefined},onLoadHistory:()=>requests++});
 assert.equal(requests,0);
 const details=findAll(tree,node=>node.type==='details')[0];
 details.props.onToggle({currentTarget:{open:false}});assert.equal(requests,0);
 details.props.onToggle({currentTarget:{open:true}});assert.equal(requests,1);
 const html=renderToStaticMarkup(createElement(Review,props));assert.match(html,/填写复核结论/);assert.match(html,/勾选返修人员后提交/);
});


test('self-reviewing admin gets an explicit acceptance restriction and other leader can accept',()=>{
 const own=renderToStaticMarkup(createElement(Review,{...props,user:{id:'a',role:'admin'},roles:['leader','reviewer'],task:{...task,review_complete:true}}));
 assert.match(own,/待验收/);assert.match(own,/你参与了本任务标注，不能验收自己的结果/);assert.doesNotMatch(own,/验收并锁定最终结果/);
 const other=renderToStaticMarkup(createElement(Review,{...props,user:{id:'admin',role:'admin'},roles:['leader','reviewer'],task:{...task,review_complete:true}}));
 assert.match(other,/验收并锁定最终结果/);
});
