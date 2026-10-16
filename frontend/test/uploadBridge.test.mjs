import test from 'node:test';
import assert from 'node:assert/strict';
import {installCommands,offlineInstallCommands,takeHelperHandoff} from '../src/uploadBridge.ts';

test('installer commands use the current HTTPS API and never include pairing credentials',()=>{
 const value=installCommands('https://api.example.com:8443');
 assert.match(value.mac,/https:\/\/api.example.com:8443\/api\/r2\/uploader\/install.sh/);
 assert.match(value.windows,/install.ps1/);
 for(const command of Object.values(value))assert.doesNotMatch(command,/claim|Bearer|password|ExecutionPolicy/);
 for(const command of Object.values(value))assert.match(command,/--noproxy '\*'/);
 assert.match(value.windows,/LASTEXITCODE/);
 for(const origin of ['http://api.example.com','https://api.example.com/path',"https://bad'.example.com",'https://user:pass@api.example.com'])assert.throws(()=>installCommands(origin));
});
test('handoff is removed from browser history and preserves the current project URL',()=>{
 const id='a'.repeat(32),claim='b'.repeat(43),urls=[];
 const value=takeHelperHandoff({hash:`#datamark-upload=${id}:${claim}`,pathname:'/',search:'?task=1'},url=>urls.push(url));
 assert.deepEqual(value,{id,claim});assert.deepEqual(urls,['/?task=1']);
 assert.equal(takeHelperHandoff({hash:'#other',pathname:'/',search:''},()=>assert.fail()),null);
});
test('malformed association is cleared and rejected instead of being sent to the backend',()=>{
 const urls=[];
 assert.throws(()=>takeHelperHandoff({hash:'#datamark-upload=bad',pathname:'/',search:''},url=>urls.push(url)));
 assert.deepEqual(urls,['/']);
});
test('install commands carry the originating browser on both platforms',()=>{
 for(const [ua,browser] of [['Mozilla Chrome/130 Safari/537','chrome'],['Mozilla Chrome/130 Edg/130','edge'],['Mozilla Firefox/130','firefox'],['Mozilla Version/18 Safari/605','safari']]){
  const commands=installCommands('https://api.example.com:8443',ua);
  for(const command of Object.values(commands))assert.ok(command.includes('?browser='+browser));
 }
 for(const command of Object.values(installCommands('https://api.example.com','unknown')))assert.ok(!command.includes('?browser='));
});

test('offline installation uses the current platform addresses and originating browser without changing host policy',()=>{
 const value=offlineInstallCommands('https://api.example.com:8443','https://app.example.com','Chrome/130 Safari/537');
 assert.match(value.mac,/sh \.\/install.command/);
 for(const command of Object.values(value)){
  assert.ok(command.includes('https://api.example.com:8443'));assert.ok(command.includes('https://app.example.com'));assert.ok(command.includes("browser 'chrome'"));
  assert.doesNotMatch(command,/ExecutionPolicy|Bearer|password|github.com|pypi/);
 }
 assert.throws(()=>offlineInstallCommands('https://api.example.com',"https://bad'.example.com"));
});
