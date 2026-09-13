# SMS Market Search and Recharge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add alias-aware SMS service search and a recoverable online recharge flow that pays through OmniChat XorPay and credits only the MAXCOURSE SMS USD wallet.

**Architecture:** OmniChat remains the payment authority and stores `app = 'sms_market'` orders in the shared auth database. MAXCOURSE proxies authenticated requests with the existing parent-domain SSO token and settles paid orders into its local SMS wallet with a unique ledger reference. Search aliases are returned with the HeroSMS catalog and normalized in a small browser-side search module.

**Tech Stack:** Python 3.12, FastAPI, Flask, SQLite, requests/httpx, vanilla JavaScript, Node test runner

**Spec:** `docs/superpowers/specs/2026-09-13-sms-market-search-recharge-design.md`

## Global Constraints

- Fixed exchange rate: `1 USD = 6.80 CNY`.
- Fixed packages: `1 USD = 680 fen`, `5 USD = 3400 fen`, `10 USD = 6800 fen`.
- SMS recharge never changes OmniChat credits.
- XorPay secrets remain only in OmniChat private configuration.
- Existing OmniChat recharge endpoints and existing SMS Market purchases remain backward compatible.
- Every money mutation is integer-based and idempotent.
- Existing user-owned dirty files in the primary MAXCOURSE checkout must remain untouched.
- Deploy OmniChat before MAXCOURSE.

---

### Task 1: OmniChat application-scoped recharge storage

**Files:**
- Modify: `/tmp/omnichat-sms-recharge.UDkjhf/app/db.py`
- Create: `/tmp/omnichat-sms-recharge.UDkjhf/tests/test_sms_market_recharge.py`

**Interfaces:**
- Produces: `recharge_by_request(user_id: int, request_id: str, app: str = "omnichat")`
- Produces: `recharge_create_once(..., app: str = "omnichat")`
- Produces: `recharge_pending_count(user_id: int, app: str | None = None)`
- Produces: `recharge_list_user(user_id: int, limit: int = 10, app: str = "omnichat")`
- Produces: `recharge_finish(...)` that credits shared balance only when the persisted order app is `omnichat`

- [ ] **Step 1: Write failing storage tests**

```python
def test_request_id_and_pending_cap_are_scoped_by_app(self):
    omni, _ = db.recharge_create_once(
        "R-omni", self.uid, 680, 680, 0, "xorpay",
        request_id="same-request", pending_cap=1, app="omnichat")
    sms, created = db.recharge_create_once(
        "R-sms", self.uid, 680, 10_000, 0, "xorpay",
        request_id="same-request", pending_cap=1, app="sms_market")
    self.assertTrue(created)
    self.assertEqual((omni["app"], sms["app"]), ("omnichat", "sms_market"))

def test_finishing_sms_order_does_not_credit_omnichat_balance(self):
    db.recharge_create_once(
        "R-sms", self.uid, 680, 10_000, 0, "xorpay",
        request_id="sms-request-001", pending_cap=5, app="sms_market")
    row = db.recharge_finish(
        "R-sms", "credited", external_id="trade-sms-1",
        expected_channel="xorpay", expected_app="sms_market",
        expected_amount_fen=680, unique_external=True)
    self.assertEqual(row["status"], "credited")
    self.assertEqual(db.get_credits(self.uid), 0)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `/Users/xhlm/Desktop/Study/OmniChat/.venv/bin/python -m unittest tests.test_sms_market_recharge -v`

Expected: failures because recharge helpers are hard-coded to `omnichat` and finishing every credited order changes shared credits.

- [ ] **Step 3: Implement application-scoped storage**

Use `app` as a bound SQL parameter in request lookup, pending counts, inserts and lists. Preserve `app="omnichat"` defaults so all existing callers remain compatible. In `recharge_finish`, branch on the row's persisted app:

```python
if status == "credited" and row["app"] == "omnichat":
    _apply_credits(db, row["user_id"], row["credits"], "recharge",
                   note=f"在线充值 {order_id}")
    if row["bonus"]:
        _apply_credits(db, row["user_id"], row["bonus"], "grant",
                       note=f"充值赠送 {order_id}")
