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
  vm.runInNewContext(`${source}\nmodule.exports = { createRechargeController };`, sandbox, { filename });
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
  let nextId = 1;
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
    setTimer(callback) { timers.push(callback); return timers.length; },
    clearTimer() {},
    onCheckout: (url) => checkouts.push(url),
    onWallet: (balance) => wallets.push(Number(balance).toFixed(4)),
    onSuccess: (order) => successes.push(order.id),
    onView: (view) => views.push(view),
  });
  controller.setAccount('alice');
  controller.open();
  return {
    controller, requests, pending, storageData, checkouts, successes, wallets, timers, views,
    create: (amount) => controller.create(amount),
    poll: (id) => controller.poll(id),
    resolveCreate: (payload) => pending.shift().resolve(payload),
    rejectCreate: (error) => pending.shift().reject(error),
    resolvePoll: (payload) => pending.shift().resolve(payload),
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
  assert.equal(ui.timers.length, 0);
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
  assert.equal(ui.timers.length, 0);
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
