const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadRechargeModule() {
  const filename = path.join(__dirname, '..', 'sms-lab', 'reseller.js');
  const source = fs.readFileSync(filename, 'utf8').split('\nconst state =')[0];
  const sandbox = {
    module: { exports: {} }, exports: {}, URL, URLSearchParams,
    console, setTimeout, clearTimeout,
  };
  vm.runInNewContext(`${source}\nmodule.exports = { createRechargeController, createModalFocusManager: typeof createModalFocusManager === 'undefined' ? undefined : createModalFocusManager };`, sandbox, { filename });
  return sandbox.module.exports;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function paidOrder(id, status = 'pending', requestId = 'recharge_test_001') {
  return {
    id, app: 'sms_market', amount_fen: 3400, wallet_units: 50000,
    wallet_amount: '5.0000', status, channel: 'xorpay',
    created_at: '2026-09-13T12:00:00Z',
    finished_at: status === 'pending' ? null : '2026-09-13T12:01:00Z',
    request_id: requestId,
  };
}

const checkout = 'https://chat.bnbscheduler.top/api/recharge/sms-market/orders/S-one/checkout';

function setupRechargeHarness() {
  const { createRechargeController } = loadRechargeModule();
  const requests = [];
  const pending = [];
  const storageData = new Map();
  const checkouts = [];
  const successes = [];
  const wallets = [];
  const timers = [];
  const views = [];
  const opens = [];
  const closes = [];
  let nextId = 1;
  let nextTimerId = 1;
  const controller = createRechargeController({
    request(pathname, options = {}) {
      const item = { pathname, method: options.method || 'GET', body: options.body };
      const wait = deferred();
      requests.push(item);
      pending.push(wait);
      return wait.promise;
    },
    storage: {
      getItem(key) { return storageData.has(key) ? storageData.get(key) : null; },
      setItem(key, value) { storageData.set(key, value); },
      removeItem(key) { storageData.delete(key); },
    },
    makeRequestId: () => `recharge_test_00${nextId++}`,
    setTimer(callback, delay) {
      const timer = { id: nextTimerId++, callback, delay, cancelled: false, ran: false };
      timers.push(timer);
      return timer.id;
    },
    clearTimer(id) {
      const timer = timers.find((item) => item.id === id);
      if (timer) timer.cancelled = true;
    },
    onCheckout: (url) => checkouts.push(url),
    onWallet: (balance) => wallets.push(Number(balance).toFixed(4)),
    onSuccess: (order) => successes.push(order.id),
    onView: (view) => views.push(view),
    onOpen: (trigger) => opens.push(trigger),
    onClose: () => closes.push(true),
  });
  controller.setAccount('alice');
  controller.open();
  return {
    controller, requests, pending, storageData, checkouts, successes, wallets, timers, views, opens, closes,
    create: (amount) => controller.create(amount),
    poll: (id) => controller.poll(id),
    resolveCreate: (payload) => pending.shift().resolve(payload),
    rejectCreate: (error) => pending.shift().reject(error),
    rejectPoll: (error) => pending.shift().reject(error),
    resolvePoll: (payload) => pending.shift().resolve(payload),
    runNextTimer() {
      const timer = timers.find((item) => !item.cancelled && !item.ran);
      if (!timer) return false;
      timer.ran = true;
      timer.callback();
      return true;
    },
    activeTimers: () => timers.filter((item) => !item.cancelled && !item.ran),
    flush: () => new Promise((resolve) => setImmediate(resolve)),
    walletText: () => wallets.at(-1),
    successCount: () => successes.length,
    view: () => views.at(-1),
  };
}

test('double submit creates only one recharge order', async () => {
  const ui = setupRechargeHarness();
  const first = ui.create(5);
  const second = ui.create(5);
  assert.equal(ui.requests.filter((request) => request.method === 'POST').length, 1);
  ui.resolveCreate({ order: paidOrder('S-one'), checkout_url: checkout, reused: false });
  await Promise.all([first, second]);
  assert.deepEqual(ui.checkouts, [checkout]);
});

test('a paid order refreshes the SMS wallet once', async () => {
  const ui = setupRechargeHarness();
  const polling = ui.poll('S-paid');
  ui.resolvePoll({ order: paidOrder('S-paid', 'credited'), wallet_balance: 5 });
  await polling;
  await ui.flush();
  assert.equal(ui.walletText(), '5.0000');
  assert.equal(ui.successCount(), 1);
  assert.equal(ui.activeTimers().length, 0);
});

test('closing the modal ignores an in-flight create response', async () => {
  const ui = setupRechargeHarness();
  const creating = ui.create(5);
  ui.controller.close();
  ui.resolveCreate({ order: paidOrder('S-one'), checkout_url: checkout, reused: false });
  await creating;
  assert.deepEqual(ui.checkouts, []);
  assert.equal(ui.view().visible, false);
});

test('switching accounts ignores the previous account response', async () => {
  const ui = setupRechargeHarness();
  const polling = ui.poll('S-paid');
  ui.controller.setAccount('bob');
  ui.resolvePoll({ order: paidOrder('S-paid', 'credited'), wallet_balance: 5 });
  await polling;
  assert.equal(ui.walletText(), undefined);
  assert.equal(ui.successCount(), 0);
});

test('a failed recharge is terminal and reports failure without polling again', async () => {
  const ui = setupRechargeHarness();
  const polling = ui.poll('S-failed');
  ui.resolvePoll({ order: paidOrder('S-failed', 'failed'), wallet_balance: 0 });
  await polling;
  assert.equal(ui.view().status, 'failed');
  assert.equal(ui.activeTimers().length, 0);
  assert.equal(ui.successCount(), 0);
});

test('an uncertain create reuses the persisted request id', async () => {
  const ui = setupRechargeHarness();
  const first = ui.create(5);
  const firstRequestId = JSON.parse(ui.requests[0].body).request_id;
  ui.rejectCreate(new Error('network uncertain'));
  await assert.rejects(first, /network uncertain/);
  const second = ui.create(5);
  const secondRequestId = JSON.parse(ui.requests[1].body).request_id;
  assert.equal(secondRequestId, firstRequestId);
  ui.resolveCreate({ order: paidOrder('S-one', 'pending', firstRequestId), checkout_url: checkout, reused: true });
  await second;
});

test('URL return recovery removes only recharge_order and loads its detail', async () => {
  const ui = setupRechargeHarness();
  ui.controller.close();
  const replaced = [];
  const recovering = ui.controller.recoverFromUrl(
    'https://www.bnbscheduler.top/sms-lab/?campaign=fall&recharge_order=S-paid#wallet',
    { replaceState(_state, _title, url) { replaced.push(url); } },
  );
  assert.equal(ui.view().visible, true);
  assert.equal(ui.requests[0].pathname, '/recharge/orders/S-paid');
  assert.deepEqual(replaced, ['/sms-lab/?campaign=fall#wallet']);
  ui.resolvePoll({ order: paidOrder('S-paid', 'credited'), wallet_balance: 5 });
  await recovering;
  assert.equal(ui.successCount(), 1);
  assert.equal(ui.opens.length, 2);
});

test('URL return recovery keeps the modal usable when detail is temporarily unavailable', async () => {
  const ui = setupRechargeHarness();
  ui.controller.close();
  const recovering = ui.controller.recoverFromUrl(
    'https://www.bnbscheduler.top/sms-lab/?recharge_order=S-pending',
    { replaceState() {} },
  );
  ui.rejectCreate(new Error('temporary upstream failure'));
  await assert.doesNotReject(recovering);
  assert.equal(ui.view().visible, true);
  assert.equal(ui.view().status, 'error');
});

test('checkout navigation rejects lookalike, non-HTTPS, and credentialed URLs', async () => {
  for (const url of [
    'https://chat.bnbscheduler.top.evil.test/checkout',
    'http://chat.bnbscheduler.top/checkout',
    'https://user:secret@chat.bnbscheduler.top/checkout',
  ]) {
    const ui = setupRechargeHarness();
    const creating = ui.create(5);
    ui.resolveCreate({ order: paidOrder('S-one'), checkout_url: url, reused: false });
    await creating;
    assert.deepEqual(ui.checkouts, []);
    assert.equal(ui.view().status, 'invalid_checkout');
  }
});

test('a recoverable detail failure retries with bounded backoff and then succeeds', async () => {
  const ui = setupRechargeHarness();
  const first = ui.poll('S-retry');
  const error = new Error('temporary upstream failure');
  error.status = 503;
  ui.rejectPoll(error);
  await assert.rejects(first, /temporary upstream failure/);
  assert.deepEqual(ui.activeTimers().map((timer) => timer.delay), [1000]);
  assert.equal(ui.runNextTimer(), true);
  assert.equal(ui.requests.at(-1).pathname, '/recharge/orders/S-retry');
  ui.resolvePoll({ order: paidOrder('S-retry', 'credited'), wallet_balance: 5 });
  await ui.flush();
  assert.equal(ui.successCount(), 1);
  assert.equal(ui.activeTimers().length, 0);
});

test('detail retries stop after three scheduled attempts', async () => {
  const ui = setupRechargeHarness();
  let polling = ui.poll('S-bounded');
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const error = new Error(`failure ${attempt + 1}`);
    error.status = 500;
    ui.rejectPoll(error);
    if (attempt === 0) await assert.rejects(polling, /failure 1/);
    await ui.flush();
    if (attempt < 3) {
      assert.equal(ui.runNextTimer(), true);
      await ui.flush();
    }
  }
  await ui.flush();
  assert.equal(ui.requests.filter((request) => request.pathname === '/recharge/orders/S-bounded').length, 4);
  assert.equal(ui.activeTimers().length, 0);
});

