export async function createTaskBatch(collectionIds:string[],pendingOnly:boolean,create:(collectionId:string)=>Promise<unknown>):Promise<string>{
 if(!collectionIds.length)throw new Error('请先选择完整且质检合格的采集文件夹。');
 let completed=0;
 for(const collectionId of collectionIds){
  try{await create(collectionId);completed++;}
  catch(error){throw new Error(`已${pendingOnly?'创建':'分配'} ${completed}/${collectionIds.length} 个任务；采集 ${collectionId} 未成功：${error instanceof Error?error.message:String(error)}。请核对任务列表后重试未完成的采集。`);}
 }
 return pendingOnly?`创建成功：${completed} 个任务待分配。`:`分配成功：已创建并分配 ${completed} 个任务。`;
}
