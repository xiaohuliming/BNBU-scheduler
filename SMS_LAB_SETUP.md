# SMS Market reseller setup

SMS Market is a public reseller storefront backed by one HeroSMS account. Buyers use
their MAXCOURSE account and an isolated USD wallet. The HeroSMS key remains in the Flask
process and is never sent to the browser.

Apply for HeroSMS Reseller status before accepting public orders. Every upstream order
includes the local user ID as `resellerUserId`.

## Required environment variables

```bash
HERO_SMS_API_KEY=replace-with-herosms-key
SMS_LAB_ACCESS_TOKEN=replace-with-a-random-admin-token-at-least-20-characters
SMS_RESELLER_MARKUP_PERCENT=50
```

`SMS_LAB_ACCESS_TOKEN` now protects administrator wallet operations. It is not a buyer
login credential. Buyers use the existing MAXCOURSE registration and login endpoints.

## Optional catalog controls

```bash
SMS_RESELLER_ALLOWED_SERVICES=*
SMS_RESELLER_BLOCKED_SERVICE_CODES=
SMS_RESELLER_BLOCKED_SERVICE_TERMS=
HERO_SMS_MIN_REQUEST_INTERVAL=0.15
```

Banking, cryptocurrency, payment-wallet, lending, and paid-subscription services are
blocked by default to follow HeroSMS rules. Add exact codes or lowercase name fragments
to the block variables when another service must be removed.

## Online wallet recharge

Online recharge uses OmniChat's existing XorPay Alipay checkout. OmniChat owns the
payment orders, merchant credentials and `/api/pay/xorpay/notify` callback. MAXCOURSE
owns the SMS USD wallet. SMS Market payments never add OmniChat credits.

Set this optional MAXCOURSE environment variable to the trusted OmniChat HTTPS origin:

```bash
OMNICHAT_RECHARGE_API_BASE=https://chat.bnbscheduler.top
```

This is also the default. The production client accepts only this hostname, HTTPS and
the default TLS port. It does not permit alternate destinations through configuration.
The buyer must have a valid shared `sso_token`; a MAXCOURSE session alone is insufficient.
Use the existing shared account database and parent-domain cookie configuration.
Never copy XorPay credentials into MAXCOURSE.

On OmniChat, merge this non-secret block into the private `data/payment.json`, retaining
the existing `provider`, `public_base_url` and `xorpay` merchant settings:

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

The fixed rate is `1 USD = 6.80 CNY`. The three packages credit 1, 5 or 10 USD and
charge exactly 680, 3400 or 6800 CNY cents. Orders lock their amount at creation.
Wallet accounting uses integer ten-thousandths of one USD. Service purchases retain
the separate 50 percent reseller markup.

Release OmniChat first, verify its authenticated `/api/recharge/sms-market/config`,
then release MAXCOURSE and verify `/api/sms-lab/recharge/config`. Both releases require
the final branch review. Check effective revisions, package amounts, shared login and
the SMS Market page after release. A checkout may be inspected without paying; actual
payment requires the operator's explicit authorization.

The browser return parameter only starts an authenticated order query. A signed and
validated XorPay callback marks the OmniChat order paid. MAXCOURSE then writes the
unique `online_recharge:<order_id>` wallet reference and updates the balance together
in one transaction. Repeated callbacks, polling and refreshes cannot add it twice.
If MAXCOURSE is unavailable at payment time, the paid order remains in OmniChat and
opening SMS Market or querying the order settles it later. If OmniChat is unavailable,
the current wallet stays unchanged and the UI reports the temporary failure. Reuse an
uncertain create request's `request_id` to recover the same order. Do not manually
credit an order already recorded as paid without checking its wallet reference first.

## Manual wallet credit

Send the administrator token only over HTTPS. Reuse the same `reference` to make retries
idempotent.

```bash
curl https://www.bnbscheduler.top/api/sms-lab/admin/credit \
  -H 'Content-Type: application/json' \
  --data '{
    "access_token": "ADMIN_TOKEN",
    "username": "BUYER_USERNAME",
    "amount": 10,
    "reference": "credit_20260913_001",
    "note": "Manual credit"
  }'
```

## Administrator summary

```bash
curl https://www.bnbscheduler.top/api/sms-lab/admin/summary \
  -H 'Content-Type: application/json' \
  --data '{"access_token":"ADMIN_TOKEN"}'
```

Money is stored as integer ten-thousandths of one USD. Purchases reserve the marked-up
sale price before the HeroSMS request. Upstream failures and accepted cancellations
credit the same customer sale price back exactly once.
