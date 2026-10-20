import type {HelperStatus,PredictionReport} from './uploadBridge';
export function predictionMessage(prediction?:PredictionReport):string{
 if(!prediction)return '';
 const parts=[prediction.batch_status==='partial'?`部分姿势预标注已完成（${prediction.validated_records}/${prediction.total_records} 段），未完成区间需人工标注。`:'已携带姿势预标注。'];
 const inherited=prediction.inherited_record_ids_total??prediction.inherited_record_ids?.length??0;
 if(inherited)parts.push(`${inherited} 段使用了同日其他记录的校时偏移。`);
 if(prediction.needs_alignment_review)parts.push('请结合视频人工复核校时。');
 if(prediction.needs_mounting_review){
  const paths=prediction.mounting_records?.filter(row=>row.needs_mounting_review).map(row=>row.path).join('、');
  parts.push(`腿部安装方向需要人工复核${paths?`（${paths}）`:''}：方向由本段短时证据估计，请核实佩戴方向并结合视频复核姿势预标注。`);
 }
 if(prediction.details_truncated){
  const excluded=(prediction.excluded_records_total??0)-(prediction.excluded_records?.length??0);
  const mounting=(prediction.mounting_records_total??0)-(prediction.mounting_records?.length??0);
  parts.push(`诊断列表较长，此处省略 ${Math.max(0,excluded)} 条失败详情、${Math.max(0,mounting)} 条安装方向记录及部分编号清单。完整信息保留在原始姿势预测文件中，可联系管理员按诊断编号查看。`);
 }
 return parts.join('');
}
export function predictionFailureReason(reason:string):string{
 return ({inference_failed:'IMU 推理未完成',invalid_prepared_prediction:'推理结果无法读取',alignment_unresolved:'未获得可靠校时',failed:'校时或导出失败',no_video_overlap:'校时后与视频无交集'} as Record<string,string>)[reason]??'预标注未完成';
}
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
