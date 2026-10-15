'use strict';
const $=id=>document.getElementById(id);
const token=document.querySelector('meta[name="upload-token"]').content;
let selected=null,working=false,serverBusy=false;
async function request(path,body){
 const response=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',headers:{'X-Upload-Token':token,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body)});
 const result=await response.json();if(!response.ok)throw new Error(result.detail||'操作未成功。');return result;
}
async function encryptLogin(username,password,challenge){
 if(!globalThis.crypto?.subtle)throw new Error('浏览器无法安全加密登录，请使用本机 localhost 窗口。');
 const encode=value=>btoa(String.fromCharCode(...new Uint8Array(value instanceof Uint8Array?value.buffer:value)));
 const aad=new TextEncoder().encode('datamark-login-v1:'+challenge.challenge);
 const publicKey=await crypto.subtle.importKey('spki',Uint8Array.from(atob(challenge.public_key),c=>c.charCodeAt(0)),{name:'RSA-OAEP',hash:'SHA-256'},false,['encrypt']);
 const key=await crypto.subtle.generateKey({name:'AES-GCM',length:256},true,['encrypt']);
 const iv=crypto.getRandomValues(new Uint8Array(12));
 const ciphertext=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:aad},key,new TextEncoder().encode(JSON.stringify({username,password})));
 const wrapped=await crypto.subtle.encrypt({name:'RSA-OAEP',label:aad},publicKey,await crypto.subtle.exportKey('raw',key));
 return {challenge:challenge.challenge,key:encode(wrapped),iv:encode(iv),ciphertext:encode(ciphertext)};
}
function destination(){const date=selected?.capture_date??'';$('destination').textContent=date?'S3 目标：daily/'+date+'/'+date.slice(4)+$('collector').value.trim()+'/':'';}
function busy(value){working=value;document.querySelectorAll('button').forEach(button=>button.disabled=value||serverBusy);}
async function run(action){busy(true);$('error').textContent='';try{await action();}catch(error){$('error').textContent=error.message;}finally{busy(false);}}
async function refresh(){
 const state=await request('/api/status');$('endpoint').textContent='公网后端：'+state.api;
 serverBusy=state.busy;
 $('login-section').hidden=!!state.user;$('account-section').hidden=!state.user;$('upload-section').hidden=!state.user;
 if(state.user)$('account').textContent='当前上传账号：'+state.user.display_name+' · '+state.user.username;
 if(state.job){$('progress-section').hidden=false;$('job-destination').textContent=state.job.prefix;$('progress').value=state.job.progress;$('detail').textContent=state.job.detail;
  if(state.job.state==='ready')$('result').textContent='上传完成。返回公网平台，导入此 S3 采集目录后登记、质检并分配任务。';
  else if(state.job.state==='error')$('result').textContent='本次未完成发布。已生成桌面缓存保留，重新选择同一目录可以重试。';
  else $('result').textContent='';
 }
 if(!working)document.querySelectorAll('button').forEach(button=>button.disabled=state.busy);
}
$('login').addEventListener('submit',event=>{event.preventDefault();void run(async()=>{const challenge=await request('/api/auth/login-challenge');const encrypted=await encryptLogin($('username').value.trim(),$('password').value,challenge);$('password').value='';await request('/api/auth/login-encrypted',encrypted);await refresh();});});
$('logout').addEventListener('click',()=>void run(async()=>{await request('/api/logout',{});selected=null;$('selection').hidden=true;$('progress-section').hidden=true;await refresh();}));
$('select').addEventListener('click',()=>void run(async()=>{const result=await request('/api/select',{});if(result.cancelled)return;selected=result;$('selection').hidden=false;$('summary').textContent=result.name+' · '+result.capture_date+' · '+result.videos.length+' 个视频 · '+result.attachments.length+' 个附属文件';$('file-list').textContent=['视频（本机压缩）：',...result.videos,'','附属数据（原样上传）：',...result.attachments,'','不上传（身份文件、既有时间轴等）：',...result.ignored].join('\n');destination();}));
$('collector').addEventListener('input',destination);
$('upload').addEventListener('submit',event=>{event.preventDefault();void run(async()=>{await request('/api/start',{collector:$('collector').value.trim(),uploader:$('uploader').value.trim(),note:$('note').value.trim()});await refresh();});});
void refresh().catch(error=>$('error').textContent=error.message);
setInterval(()=>{if(!working)void refresh().catch(error=>$('error').textContent=error.message);},2000);
