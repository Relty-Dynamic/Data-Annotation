"""Account administration through the existing local or Ubuntu account store."""
from fastapi import HTTPException
from ..auth import EMAIL
from ..roles import ACCOUNT_ROLES

DEFAULT_PASSWORD = '1234567890'


def administer(auth, actor, action, body):
    if actor.get('role') != 'admin':
        raise HTTPException(403, '只有超级管理员可管理账号。')
    if action == 'list':
        return auth.list_users()
    if action == 'create':
        email = body.get('email')
        name = body.get('display_name')
        password = body.get('password', DEFAULT_PASSWORD)
        if not isinstance(email, str) or len(email.strip()) > 254 or not EMAIL.fullmatch(email.strip()):
            raise HTTPException(422, '请填写有效邮箱账号。')
        if not isinstance(name, str) or not isinstance(password, str):
            raise HTTPException(422, '请填写姓名和初始密码。')
        email = email.strip().lower()
        # Explicit old-client requests keep their legacy grants until migration.
        role = body.get('role', 'annotator' if 'roles' in body else 'member')
        if role not in (*ACCOUNT_ROLES, 'annotator'):
            raise HTTPException(422, '请选择管理员、组长或成员。')
        return auth.create_user(email, name, password, role=role, email=email, email_login=True)
    user_id = body.get('user_id')
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(422, '请选择账号。')
    if action == 'role':
        return {'ok': True, 'user': auth.set_role(user_id, body.get('role'), actor['id'])}
    if action == 'upload':
        if type(body.get('can_upload')) is not bool:
            raise HTTPException(422, '请指定上传权限。')
        auth.set_upload_permission(user_id, body['can_upload'])
    elif action == 'active':
        if type(body.get('active')) is not bool:
            raise HTTPException(422, '请指定账号是否启用。')
        auth.set_active(user_id, body['active'])
    elif action == 'password':
        if not isinstance(body.get('password'), str):
            raise HTTPException(422, '请填写新密码。')
        auth.reset_password(user_id, body['password'])
    elif action == 'delete':
        auth.delete_user(user_id, actor['id'])
    else:
        raise HTTPException(422, '未知账号操作。')
    return {'ok': True}


class LocalAccounts:
    def __init__(self, auth):
        self.auth = auth

    def __call__(self, action, actor, body, request):
        return administer(self.auth, actor, action, body)


class RemoteAccounts:
    def __init__(self, accounts):
        self.accounts = accounts

    def __call__(self, action, actor, body, request):
        return self.accounts.request('POST', '/v1/admin/accounts', json={
            'token': request.cookies.get('datamark_session'),
            'csrf': request.cookies.get('datamark_csrf'), 'action': action, 'body': body})['result']
