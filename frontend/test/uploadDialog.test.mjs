import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {renderToStaticMarkup} from 'react-dom/server';
import ts from 'typescript';
import {predictionMessage,predictionFailureReason} from '../src/uploadFeedback.ts';
const require=createRequire(import.meta.url);
const code=ts.transpileModule(fs.readFileSync(new URL('../src/S3UploadEntry.tsx',import.meta.url),'utf8'),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
function render(state,{update=false,online=true,working=false,packages=null,imu=false,predict=false,prediction=null,clockSupport=false}={}){let index=0;const exports={};
 vm.runInNewContext(code,{exports,navigator:{userAgent:'Mac'},location:{origin:'https://api.example.com'},document:{body:{}},require(name){
  if(name==='react')return {useEffect(){},useRef:()=>({current:null}),useState(initial){const current=index++;return [current===0?{id:'helper',online,update_available:update,report:{busy:working,selection:{...(imu?{upload_kind:'imu'}:{}),name:'1014test',capture_date:'20261014',videos:['one.avi'],attachments:[],ignored:[]},job:{...(imu?{upload_kind:'imu'}:{}),id:'job',prefix:'daily/20261014/1014test',state,prediction,progress:state==='ready'?100:20,detail:'当前进度'}}}:current===3?false:current===5?true:current===11?packages:current===15?predict:current===19?(imu?'imu':'video'):current===20?'2026-10-14':current===21?true:current===24?clockSupport:initial,()=>{}]}};
  if(name==='react-dom')return {createPortal:node=>node};
  if(name==='./apiOrigin')return {apiOrigin:'https://api.example.com'};
  if(name==='./uploadBridge')return {installCommands:()=>({mac:'test',windows:'test'}),offlineInstallCommands:()=>({mac:'offline mac',windows:'offline windows'})};
  if(name==='./uploadFeedback')return {uploadStateName:value=>value,predictionMessage,predictionFailureReason};
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

test('IMU has its own date and raw-upload action without hidden required prediction fields',()=>{
 const html=render('uploading',{imu:true,predict:true});
 assert.match(html,/type="date"/);assert.match(html,/上传 IMU/);assert.match(html,/不压缩、不生成姿势预标注/);
 assert.doesNotMatch(html,/胸部 IMU 设备编号|大腿 IMU 设备编号|可选预处理|压缩并上传/);
});
test('the complete collection route explains simultaneous FPV and IMU',()=>{
 const html=render('compressing');assert.match(html,/FPV \/ 完整采集/);assert.match(html,/视频与 IMU 一起上传/);assert.match(html,/可选预处理/);
});
test('completed IMU does not tell users to import a video annotation task',()=>{
 const html=render('ready',{imu:true});assert.match(html,/仅含 IMU 的目录不进入视频标注/);assert.doesNotMatch(html,/可在“素材与质检”导入/);
});
test('completed material upload retains partial prediction exclusions and diagnostic ID',()=>{
 const html=render('ready',{prediction:{sha256:'a',segments:2,needs_alignment_review:true,batch_status:'partial',validated_records:1,total_records:2,job_id:'b'.repeat(64),excluded_records:[{record_id:'short',path:'IMU/short.txt',reason:'alignment_unresolved'}]}});
 assert.match(html,/部分姿势预标注已完成/);assert.match(html,/1\/2/);assert.match(html,/IMU\/short.txt/);assert.match(html,/未获得可靠校时/);assert.match(html,/预标注诊断编号/);
});
test('cross-record alignment needs an explicit continuity confirmation and worker capability',()=>{
 const html=render('compressing',{predict:true,clockSupport:true});
 assert.match(html,/确认本次胸部设备与相机的时钟连续/);assert.match(html,/未重启、换机或调时/);
 assert.match(html,/<input type="checkbox"\/>确认本次胸部设备与相机的时钟连续/);
 const legacy=render('compressing',{predict:true});assert.match(legacy,/当前算法服务尚不支持跨记录校时/);
});
