import type {HelperStatus} from './uploadBridge';
export function completedUpload(helper:HelperStatus|null):{key:string;prefix:string}|null{
 const job=helper?.report.job;
 if(!helper?.online||helper.command||helper.report.busy||helper.report.error||!job?.id||!job.prefix||job.state!=='ready')return null;
 return {key:helper.id+':'+job.id,prefix:job.prefix};
}
/** Polling and reopening a dialog must not repeat the same completion notice. */
export function claimUploadCompletion(helper:HelperStatus|null,seen:Set<string>){
 const completion=completedUpload(helper);
 if(!completion||seen.has(completion.key))return null;
 seen.add(completion.key);return completion;
}
export function uploadStateName(state?:string):string{
 return ({preparing_prediction:'正在生成姿势预标注',ready_to_compress:'等待上传',ready_to_upload:'等待上传',compressing:'正在压缩',uploading:'正在上传',validating:'正在核验并发布',ready:'上传完成',error:'上传未完成'} as Record<string,string>)[state??'']??'正在处理';
}
