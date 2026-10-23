import type {TaskCapability} from './types';
import {accountRole} from '../accountRoles.ts';

export type AccountRoleFilter = 'all' | 'admin' | 'member' | 'none' | TaskCapability;
type FilterableAccount = {display_name:string; role:string; task_roles:readonly TaskCapability[]};

export function filterAccounts<T extends FilterableAccount>(accounts:readonly T[], name:string, role:AccountRoleFilter):T[] {
  const query=name.trim().toLocaleLowerCase();
  return accounts.filter(account=>{
    if(query&&!account.display_name.toLocaleLowerCase().includes(query))return false;
    if(role==='all')return true;
    if(role==='admin')return account.role==='admin';
    if(role==='member'||role==='leader')return accountRole(account)===role;
    if(role==='none')return account.role!=='admin'&&account.task_roles.length===0;
    return account.task_roles.includes(role);
  });
}
