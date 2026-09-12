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
