# SMS Lab deployment setup

SMS Lab is a guarded, single tenant console for authorized SMS verification testing.
It is not a public reseller storefront. The HeroSMS key stays in the Flask process.

## Required environment variables

```bash
HERO_SMS_API_KEY=replace-with-herosms-key
SMS_LAB_ACCESS_TOKEN=replace-with-a-random-value-at-least-20-characters
SMS_LAB_ALLOWED_SERVICES=replace-with-approved-service-codes
SMS_LAB_PURCHASES_ENABLED=1
```

Use a comma separated allowlist for `SMS_LAB_ALLOWED_SERVICES`. Real purchases remain
disabled if this value is empty. Generate a strong access token with a password manager.
Use `SMS_LAB_ALLOWED_SERVICES=*` only for a tightly controlled private console where every
person holding the access token is trusted to spend the shared HeroSMS balance.

## Optional controls

```bash
SMS_LAB_ALLOWED_COUNTRIES=2,6
SMS_LAB_MAX_PRICE=2.00
SMS_LAB_SESSION_TTL_MINUTES=120
HERO_SMS_MIN_REQUEST_INTERVAL=0.15
```

An empty country allowlist permits all countries. One browser session can hold no more
than three active numbers. Every purchase is forced to one number and the configured
price ceiling. The server rejects attempts to operate activations created by another
browser session.

After changing environment variables, restart `maxcourse.service` and open
`/sms-lab/index.html`. Follow HeroSMS rules and local law. Do not use temporary numbers
for banking, paid subscriptions, unsolicited account creation, or any illegal purpose.
