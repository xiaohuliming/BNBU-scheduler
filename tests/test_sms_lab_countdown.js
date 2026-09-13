const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(remainingSeconds = [2]) {
  let now = Date.parse('2026-09-13T15:00:00Z');
  let writes = 0;
  let requests = 0;
  let nextTimer = 0;
  let buttons = [];
  const timers = new Map();
  const elements = new Map();
  const orders = remainingSeconds.map((seconds, index) => ({
    id: index + 1, status: 'active', can_cancel: true,
    createdAt: new Date(now - 120000 + seconds * 1000).toISOString(),
    service: { code: 'dr', name: 'OpenAI', logo_url: '' },
    country: { id: 6, name: 'Indonesia' }, sale_price: 0.3,
    phone: '620000000', otpList: [],
  }));
  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, {
      dataset: {}, textContent: '', classList: { add() {}, toggle() {} },
    });
    return elements.get(id);
  };
  const list = getElement('activation-list');
  Object.defineProperty(list, 'innerHTML', {
    set(html) {
      writes += 1;
      buttons = [...html.matchAll(/<button\b([^>]*data-action="cancel"[^>]*)>([^<]*)<\/button>/g)].map((match) => ({
        dataset: {
          action: 'cancel',
          cancelAt: /data-cancel-at="([^"]*)"/.exec(match[1])?.[1],
        },
        disabled: /\bdisabled\b/.test(match[1]),
        textContent: match[2],
      }));
    },
  });
  list.querySelectorAll = () => buttons;
  class TestDate extends Date { static now() { return now; } }
  const document = { hidden: false, getElementById: getElement, querySelectorAll: () => [] };
  const window = {
    setInterval(callback, delay) { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
    clearInterval(id) { timers.delete(id); },
  };
  const context = vm.createContext({
    window, document, Date: TestDate, console, URL,
    fetch: async () => { requests += 1; return { ok: true, json: async () => ({ orders }) }; },
  });
  const filename = path.join(__dirname, '../sms-lab/reseller.js');
  const source = fs.readFileSync(filename, 'utf8');
  vm.runInContext(source.slice(0, source.indexOf('const setAuthProvider =')), context, { filename });
  context.fixtureOrders = orders;
  vm.runInContext('state.account = { authenticated: true }; state.orders = fixtureOrders; state.ordersFingerprint = JSON.stringify(fixtureOrders); renderOrders(); startPolling();', context);
  return {
    context, document, buttons: () => buttons, timers,
    writes: () => writes, requests: () => requests,
    async tick(seconds = 1) {
      now += seconds * 1000;
      for (const timer of timers.values()) if (timer.delay === 1000) timer.callback();
    },
    reloadSameOrders: () => vm.runInContext('loadOrders(false)', context),
  };
}

test('unchanged orders count down each second without refetching or replacing the card', async () => {
  const ui = setup();
  const button = ui.buttons()[0];
  assert.equal(button.textContent, '2 秒后可取消');
  await ui.reloadSameOrders();
  const requests = ui.requests();
  const writes = ui.writes();
  await ui.tick();
  assert.equal(button.textContent, '1 秒后可取消');
  assert.equal(button.disabled, true);
  assert.equal(ui.buttons()[0], button);
  assert.equal(ui.writes(), writes);
  assert.equal(ui.requests(), requests);
  await ui.tick();
  assert.equal(button.textContent, '取消并退款');
  assert.equal(button.disabled, false);
});

test('a delayed tick catches up to the deadline instead of decrementing once', async () => {
  const ui = setup([5]);
  await ui.tick(60);
  assert.equal(ui.buttons()[0].textContent, '取消并退款');
  assert.equal(ui.buttons()[0].disabled, false);
});

test('each order has its own cancellation deadline', async () => {
  const ui = setup([1, 4]);
  await ui.tick();
  assert.equal(ui.buttons()[0].textContent, '取消并退款');
  assert.equal(ui.buttons()[1].textContent, '3 秒后可取消');
  assert.equal(ui.buttons()[1].disabled, true);
});

test('the countdown cannot re-enable a cancellation while its request is in flight', async () => {
  const ui = setup([0]);
  ui.context.fixtureButton = ui.buttons()[0];
  vm.runInContext('setButtonBusy(fixtureButton, true, "处理中...")', ui.context);
  await ui.tick(10);
  assert.equal(ui.buttons()[0].disabled, true);
  assert.equal(ui.buttons()[0].textContent, '处理中...');
});

test('restarting polling replaces the countdown timer and preserves the SMS polling interval', () => {
  const ui = setup();
  vm.runInContext('startPolling(); startPolling();', ui.context);
  assert.deepEqual([...ui.timers.values()].map((timer) => timer.delay).sort((a, b) => a - b), [1000, 5000]);
});
