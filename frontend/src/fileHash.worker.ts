import {streamingSha256} from './fileHashCore.ts';

self.onmessage = async (event: MessageEvent<Blob>) => {
  try { self.postMessage({hash: await streamingSha256(event.data)}); }
  catch (error) { self.postMessage({error: error instanceof Error ? error.message : String(error)}); }
};
