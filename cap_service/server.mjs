import http from 'node:http';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { realpathSync } from 'node:fs';
import { generateChallenge, validateChallenge } from 'capjs-core';

export function createCapServer({ bridgeSecret, challengeOptions = {} } = {}) {
  if (!bridgeSecret || bridgeSecret.length < 32) throw new Error('CAP bridge secret is required');
  // A fresh signing key invalidates outstanding challenges after restart. This
  // keeps replay protection correct without a persistent nonce database.
  const signingKey = randomBytes(48).toString('hex');
  const usedNonces = new Map();
  const secret = Buffer.from(bridgeSecret);
  const cleanup = setInterval(() => {
    for (const [key, until] of usedNonces) if (until < Date.now()) usedNonces.delete(key);
  }, 30000).unref();
  const server = http.createServer(async (req, res) => {
    const reply = (status, body) => {
      res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      res.end(JSON.stringify(body));
    };
    const supplied = Buffer.from(req.headers['x-cap-service-secret'] || '');
    if (supplied.length !== secret.length || !timingSafeEqual(supplied, secret)) {
      return reply(401, { error: 'unauthorized' });
    }
    if (req.method === 'GET' && req.url === '/health') return reply(200, { ok: true });
    if (req.method !== 'POST' || !['/challenge', '/redeem'].includes(req.url)) {
      return reply(404, { error: 'not_found' });
    }
    try {
      let size = 0, chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 16384) return reply(413, { error: 'body_too_large' });
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString());
      if (!body || typeof body.scope !== 'string' || !/^[a-f0-9]{64}$/.test(body.scope)) {
        return reply(400, { error: 'invalid_scope' });
      }
      if (usedNonces.size >= 10000) return reply(503, { error: 'capacity' });
      if (req.url === '/challenge') {
        return reply(200, await generateChallenge(signingKey, {
          expiresMs: 300000, ...challengeOptions, scope: body.scope,
          instrumentation: false,
        }));
      }
      const proof = body.proof;
      if (!proof || typeof proof.token !== 'string' || proof.token.length > 8192 ||
          !Array.isArray(proof.solutions) || proof.solutions.length > 100 ||
          !proof.solutions.every(n => Number.isSafeInteger(n) && n >= 0)) {
        return reply(400, { success: false, error: 'invalid_proof' });
      }
      const result = await validateChallenge(signingKey, proof, {
        scope: body.scope,
        tokenTtlMs: 900000,
        consumeNonce: async (key, ttl) => {
          if (usedNonces.has(key)) return false;
          usedNonces.set(key, Date.now() + ttl);
          return true;
        },
      });
      // Flask grants a bound HttpOnly clearance on this authenticated response.
      // The widget's opaque token is never accepted as authorization elsewhere.
      return reply(result.success ? 200 : 400, result.success
        ? { success: true, token: result.token, expires: result.expires }
        : { success: false, error: result.reason });
    } catch {
      return reply(400, { success: false, error: 'invalid_request' });
    }
  });
  server.requestTimeout = 10000;
  server.headersTimeout = 5000;
  server.on('close', () => clearInterval(cleanup));
  return server;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === realpathSync(process.argv[1])) {
  createCapServer({ bridgeSecret: process.env.MAXCOURSE_CAP_SECRET })
    .listen(Number(process.env.MAXCOURSE_CAP_PORT || 5068), '127.0.0.1', () => {
      console.log('MAXCOURSE Cap verification service ready on loopback');
    });
}
