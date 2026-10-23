import type {Account} from '../auth';
import {isManager} from '../accountRoles.ts';
import type {TaskItem,TaskCapability} from './types';

export function canAssignAnnotator(account:{active:boolean;role?:string;task_access?:boolean},roles:TaskCapability[]):boolean{
 if(!account.active||account.task_access===false)return false;
 if(account.role==='member'||account.role==='leader'||account.role==='admin')return true;
 return roles.includes('leader')||roles.includes('annotator');
}

export function canReviewTask(user:Pick<Account,'id'|'role'|'task_access'>,roles:TaskCapability[],task:Pick<TaskItem,'annotator_ids'|'reviewer_id'>):boolean{
 if(user.task_access===false)return false;
 if(isManager(user,roles))return true;
 return !task.annotator_ids.includes(user.id)&&task.reviewer_id===user.id;
}

export function canOpenReviewWorkspace(user:Pick<Account,'id'|'role'>,roles:TaskCapability[],task:Pick<TaskItem,'annotator_ids'|'reviewer_id'|'status'>):boolean{
 return ['submitted','reviewing','adjudicating','accepted','delivered'].includes(task.status)&&canReviewTask(user,roles,task);
}

export function canUseLegacyTrackTools(user:Pick<Account,'role'>,tasksEnabled:boolean):boolean{
 return user.role==='admin'||!tasksEnabled;
}

export function canAssignReviewer(account:{active:boolean;role?:string;task_access?:boolean},roles:TaskCapability[]):boolean{
 if(!account.active||account.task_access===false)return false;
 if(account.role==='member'||account.role==='leader'||account.role==='admin')return true;
 return roles.includes('leader')||roles.includes('reviewer');
}
