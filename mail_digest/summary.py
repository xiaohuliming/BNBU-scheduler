"""Narrow authenticated boundary to the sibling OmniChat service."""
import json
import requests
from .client import MailError

OMNI = 'https://chat.bnbscheduler.top/api/integrations/mail-summary'


def omni_request(token, messages=None):
    if not token or '\r' in token or '\n' in token:
        raise MailError('请重新登录 MAXCOURSE，以连接 OmniChat 共享账号。', 'shared_login_required', 401)
    try:
        with requests.Session() as http:
            response = http.request('POST' if messages is not None else 'GET',
                                    OMNI if messages is not None else OMNI + '/config',
                                    json={'messages': messages} if messages is not None else None,
                                    headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
                                    timeout=(8, 120), allow_redirects=False)
        errors = {401: ('请重新登录以连接 OmniChat。', 'shared_login_required'),
                  402: ('OmniChat 积分不足，请充值后重试。', 'insufficient_credits'),
                  403: ('OmniChat 账号暂时不可用。', 'omnichat_account_unavailable'),
                  404: ('OmniChat 邮件总结接口或模型尚未就绪。', 'summary_not_configured'),
                  429: ('总结请求较多，请稍后重试。', 'summary_rate_limited')}
        if response.status_code in errors:
            message, code = errors[response.status_code]
            raise MailError(message, code, response.status_code)
        if response.status_code != 200:
            raise MailError('OmniChat 暂时无法生成总结，请稍后重试。', 'summary_unavailable')
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (requests.RequestException, ValueError, TypeError):
        raise MailError('OmniChat 连接中断，暂时无法确认生成结果。请稍后再试。', 'summary_unavailable') from None


def summarize(token, messages):
    payload = omni_request(token, messages)
    try:
        choice = payload['choices'][0]
        if choice.get('finish_reason') not in (None, 'stop', 'end_turn'):
            raise ValueError()
        text = choice['message']['content'].strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        digest = json.loads(text)
        if not isinstance(digest, dict) or not isinstance(digest.get('overview'), str):
            raise ValueError()
        items = digest['items']
        if not isinstance(items, list) or len(items) != len(messages):
            raise ValueError()
        ids = {m['id'] for m in messages}
        seen, clean = set(), []
        for item in items:
            if not isinstance(item, dict) or item.get('id') not in ids or item['id'] in seen:
                raise ValueError()
            if item.get('priority') not in ('action', 'info'):
                raise ValueError()
            seen.add(item['id'])
            if not all(isinstance(item.get(k), str) and len(item[k]) <= 2000 for k in ('summary', 'action', 'deadline')):
                raise ValueError()
            clean.append({key: item[key] for key in ('id', 'summary', 'priority', 'action', 'deadline')})
        return {'overview': digest['overview'][:3000], 'items': clean, 'model': payload.get('model', ''),
                'credits': payload.get('usage', {}).get('credits')}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise MailError('AI 返回的总结不完整，未展示不可靠结果。可稍后重试或查看原文。', 'invalid_summary') from None
