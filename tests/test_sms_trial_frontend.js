const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(price = 0.3, available = true, balance = 0) {
  const elements = new Map();
  const requests = [];
  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, {
      dataset: {}, textContent: '', innerHTML: '', disabled: false,
      classList: { add() {}, remove() {}, toggle() {} }, focus() {},
    });
    return elements.get(id);
  };
  const account = { authenticated: true, user: { username: 'trial', display_name: 'trial' },
    wallet: { balance }, free_trial: { available, status: available ? 'available' : 'used', max_price: 0.5 } };
  const nextTrial = { available: false, status: 'reserved', max_price: 0.5 };
  const context = vm.createContext({
    console, URL, Date, fixture: { account, price },
    document: { getElementById: getElement, querySelectorAll: () => [] },
    window: { clearTimeout() {}, setTimeout() {} },
    crypto: { randomUUID: () => 'test_trial_purchase_001' },
    fetch: async (url, options) => {
      requests.push({ url, options });
      const payload = options?.method === 'POST'
        ? { wallet_balance: balance, free_trial: nextTrial, order: { id: 1 } }
        : { orders: [], free_trial: nextTrial };
      return { ok: true, json: async () => payload };
    },
  });
  const file = path.join(__dirname, '../sms-lab/reseller.js');
  const source = fs.readFileSync(file, 'utf8');
  vm.runInContext(source.slice(0, source.indexOf('const copyText =')), context, { filename: file });
  vm.runInContext("state.account = fixture.account; state.selectedService = { code: 'dr', name: 'OpenAI' }; state.selectedCountry = { id: 6, name: 'Indonesia', price: fixture.price }; renderAccount();", context);
  return { getElement, requests, context, run: (code) => vm.runInContext(code, context) };
}

test('an eligible zero-balance customer can confirm a free order with explicit trial intent', async () => {
  const ui = setup();
  assert.equal(ui.getElement('purchase-button').disabled, false);
  assert.match(ui.getElement('purchase-button').textContent, /免费/);
  ui.run('openPurchaseConfirm()');
  assert.match(ui.getElement('confirm-price').textContent, /0\.0000.*免费/);
  await ui.run('submitPurchase()');
  const post = ui.requests.find((r) => r.options?.method === 'POST');
  assert.equal(JSON.parse(post.options.body).use_free_trial, true);
  assert.equal(ui.run('state.account.wallet.balance'), 0);
  assert.equal(ui.run('state.account.free_trial.available'), false);
});

test('the free cap includes 0.50 but does not subsidize a higher-price order', () => {
  assert.equal(setup(0.5).getElement('purchase-button').disabled, false);
  const expensive = setup(0.5001);
  assert.equal(expensive.getElement('purchase-button').disabled, true);
});

test('a consumed trial still requires wallet funds and paid confirmation', async () => {
  assert.equal(setup(0.3, false).getElement('purchase-button').disabled, true);
  const ui = setup(0.3, false, 1);
  ui.run('openPurchaseConfirm()');
  assert.equal(ui.getElement('confirm-price').textContent, '0.3000 USD');
  await ui.run('submitPurchase()');
  const post = ui.requests.find((r) => r.options?.method === 'POST');
  assert.equal(JSON.parse(post.options.body).use_free_trial, false);
});
