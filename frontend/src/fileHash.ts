// Incremental SHA-256 keeps large previews out of a single ArrayBuffer.
const K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
const rotate = (n:number,s:number) => (n>>>s)|(n<<(32-s));
export async function fileSha256(blob: Blob, signal?: AbortSignal): Promise<string> {
  const h = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
  const pending = new Uint8Array(64), w = new Uint32Array(64);
  let used=0, processed=0, yielded=0;
  function block(bytes: Uint8Array) {
    const view=new DataView(bytes.buffer,bytes.byteOffset,64);
    for(let i=0;i<16;i++)w[i]=view.getUint32(i*4);
    for(let i=16;i<64;i++) {
      const a=w[i-15],b=w[i-2];
      w[i]=(w[i-16]+(rotate(a,7)^rotate(a,18)^(a>>>3))+w[i-7]+(rotate(b,17)^rotate(b,19)^(b>>>10)))>>>0;
    }
    let [a,b,c,d,e,f,g,v]=h;
    for(let i=0;i<64;i++) {
      const t=(v+(rotate(e,6)^rotate(e,11)^rotate(e,25))+((e&f)^(~e&g))+K[i]+w[i])>>>0;
      const u=((rotate(a,2)^rotate(a,13)^rotate(a,22))+((a&b)^(a&c)^(b&c)))>>>0;
      v=g;g=f;f=e;e=(d+t)>>>0;d=c;c=b;b=a;a=(t+u)>>>0;
    }
    [a,b,c,d,e,f,g,v].forEach((n,i)=>h[i]=(h[i]+n)>>>0);
  }
  const reader=blob.stream().getReader();
  try {
    while(true){
      signal?.throwIfAborted();
      const {done,value}=await reader.read();if(done)break;
      processed+=value.length;
      if(processed-yielded>=8*1024*1024){await new Promise(resolve=>setTimeout(resolve,0));yielded=processed;}
      let cursor=0;
      while(cursor<value.length){
        const take=Math.min(64-used,value.length-cursor);pending.set(value.subarray(cursor,cursor+take),used);used+=take;cursor+=take;
        if(used===64){block(pending);used=0;}
      }
    }
  } finally {await reader.cancel().catch(()=>{});reader.releaseLock();}
  pending[used++]=0x80;
  if(used>56){pending.fill(0,used);block(pending);used=0;}
  pending.fill(0,used,56);
  const bits=BigInt(blob.size)*8n, view=new DataView(pending.buffer);
  view.setUint32(56,Number(bits>>32n));view.setUint32(60,Number(bits&0xffffffffn));block(pending);
  return h.map(n=>n.toString(16).padStart(8,'0')).join('');
}
