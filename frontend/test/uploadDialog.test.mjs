import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';
const require=createRequire(import.meta.url);
const code=ts.transpileModule(fs.readFileSync(new URL('../src/S3UploadEntry.tsx',import.meta.url),'utf8'),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
function render(state,{update=false,online=true,working=false,packages=null}={}){let index=0;const exports={};
 vm.runInNewContext(code,{exports,location:{origin:'https://api.example.com'},document:{body:{}},require(name){
  if(name==='react')return {useEffect(){},useRef:()=>({current:null}),useState(initial){const current=index++;return [current===0?{id:'helper',online,update_available:update,report:{busy:working,selection:{name:'1014test',capture_date:'20261014',videos:['one.avi'],attachments:[],ignored:[]},job:{id:'job',prefix:'daily/20261014/1014test',state,progress:state==='ready'?100:20,detail:'当前进度'}}}:current===3?false:current===5?true:current===11?packages:initial,()=>{}]}};
  if(name==='react-dom')return {createPortal:node=>node};
  if(name==='./apiOrigin')return {apiOrigin:'https://api.example.com'};
  if(name==='./uploadBridge')return {installCommands:()=>({mac:'test',windows:'test'}),offlineInstallCommands:()=>({mac:'offline mac',windows:'offline windows'})};
  if(name==='./uploadFeedback')return {uploadStateName:value=>value};
  if(name==='./tasks/SuccessDialog')return {default:()=>null};
  if(name.endsWith('.css')||name==='./auth')return {};
  return require(name);
 }});
 return renderToStaticMarkup(exports.UploadDialog({onClose(){}}));
}
test('upload uses a native centered dialog and keeps only relevant upload information',()=>{
 const html=render('compressing');assert.match(html,/<dialog/);assert.match(html,/s3-upload-form/);assert.match(html,/压缩并上传/);assert.match(html,/本机采集文件夹|1014test/);assert.doesNotMatch(html,/timeline 由|无需进入另一套|DataMark · 公网 S3/);
});
test('a published job offers its collection path and cannot accidentally resubmit the same selection',()=>{
 const html=render('ready');assert.doesNotMatch(html,/s3-upload-form/);assert.match(html,/daily\/20261014\/1014test/);assert.match(html,/复制采集目录/);
});

test('a new component version offers updating only when the current upload is idle',()=>{
 assert.match(render('ready',{update:true}),/更新本机上传组件/);
 assert.doesNotMatch(render('compressing',{update:true,working:true}),/更新本机上传组件/);
});
test('offline installation lists verified platform downloads and preserves local commands',()=>{
 const html=render('ready',{online:false,packages:{ready:true,platforms:{'windows-x64':{file:'windows-kit.zip',url:'https://api.example.com/api/r2/uploader/packages/windows-kit.zip',sha256:'a'.repeat(64),size:1024}}}});
 assert.match(html,/下载完整离线安装包/);assert.match(html,/windows-kit.zip/);assert.match(html,/windows-kit.zip.sha256/);assert.match(html,/offline windows/);assert.match(html,/无需 VPN/);
});
