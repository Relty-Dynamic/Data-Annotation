import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import ts from 'typescript';
const require=createRequire(import.meta.url);
const compiled=ts.transpileModule(fs.readFileSync(new URL('../src/AuthShell.tsx',import.meta.url),'utf8'),{
 compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}
}).outputText;
function setup(failure=''){
 const input={value:'isolated-test-password'},calls=[],errors=[];
 let states=0;
 const hooks={useEffect(){},useRef:()=>({current:input}),useState(initial){const index=states++;return [index===1?false:initial,value=>{if(index===3)errors.push(value)}]}};
 const auth={setCsrfToken(){},async authFetch(path,options){
  calls.push({path,options});
  if(path.endsWith('login-challenge'))return {ok:failure!=='challenge',async json(){return {challenge:'test'}}};
  assert.equal(input.value,'','password must be cleared before the login network request');
  assert.equal(options.body.includes('isolated-test-password'),false);
  return {ok:failure!=='login',async json(){return failure==='login'?{detail:'登录失败'}:{csrf:'test',user:{id:'test'}}}};
 }};
 const encryption={async encryptLogin(username,password){assert.equal(password,'isolated-test-password');if(failure==='encrypt')throw new Error('加密失败');return {ciphertext:'opaque-test-envelope'}}};
 const exports={};
 vm.runInNewContext(compiled,{exports,require(name){
  if(name==='react')return hooks;
  if(name==='react/jsx-runtime')return require(name);
  if(name==='./auth')return auth;
  if(name==='./loginEncryption')return encryption;
  if(name==='./useBrowserLifetime')return {useBrowserLifetime(){}};
  if(name==='./S3UploadEntry')return {S3HelperHandoff:()=>null};
  return {default:()=>null};
 },URLSearchParams,window:{location:{search:''}}});
 const tree=exports.default();
 function find(node,predicate){if(!node||typeof node!=='object')return; if(predicate(node))return node;for(const child of [node.props?.children].flat(Infinity)){const found=find(child,predicate);if(found)return found;}}
 return {input,calls,errors,form:find(tree,node=>node.type==='form'),password:find(tree,node=>node.type==='input'&&node.props.type==='password')};
}
test('login password is not echoed through HTML value or defaultValue properties',()=>{
 const {password}=setup();assert.ok(password);assert.equal('value' in password.props,false);assert.equal('defaultValue' in password.props,false);assert.equal(password.props.type,'password');
});
test('submitting login clears the input before sending the encrypted request',async()=>{
 const {input,form,calls}=setup();await form.props.onSubmit({preventDefault(){}});
 assert.equal(input.value,'');assert.equal(calls.length,2);assert.ok(calls[1].path.endsWith('login-encrypted'));
});
for(const failure of ['challenge','encrypt','login'])test(`password is cleared on ${failure} failure`,async()=>{
 const {input,form,calls,errors}=setup(failure);await form.props.onSubmit({preventDefault(){}});
 assert.equal(input.value,'');assert.ok(errors.some(Boolean));assert.equal(calls.length,failure==='login'?2:1);
});
