// Synthetic DOM interaction tests; no real CAPTCHA or browser is solved here.
// Run with jsdom available through NODE_PATH.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const script = fs.readFileSync(path.join(__dirname, '../human-check/client.js'), 'utf8');
const settle = () => new Promise(resolve => setTimeout(resolve, 10));

function environment() {
  const dom = new JSDOM('<head></head><body><input id="editor" value="keep this text"></body>', {
    url: 'https://maxcourse.test/', runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const w = dom.window, calls = [];
  let verified = false, challenge = true, hardLimit = false;
  w.Request = class extends Request {
    constructor(input, options) { super(typeof input === 'string' ? new URL(input, w.location.href) : input, options); }
  };
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.customElements.define('cap-widget', class extends w.HTMLElement {});
  w.fetch = async input => {
    const request = typeof input === 'string' ? new w.Request(input) : input;
    if (new URL(request.url).pathname === '/api/human/status') {
      return Response.json({ verified });
    }
    calls.push({ path: new URL(request.url).pathname, method: request.method,
      body: await request.text(), contentType: request.headers.get('Content-Type') });
    if (hardLimit) return Response.json({ error: 'slow down' }, { status: 429 });
    if (!verified && challenge) return Response.json({ error: 'verify first' }, {
      status: 403, headers: { 'X-Maxcourse-Challenge': 'required' },
    });
    return Response.json({ ok: true });
  };
  w.eval(script);
  const solve = () => w.document.querySelector('cap-widget').dispatchEvent(new w.CustomEvent('solve', { detail: { token: 'client-event-only' } }));
  return { w, calls, solve, verified: value => { verified = value; }, challenge: value => { challenge = value; }, hardLimit: value => { hardLimit = value; }, close: () => dom.window.close() };
}

(async () => {
  let e = environment();
  try {
    e.challenge(false);
    assert.equal((await e.w.fetch('/api/courses')).status, 200);
    e.hardLimit(true);
    assert.equal((await e.w.fetch('/api/courses')).status, 429);
    assert.equal(e.calls.length, 2);
    assert.equal(e.w.document.querySelectorAll('dialog').length, 0);
  } finally { e.close(); }

  e = environment();
  try {
    const body = JSON.stringify({ title: 'keep my task', text: 'do not submit twice' });
    const one = e.w.fetch('/api/todos', { method: 'POST', body, headers: { 'Content-Type': 'application/json' } });
    const two = e.w.fetch('/api/semesters');
    await settle();
    assert.equal(e.w.document.querySelectorAll('dialog').length, 1);
    e.solve(); await settle();
    assert.equal(e.calls.length, 2, 'A client solve event without server clearance must not replay requests');
    e.verified(true); e.solve();
    assert.equal((await one).status, 200);
    assert.equal((await two).status, 200);
    assert.equal(e.calls.length, 4);
    assert.deepEqual(e.calls.filter(c => c.path === '/api/todos').map(c => c.body), [body, body]);
    assert.equal(e.w.document.querySelector('#editor').value, 'keep this text');
    assert.equal(e.w.document.querySelectorAll('dialog').length, 0);
  } finally { e.close(); }

  e = environment();
  try {
    const request = e.w.fetch('/api/todos', { method: 'POST', body: 'retained' });
    await settle(); e.w.document.querySelector('[data-cancel]').click();
    assert.equal((await request).status, 403);
    assert.equal(e.calls.length, 1);
    assert.equal(e.w.document.body.style.overflow, '');
  } finally { e.close(); }

  e = environment();
  try {
    const controller = new AbortController();
    const request = e.w.fetch('/api/parse-transcript', { method: 'POST', body: 'upload', signal: controller.signal });
    await settle(); controller.abort(); e.verified(true); e.solve();
    await assert.rejects(request, error => error.name === 'AbortError');
    assert.equal(e.calls.length, 1);
  } finally { e.close(); }

  e = environment();
  try {
    const frame = e.w.document.createElement('iframe');
    frame.src = 'https://maxcourse.test/api/media-dl/proxy?feedback=local-test-frame';
    frame.hidden = true; e.w.document.body.append(frame);
    const received = [];
    frame.contentWindow.postMessage = data => received.push(data);
    e.w.dispatchEvent(new e.w.MessageEvent('message', { origin: 'https://attacker.test', source: frame.contentWindow,
      data: { type: 'maxcourse-human-required' } }));
    await settle(); assert.equal(e.w.document.querySelectorAll('dialog').length, 0);
    e.w.dispatchEvent(new e.w.MessageEvent('message', { origin: 'https://maxcourse.test', source: frame.contentWindow,
      data: { type: 'maxcourse-human-required' } }));
    await settle(); assert.equal(e.w.document.querySelectorAll('dialog').length, 1);
    e.verified(true); e.solve(); await settle();
    assert.equal(received.length, 1);
    assert.equal(received[0].type, 'maxcourse-human-result');
    assert.equal(received[0].success, true);
  } finally { e.close(); }

  e = environment();
  try {
    const form = new FormData(); form.set('file', new Blob(['sample-pdf-bytes']), 'sample.pdf');
    const request = e.w.fetch('/api/parse-transcript', { method: 'POST', body: form });
    await settle(); e.verified(true); e.solve(); await request;
    assert.equal(e.calls[0].body, e.calls[1].body);
    assert.equal(e.calls[0].contentType, e.calls[1].contentType);
  } finally { e.close(); }
  console.log('Human verification UI passed: single dialog, server-confirmed clearance, one POST retry, uploads, cancel, abort, and hard limits.');
})().catch(error => { console.error(error); process.exitCode = 1; });
