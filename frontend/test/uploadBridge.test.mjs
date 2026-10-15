import test from 'node:test';
import assert from 'node:assert/strict';
import {installCommands,takeHelperHandoff} from '../src/uploadBridge.ts';

test('installer commands use the current HTTPS API and never include pairing credentials',()=>{
 const value=installCommands('https://api.example.com:8443');
 assert.match(value.mac,/https:\/\/api.example.com:8443\/api\/r2\/uploader\/install.sh/);
 assert.match(value.windows,/install.ps1/);
 for(const command of Object.values(value))assert.doesNotMatch(command,/claim|Bearer|password|ExecutionPolicy/);
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
