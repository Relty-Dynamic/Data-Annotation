export type UploadStage = {stage:string;state:'waiting'|'running'|'done'|'stopped';completed:number;total:number;active:number;progress:number;elapsed_ms:number};
export type UploadPreparation = {elapsed_ms:number;stages:UploadStage[]};
const labels:Record<string,string>={source:'原视频校验',compress:'压缩与保存',cache:'预览图片',upload:'上传文件',verify:'云端校验',prediction:'姿势预标注',publish:'整组发布'};
export function stageLabel(stage:string){return labels[stage]??'素材处理';}
export function elapsedLabel(ms:number){
 const seconds=Math.max(0,Math.floor(ms/1000));
 return seconds<60?`${seconds} 秒`:`${Math.floor(seconds/60)} 分 ${seconds%60} 秒`;
}
export function stageStatus(value:UploadStage){
 const state={waiting:'等待中',running:'进行中',done:'完成',stopped:'已停止'}[value.state];
 return value.total>1?`${state} · ${value.completed}/${value.total} 段`:state;
}
