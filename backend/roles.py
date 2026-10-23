"""Account roles are identities; task capabilities are derived permissions."""
ACCOUNT_ROLES = ('admin', 'leader', 'member')
ROLE_MODEL = 'three-role-v1'
ROLE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS account_roles (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('admin','leader','member')),
    task_access INTEGER NOT NULL CHECK(task_access IN (0,1))
);
CREATE TABLE IF NOT EXISTS account_role_events (
    id TEXT PRIMARY KEY, actor_id TEXT, user_id TEXT, at INTEGER NOT NULL,
    previous_role TEXT, role TEXT NOT NULL, task_access INTEGER NOT NULL
);
'''


def capabilities(account):
    if account.get('task_access') is False:
        return []
    role = account.get('role')
    if role in ('admin', 'leader'):
        return ['annotator', 'leader', 'reviewer']
    if role == 'member':
        return ['annotator', 'reviewer']
    return []


def canonical(account):
    return account.get('role_model') == ROLE_MODEL or account.get('role') in ('leader', 'member')


def is_project_member(account):
    """Preserve legacy project claiming and isolation for non-admin identities."""
    return bool(account) and account.get('role') in ('annotator', 'member', 'leader')


def legacy_role(roles):
    if not isinstance(roles, list) or any(r not in ('annotator', 'reviewer', 'leader') for r in roles):
        raise ValueError('旧任务角色格式不正确')
    return 'leader' if 'leader' in roles else 'member'
