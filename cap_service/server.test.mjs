import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createCapServer } from './server.mjs';
import { prng } from './node_modules/capjs-core/src/prng.js';
import { mkdtempSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const secret = 'test-only-bridge-secret-not-for-production';
const scope = 'a'.repeat(64);

test('service starts when systemd invokes the current-release symlink', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'cap-entry-'));
  const entry = join(dir, 'server.mjs');
  symlinkSync(fileURLToPath(new URL('./server.mjs', import.meta.url)), entry);
  const child = spawn(process.execPath, [entry], { env: { ...process.env, MAXCOURSE_CAP_SECRET: secret, MAXCOURSE_CAP_PORT: '0' } });
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Service entry did not listen')), 5000);
      child.stdout.on('data', output => { if (String(output).includes('ready on loopback')) { clearTimeout(timer); resolve(); } });
      child.once('exit', code => { clearTimeout(timer); reject(new Error(`Service exited before listen: ${code}`)); });
      child.once('error', reject);
    });
  } finally { child.kill(); rmSync(dir, { recursive: true, force: true }); }
});

export function solveFixture(challenge) {
  const { token, challenge: { c, s, d } } = challenge;
  const solutions = [];
  for (let i = 1; i <= c; i++) {
    const salt = prng(token + i, s), target = prng(token + i + 'd', d);
    let value = 0;
    while (!createHash('sha256').update(salt + value).digest('hex').startsWith(target)) value++;
    solutions.push(value);
  }
  return { token, solutions };
}

test('official verifier enforces proof, scope, auth, expiry, and single redemption', async () => {
  const server = createCapServer({ bridgeSecret: secret, challengeOptions: { challengeCount: 2, challengeDifficulty: 1 } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const post = (path, body, key = secret) => fetch(base + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Cap-Service-Secret': key }, body: JSON.stringify(body),
  });
  try {
    assert.equal((await post('/challenge', { scope }, 'wrong')).status, 401);
    const challenge = await (await post('/challenge', { scope })).json();
    const proof = solveFixture(challenge);
    const invalid = { ...proof, solutions: [-1, -1] };
    assert.equal((await post('/redeem', { scope, proof: invalid })).status, 400);
    const wrongScope = await (await post('/redeem', { scope: 'b'.repeat(64), proof })).json();
    assert.equal(wrongScope.error, 'scope_mismatch');
    const first = await (await post('/redeem', { scope, proof })).json();
    assert.equal(first.success, true);
    assert(first.token);
    const second = await (await post('/redeem', { scope, proof })).json();
    assert.equal(second.error, 'already_redeemed');
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
  const expiredServer = createCapServer({ bridgeSecret: secret, challengeOptions: { expiresMs: -10000 } });
  await new Promise(resolve => expiredServer.listen(0, '127.0.0.1', resolve));
  try {
    const url = `http://127.0.0.1:${expiredServer.address().port}`;
    const headers = { 'Content-Type': 'application/json', 'X-Cap-Service-Secret': secret };
    const ch = await (await fetch(url + '/challenge', { method: 'POST', headers, body: JSON.stringify({ scope }) })).json();
    const res = await (await fetch(url + '/redeem', { method: 'POST', headers,
      body: JSON.stringify({ scope, proof: { token: ch.token, solutions: Array(ch.challenge.c).fill(0) } }) })).json();
    assert.equal(res.error, 'expired');
  } finally {
    expiredServer.closeAllConnections();
    await new Promise(resolve => expiredServer.close(resolve));
  }
});