test('closing cancels retry and reopening resumes the retained order', async () => {
  const ui = setupRechargeHarness();
  const polling = ui.poll('S-resume');
  const error = new Error('temporary');
  error.status = 503;
  ui.rejectPoll(error);
  await assert.rejects(polling, /temporary/);
  assert.equal(ui.activeTimers().length, 1);
  ui.controller.close();
  assert.equal(ui.activeTimers().length, 0);
  assert.equal(ui.runNextTimer(), false);
  const before = ui.requests.length;
  ui.controller.open('reopen-button');
  assert.equal(ui.requests.length, before + 1);
  assert.equal(ui.requests.at(-1).pathname, '/recharge/orders/S-resume');
  ui.resolvePoll({ order: paidOrder('S-resume', 'credited'), wallet_balance: 5 });
  await ui.flush();
});

test('account switching cancels retry and forgets the previous order', async () => {
  const ui = setupRechargeHarness();
  const polling = ui.poll('S-alice');
  const error = new Error('temporary');
  error.status = 503;
  ui.rejectPoll(error);
  await assert.rejects(polling, /temporary/);
  assert.equal(ui.activeTimers().length, 1);
  ui.controller.setAccount('bob');
  assert.equal(ui.activeTimers().length, 0);
  const before = ui.requests.length;
  ui.controller.open('bob-button');
  assert.equal(ui.requests.length, before);
});

