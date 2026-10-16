import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';

const require=createRequire(import.meta.url),cache=new Map();
function load(name){
 const file=fileURLToPath(new URL(`../src/tasks/${name}`,import.meta.url));
 if(cache.has(file))return cache.get(file);
 const exports={};cache.set(file,exports);
 const code=ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(code,{exports,require(name){
  if(name.endsWith('.css'))return {};
  if(name.startsWith('./')){
   const resolved=path.resolve(path.dirname(file),name);
   const target=['.tsx','.ts'].map(ext=>resolved+ext).find(fs.existsSync);
   if(target)return load(path.basename(target));
  }
  return require(name);
 }});
 return exports;
}
const axis={id:'posture',name:'姿势',mode:'state',labels:['坐','站']};
const props={rules:[{id:'rule',name:'日常姿势',task_type:'姿势',axes:[axis],boundary_tolerance_ms:null}],leader:true,busy:false,axes:[axis],onAxes(){},onCopy(){},onPublish(){}};
test('published rules and rule creation are separate groups collapsed by default',()=>{
 const html=renderToStaticMarkup(React.createElement(load('RuleLibrary.tsx').default,props));
 const groups=html.match(/<details class="task-rule-group[^"]*"[^>]*>/g);
 assert.equal(groups.length,2);assert.ok(groups.every(tag=>!tag.includes(' open')));
 assert.match(html,/已发布规则/);assert.match(html,/发布新规则/);
 assert.match(html,/不限制（人工复核）/);assert.doesNotMatch(html,/>删除规则</);
});
test('rule editor defaults to unlimited tolerance without changing axis type options',()=>{
 const html=renderToStaticMarkup(React.createElement(load('RuleEditor.tsx').default,props));
 assert.match(html,/<option value="none" selected="">不限制（默认）/);
 assert.doesNotMatch(html,/name="tolerance"/);
 assert.match(html,/<option value="state"/);assert.match(html,/<option value="event"/);
});
test('success prompt has its own dialog style and safe non-submit controls',()=>{
 const html=renderToStaticMarkup(React.createElement(load('SuccessDialog.tsx').default,{message:'规则发布成功。',onClose(){}}));
 assert.match(html,/<dialog[^>]+class="task-success-dialog"/);
 assert.match(html,/task-success-symbol/);assert.match(html,/规则发布成功/);
 assert.match(html,/aria-label="关闭提示"/);
 const buttons=html.match(/<button[^>]*>/g);
 assert.equal(buttons.length,2);assert.ok(buttons.every(tag=>tag.includes('type="button"')));
 assert.doesNotMatch(html,/task-rule-delete-dialog/);
});
