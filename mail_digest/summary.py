"""Server-funded, bounded weekly briefs through the sibling OmniChat relay."""
import os
import requests
from .client import MailError

OMNI = 'https://chat.bnbscheduler.top/api/integrations/mail-brief'


def configured():
    return bool(os.getenv('MAXCOURSE_MAIL_BRIEF_TOKEN', '').strip())


def summarize_week(messages, window_start, window_end):
    token = os.getenv('MAXCOURSE_MAIL_BRIEF_TOKEN', '').strip()
    if not token:
        raise MailError('邮件摘要服务尚未配置。', 'not_configured', 503)
    try:
        with requests.Session() as http:
            response = http.post(OMNI, json={'messages': messages,
                                  'window_start': int(window_start), 'window_end': int(window_end)},
                                 headers={'X-Mail-Brief-Token': token, 'Accept': 'application/json'},
                                 timeout=(8, 150), allow_redirects=False)
        if response.status_code != 200:
            raise MailError('邮件摘要暂时不可用。', 'summary_unavailable')
        payload = response.json()
        items = payload['items']
        if not isinstance(items, list) or len(items) > 4:
            raise ValueError()
        known = {m['id'] for m in messages}
        clean = []
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get('text'), str)
                    or not 1 <= len(item['text']) <= 180 or not isinstance(item.get('source_ids'), list)
                    or not 1 <= len(item['source_ids']) <= 4
                    or any(not isinstance(mid, str) or mid not in known for mid in item['source_ids'])):
                raise ValueError()
            clean.append({'text': item['text'], 'source_ids': list(dict.fromkeys(item['source_ids']))})
        return clean
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise MailError('邮件摘要暂时不可用。', 'summary_unavailable') from None