test('return parameter remains until login and is then recovered automatically', async () => {
  const ui = setupRechargeHarness();
  ui.controller.setAccount(null);
  const replaced = [];
  const url = 'https://www.bnbscheduler.top/sms-lab/?campaign=fall&recharge_order=S-login#wallet';
  await ui.controller.recoverFromUrl(url, {
    replaceState(_state, _title, next) { replaced.push(next); },
  });
  assert.deepEqual(replaced, []);
  assert.equal(ui.requests.length, 0);
  ui.controller.setAccount('alice');
  assert.deepEqual(replaced, ['/sms-lab/?campaign=fall#wallet']);
  assert.equal(ui.requests[0].pathname, '/recharge/orders/S-login');
  ui.resolvePoll({ order: paidOrder('S-login', 'credited'), wallet_balance: 5 });
  await ui.flush();
});

test('modal focus manager traps Tab, blocks background, and restores its trigger', () => {
  const { createModalFocusManager } = loadRechargeModule();
  const documentState = { activeElement: null };
  const focusable = ['close', 'one', 'five', 'ten', 'confirm'].map((name) => ({
    name,
    disabled: false,
    focus() { documentState.activeElement = this; },
  }));
  const modal = { querySelectorAll: () => focusable, contains: (node) => focusable.includes(node) };
  const backgrounds = [{ inert: false }, { inert: false }, { inert: false }];
  const trigger = { focus() { documentState.activeElement = this; } };
  const focus = createModalFocusManager({ modal, backgrounds, document: documentState });

  focus.open(trigger);
  assert.equal(documentState.activeElement, focusable[0]);
  assert.equal(backgrounds.every((item) => item.inert), true);

  documentState.activeElement = focusable.at(-1);
  let prevented = false;
  focus.handleKeydown({ key: 'Tab', shiftKey: false, preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(documentState.activeElement, focusable[0]);

  documentState.activeElement = focusable[0];
  focus.handleKeydown({ key: 'Tab', shiftKey: true, preventDefault() {} });
  assert.equal(documentState.activeElement, focusable.at(-1));

  focus.close();
  assert.equal(backgrounds.every((item) => !item.inert), true);
  assert.equal(documentState.activeElement, trigger);
});

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...items) { items.forEach((item) => this.values.add(item)); }
  remove(...items) { items.forEach((item) => this.values.delete(item)); }
  toggle(item, force) {
    const enabled = force === undefined ? !this.values.has(item) : force;
    if (enabled) this.values.add(item); else this.values.delete(item);
    return enabled;
  }
}

