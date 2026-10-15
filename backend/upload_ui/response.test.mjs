import test from 'node:test';
import assert from 'node:assert/strict';
import {readUploadResponse} from './response.mjs';

test('upload helper preserves valid success data and actionable JSON errors',async()=>{
 assert.deepEqual(await readUploadResponse(new Response('{"state":"ready"}',{status:200})),{state:'ready'});
 await assert.rejects(readUploadResponse(new Response('{"detail":"上传权限已关闭。"}',{status:403})),/上传权限已关闭/);
});
test('upload helper explains non-JSON server errors without exposing their bodies',async()=>{
 for(const body of ['Internal Server Error','<html>private fixture traceback</html>']){
  await assert.rejects(readUploadResponse(new Response(body,{status:500})),error=>{
   assert.match(error.message,/HTTP 500/);
   assert.doesNotMatch(error.message,/Unexpected token|private fixture|Internal Server Error/);
   return true;
  });
 }
});
test('upload helper rejects malformed successful responses and non-object JSON',async()=>{
 for(const body of ['not JSON','null','[]','"text"'])await assert.rejects(readUploadResponse(new Response(body,{status:200})));
});
