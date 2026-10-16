import type {Account} from '../auth';
import type {TaskItem,TaskRole} from './types';

export function canAssignAnnotator(account:{active:boolean;role?:string},roles:TaskRole[]):boolean{
 return account.active&&(account.role==='admin'||roles.includes('leader')||roles.includes('annotator'));
}

export function canReviewTask(user:Pick<Account,'id'|'role'>,roles:TaskRole[],task:Pick<TaskItem,'annotator_ids'|'reviewer_id'>):boolean{
 if(user.role==='admin'||roles.includes('leader'))return true;
 return !task.annotator_ids.includes(user.id)&&task.reviewer_id===user.id;
}

export function canOpenReviewWorkspace(user:Pick<Account,'id'|'role'>,roles:TaskRole[],task:Pick<TaskItem,'annotator_ids'|'reviewer_id'|'status'>):boolean{
 return ['submitted','reviewing','adjudicating','accepted','delivered'].includes(task.status)&&canReviewTask(user,roles,task);
}

export function canUseLegacyTrackTools(user:Pick<Account,'role'>,tasksEnabled:boolean):boolean{
 return user.role==='admin'||!tasksEnabled;
}

export function canAssignReviewer(account:{active:boolean;role?:string},roles:TaskRole[]):boolean{
 return account.active&&(account.role==='admin'||roles.includes('leader')||roles.includes('reviewer'));
}
