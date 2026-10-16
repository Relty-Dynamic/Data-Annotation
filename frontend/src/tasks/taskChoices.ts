import type {Account} from '../auth';
import type {TaskItem,TaskRole} from './types';
import {taskStatusName} from './types.ts';
export interface TaskChoice {key:string;id:string;kind:'annotate'|'review';name:string;detail:string;status:string;url:string;waiting:boolean}
export function assignedTaskChoices(user:Pick<Account,'id'>&Partial<Pick<Account,'role'>>,tasks:TaskItem[],roles:TaskRole[]=[]):TaskChoice[]{
 return tasks.filter(task=>!['deleted','pending','unassigned'].includes(task.status)).flatMap(task=>{
  const base={id:task.id,name:task.collection?`${task.collection.name} · ${task.collection.videos.length} 段视频`:task.video?.collection_name?`${task.video.collection_name} · ${task.video.video_name??task.video_id}`:task.video?.video_name??task.video_id,detail:`${task.rule?.name??'标注任务'} · 截止 ${task.deadline}`,status:taskStatusName(task)};
  const own=task.annotator_ids.includes(user.id),manager=user.role==='admin'||roles.includes('leader');
  return [...(own?[{...base,key:task.id+':annotate',kind:'annotate' as const,url:'?task='+encodeURIComponent(task.id),waiting:false}]:[]),...((manager||!own&&task.reviewer_id===user.id)?[{...base,key:task.id+':review',kind:'review' as const,url:'?task='+encodeURIComponent(task.id)+'&review=1',waiting:!['submitted','reviewing','adjudicating','accepted','delivered'].includes(task.status)}]:[])];
 }).sort((a,b)=>Number(a.waiting)-Number(b.waiting)||a.name.localeCompare(b.name));
}
export function reviewStage(task:Pick<TaskItem,'status'|'review_complete'>):string{
 if(task.status==='reviewing'&&task.review_complete)return '复核已完成，等待管理员或组长验收';
 return ({submitted:'待开始复核',reviewing:'复核进行中',adjudicating:'存在待处理差异，请裁定或退回返修',rework:'已退回返修，等待标注员重新提交',accepted:'已验收，结果只读',delivered:'已交付，结果只读'} as Record<string,string>)[task.status]??'等待标注提交';
}
