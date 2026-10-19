"""Use the existing public rule API with the caller's shared-account session."""
import json
import re

import httpx
from fastapi import HTTPException

PUBLIC_API = 'http://datamark-public-setup-api-1:8765'
PUBLIC_HOST = 'api-annotate.reltydynamic.com'
PUBLIC_ORIGIN = 'https://datamark.reltydynamic.com'
MAX_RESPONSE = 4 * 1024**2


class PublicRules:
    def __init__(self, *, transport=None):
        self.transport = transport

    def request(self, request, method='GET', rule_id=None, body=None):
        if method not in ('GET', 'POST', 'DELETE') or (method == 'DELETE') != (rule_id is not None):
            raise ValueError('Unsupported public rule operation')
        if rule_id is not None and not re.fullmatch('[0-9a-f]{32}', rule_id):
            raise HTTPException(404, '规则不存在。')
        session, csrf = (request.cookies.get(key, '') for key in ('datamark_session', 'datamark_csrf'))
        if not session or not csrf:
            raise HTTPException(401, '登录状态不完整，请重新登录。')
        headers = {'Host': PUBLIC_HOST, 'Origin': PUBLIC_ORIGIN}
        if method != 'GET':
            headers['X-CSRF-Token'] = request.headers.get('x-csrf-token', '')
        cookies = {'datamark_session': session, 'datamark_csrf': csrf}
        path = '/api/tasks/rules' + ('/' + rule_id if rule_id else '')
        try:
            with httpx.Client(base_url=PUBLIC_API, timeout=10, trust_env=False, follow_redirects=False,
                              transport=self.transport, headers=headers, cookies=cookies) as client:
                with client.stream(method, path, json=body) as response:
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > MAX_RESPONSE:
                            raise HTTPException(502, '公网规则返回内容过大。')
                    try:
                        result = json.loads(payload)
                    except ValueError:
                        raise HTTPException(502, '公网规则返回格式无效。') from None
                    if response.status_code != 200:
                        if response.status_code in (401, 403, 404, 409, 422):
                            detail = result.get('detail') if isinstance(result, dict) else None
                            if response.status_code == 403:
                                detail = '公网规则权限不足，请由管理员核对公网任务角色。'
                            raise HTTPException(response.status_code, detail or '公网规则操作未通过校验。')
                        raise HTTPException(503, '公网规则服务暂时不可用，请稍后重试。')
                    if not isinstance(result, list if method == 'GET' else dict):
                        raise HTTPException(502, '公网规则返回格式无效。')
                    return result
        except httpx.HTTPError:
            # A timed-out write may already be committed: never retry it automatically.
            detail = ('公网规则服务连接失败，请稍后重试。' if method == 'GET' else
                      '公网规则操作结果尚未确认，请刷新规则列表核对后再操作。')
            raise HTTPException(503, detail) from None

    def for_task(self, request, rule_id):
        for rule in self.request(request):
            if isinstance(rule, dict) and rule.get('id') == rule_id:
                return rule
        raise HTTPException(409, '所选规则已删除或不属于当前公网规则，请刷新后重新选择。')
