const test = require('node:test');
const assert = require('node:assert/strict');
const search = require('../sms-lab/search.js');
const openai = { code: 'dr', name: 'OpenAI', aliases: ['chatgpt', 'gpt', 'open ai'] };

test('brand aliases survive spaces, punctuation, and case', () => {
  for (const query of ['ChatGPT', 'Chat GPT', 'open-ai', 'GPT']) {
    assert.equal(search.matches(openai, query), true);
  }
});

test('unrelated queries do not match OpenAI', () => {
  assert.equal(search.matches(openai, 'telegram'), false);
});
