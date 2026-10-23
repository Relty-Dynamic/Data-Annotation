export type AccountRole = 'admin'|'leader'|'member';
export const accountRoleNames:Record<AccountRole,string>={admin:'管理员',leader:'组长',member:'成员'};
export function accountRole(account:{role:string;task_roles?:readonly string[]}):AccountRole {
 if(account.role==='admin'||account.role==='leader'||account.role==='member')return account.role;
 return account.task_roles?.includes('leader')?'leader':'member';
}
export function isManager(account:{role?:string},legacyCapabilities?:readonly string[]):boolean {
 return account.role==='admin'||account.role==='leader'||account.role==='annotator'&&legacyCapabilities?.includes('leader')===true;
}
