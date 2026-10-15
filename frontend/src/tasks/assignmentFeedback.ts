export async function createTaskBatch(videoIds:string[],pendingOnly:boolean,create:(videoId:string)=>Promise<unknown>):Promise<string>{
 if(!videoIds.length)throw new Error('请先选择合格视频。');
 let completed=0;
 for(const videoId of videoIds){
  try{await create(videoId);completed++;}
  catch(error){throw new Error(`已${pendingOnly?'创建':'分配'} ${completed}/${videoIds.length} 个任务；视频 ${videoId} 未成功：${error instanceof Error?error.message:String(error)}。请核对任务列表后重试未完成的视频。`);}
 }
 return pendingOnly?`创建成功：${completed} 个任务待分配。`:`分配成功：已创建并分配 ${completed} 个任务。`;
}
