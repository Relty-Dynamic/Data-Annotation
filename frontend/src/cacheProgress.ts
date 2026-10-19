import type {CacheMetrics} from './browserVideoCache.ts';

export function cacheProgressText(ready:number,total:number,metrics:CacheMetrics) {
  const mb=(value:number)=>(value/1024/1024).toFixed(1)+' MB';
  const seconds=(value:number)=>Math.floor(value/1000)+' 秒';
  return `已就绪 ${ready}/${total} 个文件（${mb(metrics.readyBytes)}/${mb(metrics.totalBytes)}）；`+
    `已下载 ${mb(metrics.downloadedBytes)}（${metrics.downloading} 路处理中）；`+
    `已完整校验 ${mb(metrics.checkedBytes)}（${metrics.checking} 路处理中）；`+
    `${metrics.saving?'正在保存登记；':''}已用时 ${seconds(metrics.elapsedMs)}`;
}
