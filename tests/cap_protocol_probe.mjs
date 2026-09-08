// End-to-end proof validation against an isolated local QA server only.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { prng } from '../cap_service/node_modules/capjs-core/src/prng.js';

const base = new URL(process.argv[2] || 'http://127.0.0.1:5069');
assert(['127.0.0.1', 'localhost'].includes(base.hostname), 'Use a local isolated QA server');
const cookies = new Map();
async function send(path, body) {
  const response = await fetch(new URL(path, base), {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'User-Agent': 'python-requests/maxcourse-cap-probe', 'Origin': base.origin,
      'Content-Type': 'application/json', 'Cookie': [...cookies].map(([k, v]) => `${k}=${v}`).join('; ') },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  for (const raw of response.headers.getSetCookie()) {
    const [part] = raw.split(';'), split = part.indexOf('=');
    cookies.set(part.slice(0, split), part.slice(split + 1));
  }
  return { status: response.status, data: await response.json() };
}
const blocked = await send('/api/semesters');
assert.equal(blocked.data.code, 'human_verification_required');
const { data: challenge } = await send('/api/human/challenge', {});
const { c, s, d } = challenge.challenge;
const solutions = [];
for (let i = 1; i <= c; i++) {
  const salt = prng(challenge.token + i, s), target = prng(challenge.token + i + 'd', d);
  let nonce = 0;
  while (!createHash('sha256').update(salt + nonce).digest('hex').startsWith(target)) nonce++;
  solutions.push(nonce);
}
const proof = { token: challenge.token, solutions };
assert.equal((await send('/api/human/redeem', proof)).data.success, true);
assert.equal((await send('/api/human/status')).data.verified, true);
assert.equal((await send('/api/semesters')).status, 200);
assert.equal((await send('/api/human/redeem', proof)).status, 400);
console.log('Local protocol passed: suspicion → official PoW → bound cookie → resumed API; replay rejected.');
