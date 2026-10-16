import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';
const require=createRequire(import.meta.url),exports={};
const code=ts.transpileModule(fs.readFileSync(new URL('../src/tasks/TaskMaterialList.tsx',import.meta.url),'utf8'),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
vm.runInNewContext(code,{exports,require});
const MaterialList=exports.default;
const clip=(id,quality='unknown',extra={})=>({id,collector_id:'collector',date:'2026-10-16',scene:'室内',duration_ms:12000,quality,uploader_id:'owner',source:{backend:'s3',key:`project/${id}`},...extra});
const props={collections:[{id:'one',name:'1001lixin',videos:[clip('v1'),clip('v2','passed'),clip('v3','failed')]}],sources:[],userId:'owner',canUpload:true,busy:false,onQuality(){}};
function findAll(node,predicate){if(!node||typeof node!=='object')return [];return [...(predicate(node)?[node]:[]),...[node.props?.children].flat(Infinity).flatMap(child=>findAll(child,predicate))];}
test('folder list starts collapsed and distinguishes all three quality states without relying on color',()=>{
 const html=renderToStaticMarkup(createElement(MaterialList,props));
 assert.match(html,/1001lixin/);assert.match(html,/3 段视频/);assert.match(html,/合格 1/);assert.match(html,/待质检 1/);assert.match(html,/不合格 1/);
 assert.doesNotMatch(html,/<details[^>]*\sopen(?:=|\s|>)/);
});
test('only the original uploader can edit quality, and each change targets the correct video',()=>{
 const calls=[];
 const tree=MaterialList({...props,onQuality:(...args)=>calls.push(args)});
 const selectors=findAll(tree,node=>node.type==='select');assert.equal(selectors.length,3);
 selectors[1].props.onChange({target:{value:'failed'}});assert.deepEqual(calls,[['v2','failed']]);
 for(const extra of [{canUpload:false},{userId:'other'}]){
  const html=renderToStaticMarkup(createElement(MaterialList,{...props,...extra}));assert.doesNotMatch(html,/<select/);assert.match(html,/未质检/);
 }
 const html=renderToStaticMarkup(createElement(MaterialList,{...props,busy:true}));assert.match(html,/<select[^>]*disabled/);
});
test('legacy video names resolve from source identity and unsafe names are escaped',()=>{
 const sources=[{name:'<script>legacy.mp4',source:props.collections[0].videos[0].source}];
 const html=renderToStaticMarkup(createElement(MaterialList,{...props,sources}));
 assert.match(html,/&lt;script&gt;legacy.mp4/);assert.doesNotMatch(html,/<script>/);assert.match(html,/12 秒/);
});
test('empty state gives appropriate guidance for uploaders and readers',()=>{
 const render=canUpload=>renderToStaticMarkup(createElement(MaterialList,{...props,collections:[],canUpload}));
 assert.match(render(true),/填写信息并登记文件夹/);assert.match(render(false),/上传人员登记素材后/);
});
