import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
if (process.env.VERCEL && !process.env.VITE_DATAMARK_API_ORIGIN) {
  throw new Error('Vercel 构建必须设置 VITE_DATAMARK_API_ORIGIN 为 Ubuntu 公网 HTTPS 接口域名。');
}
export default defineConfig({plugins:[react()],server:{host:'127.0.0.1',proxy:{'/api':{target:'http://127.0.0.1:8765',ws:true}}}});
