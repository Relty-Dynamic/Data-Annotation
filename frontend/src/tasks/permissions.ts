import type {Account} from '../auth';
import type {TaskItem,TaskRole} from './types';

export function canAssignAnnotator(account:{active:boolean;role?:string},roles:TaskRole[]):boolean{
 return account.active&&(account.role==='admin'||roles.includes('annotator'));
}

export function canReviewTask(user:Pick<Account,'id'|'role'>,roles:TaskRole[],task:Pick<TaskItem,'annotator_ids'|'reviewer_id'>):boolean{
 if(user.role==='admin')return true;
 return !task.annotator_ids.includes(user.id)&&(roles.includes('leader')||task.reviewer_id===user.id);
}