```

Reject unsupported app names before composing SQL. The allowed values are `omnichat` and `sms_market`.

- [ ] **Step 4: Run focused and existing recharge tests**

Run:

```bash
/Users/xhlm/Desktop/Study/OmniChat/.venv/bin/python -m unittest tests.test_sms_market_recharge tests.test_recharge tests.test_xorpay -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_sms_market_recharge.py
git commit -m "feat: scope recharge orders by application"
```

---

### Task 2: OmniChat SMS Market payment API

**Files:**
- Modify: `/tmp/omnichat-sms-recharge.UDkjhf/app/payments.py`
- Modify: `/tmp/omnichat-sms-recharge.UDkjhf/app/main.py`
- Modify: `/tmp/omnichat-sms-recharge.UDkjhf/payment.example.json`
- Modify: `/tmp/omnichat-sms-recharge.UDkjhf/tests/test_sms_market_recharge.py`
- Modify: `/tmp/omnichat-sms-recharge.UDkjhf/tests/test_xorpay.py`

**Interfaces:**
- Consumes: application-scoped storage from Task 1
- Produces: `payments.sms_market_packages()` returning literal package dictionaries with `usd_units`, `usd`, and `fen`
- Produces: `payments.xorpay_create(order_id, amount_fen, *, title=None, return_url=None)`
- Produces: authenticated `/api/recharge/sms-market/*` routes
- Produces: SMS order payloads containing `id`, `app`, `amount_fen`, `wallet_units`, `wallet_amount`, `status`, `channel`, `created_at`, `finished_at`, and `request_id`

- [ ] **Step 1: Write failing package and route tests**

```python
def test_sms_config_exposes_fixed_packages_without_secret(self):
    response = self.client.get("/api/recharge/sms-market/config", headers=self.headers)
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["usd_cny"], "6.80")
    self.assertEqual(
        [(p["usd"], p["fen"]) for p in response.json()["packages"]],
        [("1.00", 680), ("5.00", 3400), ("10.00", 6800)])
    self.assertNotIn("appsecret", response.text)

def test_sms_order_uses_locked_amount_and_no_omnichat_bonus(self):
    response = self.client.post(
        "/api/recharge/sms-market/orders", headers=self.headers,
        json={"package_usd": 5, "request_id": "sms-order-request-001"})
    self.assertEqual(response.status_code, 200)
    order = response.json()["order"]
    self.assertEqual((order["amount_fen"], order["wallet_units"]), (3400, 50_000))
    self.assertEqual(order["app"], "sms_market")
```

Add ownership, invalid-package, idempotency, pending-cap, checkout-return and callback tests. The callback test must assert the shared credit balance remains unchanged.

- [ ] **Step 2: Run tests and verify RED**

Run: `/Users/xhlm/Desktop/Study/OmniChat/.venv/bin/python -m unittest tests.test_sms_market_recharge -v`

Expected: 404 responses because SMS Market routes do not exist.

- [ ] **Step 3: Implement fixed package helpers and checkout override**

Read the following private config shape, with safe defaults for local tests:

```json
{
  "sms_market": {
    "usd_cny": "6.80",
    "packages_usd": [1, 5, 10],
    "return_url": "https://www.bnbscheduler.top/sms-lab/",
    "title": "SMS Market 钱包充值"
  }
}
```

Convert `Decimal("6.80") * package_usd * 100` to integer fen and `package_usd * 10000` to wallet units. Validate the return URL as a fixed HTTPS URL with host `www.bnbscheduler.top` and path `/sms-lab/`. Extend `xorpay_create` only through keyword arguments; existing calls must produce byte-for-byte equivalent query semantics.

- [ ] **Step 4: Implement authenticated SMS Market routes**

Use a strict Pydantic request model:

```python
class SMSMarketRechargeCreate(BaseModel):
    package_usd: Literal[1, 5, 10]
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
```

Create `app="sms_market"` orders with `bonus=0`. Return a same-origin OmniChat checkout route rather than returning a caller-supplied destination. List and detail routes must query only the current shared user and `sms_market` app. The XorPay callback must accept both supported apps, enforce exact amount and unique transaction ID, and call `recharge_finish` with the persisted app.

- [ ] **Step 5: Run focused tests**

Run:

```bash
/Users/xhlm/Desktop/Study/OmniChat/.venv/bin/python -m unittest tests.test_sms_market_recharge tests.test_xorpay tests.test_recharge -v
```

Expected: all tests pass and existing OmniChat recharge behavior is unchanged.

- [ ] **Step 6: Commit**

```bash
git add app/payments.py app/main.py payment.example.json tests/test_sms_market_recharge.py tests/test_xorpay.py
git commit -m "feat: add SMS Market recharge checkout"
```

---

### Task 3: MAXCOURSE alias-aware service search

**Files:**
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/sms_lab/routes.py`
- Create: `/tmp/maxcourse-sms-recharge.DbR0SP/sms-lab/search.js`
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/sms-lab/reseller.js`
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/sms-lab/index.html`
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/tests/test_sms_lab.py`
- Create: `/tmp/maxcourse-sms-recharge.DbR0SP/tests/test_sms_lab_search.js`

**Interfaces:**
- Produces: service catalog items with `aliases: string[]`
- Produces: `SMSServiceSearch.normalize(value: string): string`
- Produces: `SMSServiceSearch.matches(service: object, query: string): boolean`

- [ ] **Step 1: Write failing backend catalog test**

```python
def test_openai_service_includes_search_aliases(self):
    services = self.client.get('/api/sms-lab/services').get_json()['services']
    openai = next(item for item in services if item['code'] == 'dr')
    self.assertTrue({'chatgpt', 'gpt', 'open ai'}.issubset(set(openai['aliases'])))
```

- [ ] **Step 2: Write failing browser search tests**

```javascript
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
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
/Users/xhlm/Desktop/Workplace/maxcourse/venv/bin/python -m pytest tests/test_sms_lab.py::SMSLabRouteTest::test_openai_service_includes_search_aliases -q
node --test tests/test_sms_lab_search.js
```

Expected: the Python assertion fails because aliases are missing, and Node fails because `search.js` does not exist.

- [ ] **Step 4: Implement aliases and normalized matcher**

Add a curated map keyed by HeroSMS service code. The `dr` aliases must include the literals above. Add unambiguous aliases for Telegram, WhatsApp, Google, Facebook, Instagram, Discord and WeChat. Do not add single-letter aliases. `search.js` must expose the two functions through both `module.exports` and `window.SMSServiceSearch` without dependencies.

Load `/sms-lab/search.js` before `reseller.js` and replace the inline `code/name.includes` logic with `SMSServiceSearch.matches`.

- [ ] **Step 5: Run focused tests**

Run:

```bash
/Users/xhlm/Desktop/Workplace/maxcourse/venv/bin/python -m pytest tests/test_sms_lab.py -q
node --test tests/test_sms_lab_search.js
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add sms_lab/routes.py sms-lab/search.js sms-lab/reseller.js sms-lab/index.html tests/test_sms_lab.py tests/test_sms_lab_search.js
git commit -m "feat: add aliases to SMS service search"
```

---

### Task 4: MAXCOURSE recharge proxy and exact-once wallet settlement

**Files:**
- Create: `/tmp/maxcourse-sms-recharge.DbR0SP/sms_lab/recharge.py`
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/sms_lab/storage.py`
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/sms_lab/routes.py`
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/tests/test_sms_lab.py`

**Interfaces:**
- Produces: `OmniRechargeClient(base_url, token)` with `config`, `create_order`, `list_orders`, and `get_order`
- Produces: `settle_paid_sms_recharge(conn, user_id: int, order: dict) -> tuple[bool, int]`
- Produces: Flask `/api/sms-lab/recharge/*` routes

- [ ] **Step 1: Write failing client and settlement tests**

Add tests that exercise the real local SQLite transaction and mock only the remote HTTP boundary:

```python
def test_paid_recharge_settles_wallet_exactly_once(self):
    order = {"id": "S-paid-1", "app": "sms_market", "status": "credited",
             "wallet_units": 50_000, "wallet_amount": "5.0000"}
    first = self.settle(order)
    second = self.settle(order)
    self.assertEqual(first, (True, 50_000))
    self.assertEqual(second, (False, 50_000))
    self.assertEqual(self.wallet_balance(), 50_000)
```

Add route tests for missing SSO token, valid package forwarding, upstream timeout, current-user order ownership, paid-order settlement and replay. The fake OmniChat response must include every documented order field.

- [ ] **Step 2: Run tests and verify RED**

Run: `/Users/xhlm/Desktop/Workplace/maxcourse/venv/bin/python -m pytest tests/test_sms_lab.py -q`

Expected: import or route failures because the recharge client and routes do not exist.

- [ ] **Step 3: Implement the focused OmniChat client**

Use `requests.Session` with a 10-second timeout, `Authorization: Bearer <sso_token>`, JSON content negotiation and base URL from `OMNICHAT_RECHARGE_API_BASE`, defaulting to `https://chat.bnbscheduler.top`. Reject a base URL that is not HTTPS outside tests. Map upstream 401 to a local `shared_login_required` error, 409 and 429 to their original safe messages, and network or malformed-response failures to `recharge_service_unavailable`. Never log or include the token in an error.

- [ ] **Step 4: Implement exact-once local settlement**

Validate `app == "sms_market"`, `status == "credited"`, a strict order ID and positive integer wallet units not exceeding 100,000. Under `BEGIN IMMEDIATE`, check the unique reference `online_recharge:{order_id}`, update the current local user's wallet, and insert a `kind="online_recharge"` ledger row. If the reference exists for another user, reject with a conflict and do not mutate either balance.

- [ ] **Step 5: Implement authenticated proxy routes**

Read the HttpOnly `sso_token` only from `request.cookies`. Never accept a token, username or user ID in JSON. For create, pass only `package_usd` and `request_id` upstream. For list and detail, settle each returned paid order before returning the latest local wallet balance. Return OmniChat's fixed checkout path and never accept a return URL from the browser.

- [ ] **Step 6: Run focused tests**

Run: `/Users/xhlm/Desktop/Workplace/maxcourse/venv/bin/python -m pytest tests/test_sms_lab.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add sms_lab/recharge.py sms_lab/storage.py sms_lab/routes.py tests/test_sms_lab.py
git commit -m "feat: settle OmniChat payments into SMS wallet"
```

---

### Task 5: SMS Market recharge interface and recovery flow

**Files:**
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/sms-lab/index.html`
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/sms-lab/reseller.js`
- Create: `/tmp/maxcourse-sms-recharge.DbR0SP/tests/test_sms_lab_recharge_frontend.js`
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/tests/test_sms_lab.py`

**Interfaces:**
- Consumes: MAXCOURSE recharge routes from Task 4
- Produces: recharge modal, fixed package selection, checkout navigation, status polling and return recovery

- [ ] **Step 1: Write failing frontend state tests**

Create a Node test harness around the recharge functions with deferred fetch responses. Cover these observable behaviors:

```javascript
test('double submit creates only one recharge order', async () => {
  const ui = setupRechargeHarness();
  const first = ui.create(5);
  const second = ui.create(5);
  assert.equal(ui.requests.filter(r => r.method === 'POST').length, 1);
  ui.resolveCreate({ order: paidOrder('S-one', 'pending'), checkout_url: checkout });
  await Promise.all([first, second]);
});

test('a paid order refreshes the SMS wallet once', async () => {
  const ui = setupRechargeHarness();
  ui.poll('S-paid');
  ui.resolvePoll({ order: paidOrder('S-paid', 'credited'), wallet_balance: 5 });
  await ui.flush();
  assert.equal(ui.walletText(), '5.0000');
  assert.equal(ui.successCount(), 1);
});
```

Also cover closing the modal during a request, account switching, failed upstream status, request-id reuse after uncertain creation and URL return recovery.

- [ ] **Step 2: Run tests and verify RED**

Run: `node --test tests/test_sms_lab_recharge_frontend.js`

Expected: failures because the recharge UI functions do not exist.

- [ ] **Step 3: Implement the hand-drawn recharge modal**

Add a 44px minimum “充值” button beside the USD balance and another action in the insufficient-balance helper. Render three package cards with exact labels `$1 / ¥6.80`, `$5 / ¥34.00`, `$10 / ¥68.00`. Include a close button, status region with `aria-live="polite"`, confirmation button and recent recharge list. Preserve the existing paper, ink, sun and lilac tokens.

- [ ] **Step 4: Implement safe creation, checkout and recovery**

Generate and persist one request ID per username and package until the server returns a known terminal order. Disable create controls while the POST is pending. Open only the server-provided HTTPS checkout URL whose host is `chat.bnbscheduler.top`. Poll the same-origin detail endpoint, guard every response with the current account epoch, update the wallet from the server response, and stop polling when the modal closes or the order is terminal. On `?recharge_order=<id>`, reopen the modal, load that order and remove only the consumed query parameter with `history.replaceState`.

- [ ] **Step 5: Run frontend and backend tests**

Run:

```bash
node --test tests/test_sms_lab_search.js tests/test_sms_lab_recharge_frontend.js
/Users/xhlm/Desktop/Workplace/maxcourse/venv/bin/python -m pytest tests/test_sms_lab.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add sms-lab/index.html sms-lab/reseller.js tests/test_sms_lab_recharge_frontend.js tests/test_sms_lab.py
git commit -m "feat: add SMS wallet recharge interface"
```

---

### Task 6: Integration verification, documentation and release

**Files:**
- Create: `/tmp/maxcourse-sms-recharge.DbR0SP/docs/superpowers/specs/2026-09-13-sms-market-search-recharge-design.md`
- Keep: `/tmp/maxcourse-sms-recharge.DbR0SP/docs/superpowers/plans/2026-09-13-sms-market-search-recharge.md`
- Modify: `/tmp/maxcourse-sms-recharge.DbR0SP/SMS_LAB_SETUP.md`
- Modify: `/tmp/omnichat-sms-recharge.UDkjhf/README.md`

**Interfaces:**
- Consumes: all previous tasks
- Produces: deployable commits and operator configuration instructions

- [ ] **Step 1: Add configuration documentation**

Document the OmniChat `sms_market` payment block, the MAXCOURSE `OMNICHAT_RECHARGE_API_BASE` variable, deployment order, callback ownership, recovery behavior and the fixed exchange rate. Do not include live AID, App Secret, admin token or API key.

- [ ] **Step 2: Run full local verification**

Run:

```bash
/Users/xhlm/Desktop/Study/OmniChat/.venv/bin/python -m unittest discover -s tests -q
/Users/xhlm/Desktop/Workplace/maxcourse/venv/bin/python -m pytest tests/ -q
node --test tests/test_sms_lab_search.js tests/test_sms_lab_recharge_frontend.js
git diff --check
```

Expected: both Python suites and both Node suites pass with no diff whitespace errors. If the known OmniChat full-suite concurrency tests fail, rerun each failing test alone and then rerun the complete suite once before classifying it as a regression.

- [ ] **Step 3: Perform local browser acceptance**

Start both applications with isolated temporary databases and XorPay fixture configuration. Verify desktop and 390px mobile layouts: login, search `ChatGPT`, select OpenAI, open recharge, select 1 USD, create order, open checkout, simulate a valid signed callback, return, observe one USD balance increase, refresh and observe no second increase.

- [ ] **Step 4: Commit documentation**

```bash
git -C /tmp/maxcourse-sms-recharge.DbR0SP add docs/superpowers/specs/2026-09-13-sms-market-search-recharge-design.md docs/superpowers/plans/2026-09-13-sms-market-search-recharge.md SMS_LAB_SETUP.md
git -C /tmp/maxcourse-sms-recharge.DbR0SP commit -m "docs: describe SMS Market online recharge"
git -C /tmp/omnichat-sms-recharge.UDkjhf add README.md
git -C /tmp/omnichat-sms-recharge.UDkjhf commit -m "docs: document SMS Market payment orders"
```

- [ ] **Step 5: Deploy OmniChat first**

Push the reviewed OmniChat commits to `main`, update `/opt/OmniChat/data/payment.json` with the non-secret `sms_market` block while preserving existing secrets, restart `omnichat.service`, and verify the current revision plus authenticated config endpoint.

- [ ] **Step 6: Deploy MAXCOURSE second**

Push the reviewed MAXCOURSE commits to `main`, wait for the deployment workflow, verify the production revision, `/api/sms-lab/services`, authenticated recharge config and browser flow.

- [ ] **Step 7: Run a controlled payment acceptance**

Create one 1 USD order for the operator's authenticated test account. Verify the generated checkout requests exactly 6.80 CNY. Do not complete the external payment without separate operator authorization. If the user completes it, verify one and only one `online_recharge` ledger row and a 1 USD wallet increase.
