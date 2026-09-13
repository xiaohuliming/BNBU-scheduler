const test = require('node:test');
const assert = require('node:assert/strict');
const search = require('../sms-lab/search.js');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const openai = { code: 'dr', name: 'OpenAI', aliases: ['chatgpt', 'gpt', 'open ai'] };

test('brand aliases survive spaces, punctuation, and case', () => {
  for (const query of ['ChatGPT', 'Chat GPT', 'open-ai', 'GPT']) {
    assert.equal(search.matches(openai, query), true);
  }
});

test('unrelated queries do not match OpenAI', () => {
  assert.equal(search.matches(openai, 'telegram'), false);
});

test('GPT-4 and Chinese AI search use the backend OpenAI aliases', () => {
  const root = path.join(__dirname, '..');
  const python = process.env.MAXCOURSE_TEST_PYTHON || path.join(root, 'venv/bin/python');
  const service = JSON.parse(execFileSync(python, ['-c', `
import json
from tests.test_sms_lab import SMSLabRouteTest
case = SMSLabRouteTest()
case.setUp()
try:
    services = case.client.get('/api/sms-lab/services').get_json()['services']
    print(json.dumps(next(service for service in services if service['code'] == 'dr')))
finally:
    case.doCleanups()
`], { cwd: root, encoding: 'utf8' }));
  for (const query of ['GPT-4', '人工智能']) {
    assert.equal(search.matches(service, query), true, query);
  }
});