test('the real page initializes and URL recovery uses the modal focus lifecycle', async () => {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'sms-lab', 'index.html'), 'utf8');
  const source = fs.readFileSync(path.join(root, 'sms-lab', 'reseller.js'), 'utf8');
  const ids = [...html.matchAll(/id="([^"]+)"/g)].map((match) => match[1]);
  const listeners = new Map();
  const documentState = { activeElement: null, hidden: false, referrer: '' };
  const elements = new Map(ids.map((id) => {
    const element = {
      id, dataset: {}, classList: new FakeClassList(), className: '', textContent: '', innerHTML: '',
      value: '', disabled: false, focus() { documentState.activeElement = this; },
      addEventListener(type, callback) { listeners.set(`${id}:${type}`, callback); },
      setAttribute() {}, removeAttribute() {}, closest() { return null; }, contains() { return false; },
      querySelectorAll() { return []; },
    };
    return [id, element];
  }));
  const packageButtons = [1, 5, 10].map((value) => ({
    dataset: { rechargePackage: String(value) }, classList: new FakeClassList(), disabled: false,
    setAttribute() {}, focus() { documentState.activeElement = this; },
  }));
  const filterButtons = ['active', 'all'].map((value) => ({
    dataset: { orderFilter: value }, classList: new FakeClassList(), addEventListener() {}, setAttribute() {},
  }));
  elements.get('recharge-modal').querySelectorAll = () => [elements.get('recharge-close'), ...packageButtons, elements.get('recharge-confirm')];
  elements.get('recharge-modal').contains = (node) => elements.get('recharge-modal').querySelectorAll().includes(node);
  const intervals = [];
  const backgrounds = [{ inert: false }, { inert: false }, { inert: false }];
  const document = {
    ...documentState,
    get activeElement() { return documentState.activeElement; },
    set activeElement(value) { documentState.activeElement = value; },
    getElementById: (id) => elements.get(id) || null,
    querySelectorAll(selector) {
      if (selector === '[data-recharge-package]') return packageButtons;
      if (selector === '[data-order-filter]') return filterButtons;
      return [];
    },
    querySelector(selector) {
      if (selector === '.app-header') return backgrounds[0];
      if (selector === 'main') return backgrounds[1];
      if (selector === '.footer') return backgrounds[2];
      return null;
    },
    addEventListener(type, callback) { listeners.set(`document:${type}`, callback); },
  };
  const payloads = {
    '/api/sms-lab/status': { configured: true },
    '/api/sms-lab/account': {
      authenticated: true, user: { id: 1, username: 'alice', display_name: 'Alice' },
      wallet: { balance: 0, currency: 'USD' }, ledger: [],
    },
    '/api/sms-lab/services': { services: [] },
    '/api/sms-lab/orders': { orders: [] },
    '/api/sms-lab/recharge/orders/S-return': {
      order: paidOrder('S-return', 'credited'), wallet_balance: 5,
    },
  };
  const fetch = async (url) => ({ ok: true, status: 200, json: async () => payloads[url] || {} });
  const localStorage = new Map();
  const replaced = [];
  const window = {
    location: { protocol: 'file:', href: 'file:///sms-lab/?campaign=fall&recharge_order=S-return#wallet', assign() {} },
    history: { replaceState(_state, _title, url) { replaced.push(url); } }, localStorage: {
      getItem: (key) => localStorage.get(key) || null,
      setItem: (key, value) => localStorage.set(key, value),
      removeItem: (key) => localStorage.delete(key),
    },
    setTimeout() { return 1; }, clearTimeout() {}, setInterval(callback, delay) { intervals.push({ callback, delay }); return intervals.length; },
    clearInterval() {},
  };
  const sandbox = {
    window, document, fetch, location: window.location, navigator: { clipboard: { writeText: async () => {} } },
    crypto: { randomUUID: () => 'test-uuid' }, SMSServiceSearch: { matches: () => true },
    URL, URLSearchParams, console, setTimeout, clearTimeout,
  };
  vm.runInNewContext(source, sandbox, { filename: 'sms-lab/reseller.js' });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(intervals.length, 1);
  assert.equal(elements.get('config-message').textContent, '');
  assert.deepEqual(replaced, ['/sms-lab/?campaign=fall#wallet']);
  assert.equal(documentState.activeElement, elements.get('recharge-close'));
  assert.equal(backgrounds.every((item) => item.inert), true);
  listeners.get('document:keydown')({ key: 'Escape' });
  assert.equal(backgrounds.every((item) => !item.inert), true);
  assert.equal(documentState.activeElement, elements.get('recharge-button'));
});
