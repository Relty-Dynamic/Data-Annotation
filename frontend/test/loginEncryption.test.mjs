import test from 'node:test';
import assert from 'node:assert/strict';
import {encryptLogin} from '../src/loginEncryption.ts';

test('an unsupported browser fails closed without returning plaintext credentials',async()=>{
 const descriptor=Object.getOwnPropertyDescriptor(globalThis,'crypto');
 try{
  Object.defineProperty(globalThis,'crypto',{configurable:true,value:undefined});
  await assert.rejects(()=>encryptLogin('fixture@example.com','fixture password',{challenge:'fixture',public_key:'',expires_in:120}),/无法安全加密/);
 }finally{Object.defineProperty(globalThis,'crypto',descriptor);}
});

test('login request contains randomized ciphertext and decrypts to the original Unicode credentials',async()=>{
 const keys=await crypto.subtle.generateKey({name:'RSA-OAEP',modulusLength:3072,publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['encrypt','decrypt']);
 const challenge={challenge:'test-single-use-token',public_key:Buffer.from(await crypto.subtle.exportKey('spki',keys.publicKey)).toString('base64'),expires_in:120};
 const username='测试@example.com',password='密'.repeat(1024);
 const body=await encryptLogin(username,password,challenge),second=await encryptLogin(username,password,challenge);
 assert.deepEqual(Object.keys(body).sort(),['challenge','ciphertext','iv','key']);
 assert.equal(JSON.stringify(body).includes(password),false);
 assert.equal(JSON.stringify(body).includes(username),false);
 assert.notEqual(body.ciphertext,second.ciphertext);
 const aad=new TextEncoder().encode('datamark-login-v1:'+challenge.challenge);
 const raw=await crypto.subtle.decrypt({name:'RSA-OAEP',label:aad},keys.privateKey,Buffer.from(body.key,'base64'));
 const key=await crypto.subtle.importKey('raw',raw,'AES-GCM',false,['decrypt']);
 const result=await crypto.subtle.decrypt({name:'AES-GCM',iv:Buffer.from(body.iv,'base64'),additionalData:aad},key,Buffer.from(body.ciphertext,'base64'));
 assert.deepEqual(JSON.parse(new TextDecoder().decode(result)),{username,password});
 await assert.rejects(()=>crypto.subtle.decrypt({name:'AES-GCM',iv:Buffer.from(body.iv,'base64'),additionalData:new TextEncoder().encode('different challenge')},key,Buffer.from(body.ciphertext,'base64')));
});
