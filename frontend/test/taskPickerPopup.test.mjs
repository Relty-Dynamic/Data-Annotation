import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';
import {assignedTaskChoices} from '../src/tasks/taskChoices.ts';
const require=createRequire(import.meta.url);
const code=ts.transpileModule(fs.readFileSync(new URL('../src/tasks/TaskPicker.tsx',import.meta.url),'utf8'),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
function setup(){let state=0;const changes=[],portals=[],chosen=[],body={};const tasks=[{id:'one',video_id:'1001lixin',annotator_ids:['me'],reviewer_id:'reviewer',status:'annotating',deadline:'2026-10-20'}];const exports={};
 vm.runInNewContext(code,{exports,document:{body,fullscreenElement:null},require(name){
  if(name==='react')return {useEffect(){},useLayoutEffect(){},useRef:()=>({current:null}),useState(initial){const index=state++;return [index===0?tasks:index===1?false:index===4?true:initial,value=>changes.push([index,value])];}};
  if(name==='react-dom')return {createPortal(node,target){portals.push(target);return node}};
  if(name==='./TaskPanel')return {taskRequest:async()=>[]};if(name==='./taskChoices')return {assignedTaskChoices};return require(name);
 }});
 const tree=exports.default({user:{id:'me'},currentTask:null,review:false,disabled:false,onChoose:choice=>chosen.push(choice)});
 function all(node,predicate){if(!node||typeof node!=='object')return [];return [...(predicate(node)?[node]:[]),...[node.props?.children].flat(Infinity).flatMap(child=>all(child,predicate))];}
 return {tree,changes,portals,chosen,body,all};
}
test('task chooser renders a centered native modal instead of an anchored or inline list',()=>{
 const {tree,portals,body}=setup();assert.deepEqual(portals,[body]);const html=renderToStaticMarkup(tree);
 assert.match(html,/<dialog/);assert.match(html,/aria-modal="true"/);assert.match(html,/task-assigned-dialog/);assert.doesNotMatch(html,/style="/);assert.match(html,/role="dialog"/);assert.match(html,/task-assigned-popover/);assert.doesNotMatch(html,/<details/);assert.match(html,/aria-haspopup="dialog"/);
});
test('choosing a task closes the popup and keeps the assigned task destination',()=>{
 const {tree,all,changes,chosen}=setup();all(tree,node=>node.type==='button'&&node.props.className==='task-assigned-choice')[0].props.onClick();
 assert.ok(changes.some(([index,value])=>index===4&&value===false));assert.equal(chosen[0].url,'?task=one');
});
