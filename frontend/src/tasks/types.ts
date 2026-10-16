import type {Segment} from '../domain';
export type TaskRole = 'annotator'|'reviewer'|'leader';
export interface TaskAxis {id:string;name:string;mode:'state'|'event';labels:string[];minimum_ms?:number;transport_labels?:string[];transport_types?:string[]}
export interface TaskRule {id:string;name:string;task_type:string;axes:TaskAxis[];boundary_tolerance_ms?:number|null}
export type TaskSegment = Segment & {transport_type?:string;needs_review?:boolean;review_reason?:string};
export type TaskAnnotations = Record<string,TaskSegment[]>;
export interface TaskVideo {collection_id?:string;collection_name?:string;collection_video_count?:number;video_count?:number;video_name?:string;id:string;collector_id:string;date:string;scene:string;duration_ms:number;quality:string;uploader_id?:string;source:{backend:string;key:string}}
export interface TaskCollection {id:string;backend:string;name:string;duration_ms:number;recording_start:string;videos:Array<TaskVideo&{start_ms:number;end_ms:number;recording_start:string}>}
export interface TaskIssue {id:string;axis:string;segment_id?:string;start_ms?:number;end_ms?:number;detail?:string;reason?:string;resolved?:boolean}
export interface TaskVersion {id:string;user_id:string;actor_id?:string;annotations:TaskAnnotations;created_at?:string;at?:string}
export interface TaskItem {output_sync?:import('./TaskOutputSync').TaskOutputStatus|null;collection?:TaskCollection;video_ids?:string[];review_complete?:boolean;id:string;video_id:string;rule_id:string;status:string;revision:number;annotator_ids:string[];reviewer_id:string;deadline:string;rule?:TaskRule;video?:TaskVideo;participants?:Array<{id:string;display_name:string}>;reviewed_by?:string;adjudicated_by?:string;review_notes?:Array<{actor_id:string;axis:string;start_ms?:number;end_ms?:number;reason:string;at:string}>;submissions?:TaskVersion[];issues?:TaskIssue[];differences?:Array<{axis:string;start_ms?:number;end_ms?:number;detail?:string}>;final?:TaskVersion;history?:{submissions:TaskVersion[];finals:TaskVersion[]}}
export const statusNames:Record<string,string>={deleted:'已删除',pending:'待分配',unassigned:'待分配',annotating:'标注中',submitted:'已提交',reviewing:'复核中',rework:'返修中',returned:'返修中',revision:'返修中',adjudicating:'待裁定',accepted:'已验收',delivered:'已交付'};
export function labels(text:string):string[]{return [...new Set(text.split(/[，,\n]/).map(x=>x.trim()).filter(Boolean))]}
export function updateTaskSegment(annotations:TaskAnnotations,axis:string,id:string,patch:Partial<TaskSegment>):TaskAnnotations{return {...annotations,[axis]:(annotations[axis]??[]).map(segment=>segment.id===id?{...segment,...patch}:segment)}}
export function composeFinal(axes:TaskAxis[],versions:TaskVersion[],chosen:Record<string,boolean>):{annotations:TaskAnnotations;contributors:Record<string,string[]>}{
 const annotations:TaskAnnotations={},contributors:Record<string,string[]>={};
 for(const axis of axes){annotations[axis.id]=[];versions.forEach((version,index)=>{for(const segment of version.annotations[axis.id]??[]){const key=`${axis.id}:${index}:${segment.id}`;if(chosen[key]??index===0){const id=`${index}-${segment.id}`;annotations[axis.id].push({...segment,id});contributors[`${axis.id}:${id}`]=[version.user_id??version.actor_id??''];}}});}
 return {annotations,contributors};
}
export function parseVideoCsv(text:string):Array<Record<string,string>>{
 const rows:string[][]=[];let row:string[]=[],cell='',quoted=false;
 const input=text.replace(/^\uFEFF/,'');
 for(let i=0;i<input.length;i++){const c=input[i];if(c==='"'){if(quoted&&input[i+1]==='"'){cell+='"';i++;}else quoted=!quoted;}else if(c===','&&!quoted){row.push(cell);cell='';}else if((c==='\n'||c==='\r')&&!quoted){if(c==='\r'&&input[i+1]==='\n')i++;row.push(cell);if(row.some(v=>v.trim()))rows.push(row);row=[];cell='';}else cell+=c;}
 if(quoted)throw new Error('CSV 引号未闭合。');row.push(cell);if(row.some(v=>v.trim()))rows.push(row);
 const header=rows.shift()?.map(v=>v.trim())??[];const required=['source_id','video_id','collector_id','date','scene','quality'];if(required.some(h=>!header.includes(h)))throw new Error('请使用下载的素材登记模板。');
 return rows.map(row=>{if(row.length!==header.length)throw new Error('CSV 列数与模板不一致。');return Object.fromEntries(header.map((h,i)=>[h,row[i].trim()]));});
}
export function videoCsvTemplate(sources:Array<{id:string;name:string;collection:string}>):string{const quote=(v:string)=>`"${v.replaceAll('"','""')}"`;return '\uFEFFsource_id,video_id,collector_id,date,scene,quality,video_name,collection\r\n'+sources.map(s=>[s.id,'','','','','unknown',s.name,s.collection].map(quote).join(',')).join('\r\n');}

export function issueRange(issue:TaskIssue):{start_ms:number;end_ms:number}|null {return Number.isFinite(issue.start_ms)&&Number.isFinite(issue.end_ms)&&issue.end_ms!>issue.start_ms!?{start_ms:issue.start_ms!,end_ms:issue.end_ms!}:null;}

export function taskTitle(task:TaskItem):string{return task.collection?.name??task.video?.video_name??task.video_id}

export function taskStatusName(task:{status:string;review_complete?:boolean}):string{
 return task.status==='reviewing'&&task.review_complete?'待验收':statusNames[task.status]??task.status;
}
