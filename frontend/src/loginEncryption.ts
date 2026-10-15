export interface LoginChallenge {challenge:string;public_key:string;expires_in:number}

export async function encryptLogin(username:string,password:string,challenge:LoginChallenge){
 if(!globalThis.crypto?.subtle)throw new Error('浏览器无法安全加密登录信息，请使用受信任的 HTTPS 页面或本机 localhost。');
 const encode=(value:ArrayBuffer|Uint8Array)=>btoa(String.fromCharCode(...new Uint8Array(value instanceof Uint8Array?value.buffer:value)));
 const aad=new TextEncoder().encode('datamark-login-v1:'+challenge.challenge);
 const publicKey=await crypto.subtle.importKey('spki',Uint8Array.from(atob(challenge.public_key),c=>c.charCodeAt(0)),{name:'RSA-OAEP',hash:'SHA-256'},false,['encrypt']);
 const key=await crypto.subtle.generateKey({name:'AES-GCM',length:256},true,['encrypt']);
 const iv=crypto.getRandomValues(new Uint8Array(12));
 const ciphertext=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:aad},key,new TextEncoder().encode(JSON.stringify({username,password})));
 const wrapped=await crypto.subtle.encrypt({name:'RSA-OAEP',label:aad},publicKey,await crypto.subtle.exportKey('raw',key));
 return {challenge:challenge.challenge,key:encode(wrapped),iv:encode(iv),ciphertext:encode(ciphertext)};
}
