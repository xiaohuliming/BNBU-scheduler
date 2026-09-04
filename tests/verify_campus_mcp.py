"""Optional local interoperability check with the official MCP SDK.

Run python tests/serve_campus_qa.py in the app environment, then:
  uv run --with 'mcp==1.29.1' python tests/verify_campus_mcp.py
Never prints or persists bearer tokens. Uses only a loopback test account.
"""
import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = 'http://127.0.0.1:5017'


async def main():
    async with httpx.AsyncClient(base_url=BASE, headers={'User-Agent': 'Mozilla/5.0 CampusKnowledgeQA'}) as browser:
        response = await browser.post('/api/login', json={'username': 'knowledge-qa', 'password': 'local-qa-password-only'})
        response.raise_for_status()
        info = await browser.get('/api/knowledge/info')
        info.raise_for_status()
        created = await browser.post('/api/knowledge/tokens', json={'name': 'official-sdk-smoke'})
        created.raise_for_status()
        key = created.json()
        headers = {'Authorization': 'Bearer ' + key['token']}
        try:
            async with streamablehttp_client(BASE + '/mcp', headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) == 3
                    found = await session.call_tool('search_campus', {'query': 'AI专业办公室在哪里', 'kind': 'office'})
                    assert not found.isError
                    results = json.loads(found.content[0].text)['results']
                    assert results and 'T3-602-R12' in results[0]['text']
                    handbooks = await session.call_tool('list_campus_documents', {'kind': 'handbook', 'programme': 'AI', 'cohort': '2026'})
                    assert not handbooks.isError
                    listed = json.loads(handbooks.content[0].text)
                    assert listed['total'] == 1
                    original = await session.call_tool('read_document', {'document_id': listed['documents'][0]['id'], 'page': 1})
                    assert not original.isError
                    assert json.loads(original.content[0].text)['chunks'][0]['citation_url'].endswith('#page=1')
                    courses = await session.call_tool('list_campus_documents', {'kind': 'course', 'limit': 50})
                    data = json.loads(courses.content[0].text)
                    assert data['total'] == info.json()['counts']['course'] and data['next_offset'] == 50
                    print('Official MCP SDK: initialize, tools/list, search, handbook page citations, course pagination passed.')
                    print('Negotiated protocol:', initialized.protocolVersion)
        finally:
            removed = await browser.delete('/api/knowledge/tokens/' + key['id'])
            removed.raise_for_status()
        denied = await browser.post('/api/knowledge/search', headers=headers, json={'query': 'AI'})
        assert denied.status_code == 401
        print('Revoked key immediately denied; no credentials retained.')


if __name__ == '__main__':
    asyncio.run(main())
