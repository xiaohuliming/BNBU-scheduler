# MAXCOURSE Email Notification Setup

This project sends DDL reminder emails through SMTP. The app does not store SMTP
passwords in the database or frontend; production credentials must be configured
as environment variables on the server that runs `app.py`.

## Recommended Sender

Use a domain sender such as:

- `ddl@bnbscheduler.top`
- `notify@bnbscheduler.top`
- `noreply@bnbscheduler.top`

The email provider must verify `bnbscheduler.top` with the DNS records it gives
you, usually SPF, DKIM, and sometimes DMARC.

## Environment Variables

```bash
export MAXCOURSE_PUBLIC_BASE_URL="https://www.bnbscheduler.top"
export MAXCOURSE_NOTIFICATION_SECRET="replace-with-a-long-random-token"

export SMTP_HOST="smtp.your-provider.com"
export SMTP_PORT="587"
export SMTP_USE_TLS="true"
export SMTP_USE_SSL="false"
export SMTP_USERNAME="your-smtp-username"
export SMTP_PASSWORD="your-smtp-password"
export SMTP_FROM_EMAIL="notify@bnbscheduler.top"
export SMTP_FROM_NAME="MAXCOURSE DDL"
export SMTP_REPLY_TO="support@bnbscheduler.top"
```

Use port `465` with `SMTP_USE_SSL=true` if your provider requires implicit TLS.

## Dispatch Job

The app exposes a protected dispatch endpoint for cron or a server scheduler:

```bash
curl -X POST "https://www.bnbscheduler.top/api/notifications/dispatch" \
  -H "Content-Type: application/json" \
  -H "X-Notification-Secret: $MAXCOURSE_NOTIFICATION_SECRET" \
  -d '{}'
```

A good starting schedule is every 30 minutes:

```cron
*/30 * * * * curl -fsS -X POST "https://www.bnbscheduler.top/api/notifications/dispatch" -H "X-Notification-Secret: YOUR_SECRET" -H "Content-Type: application/json" -d '{}' >/dev/null
```

Test without sending:

```bash
curl -X POST "https://www.bnbscheduler.top/api/notifications/dispatch" \
  -H "Content-Type: application/json" \
  -H "X-Notification-Secret: $MAXCOURSE_NOTIFICATION_SECRET" \
  -d '{"dry_run": true}'
```

## User Flow

1. User opens `DDL`.
2. User opens the bell menu.
3. User enters an email address, chooses reminder windows, and enables email reminders.
4. The server dispatch job checks unfinished, non-stale DDLs and sends one reminder
   for the closest configured window, such as `24h`, `3h`, or `1h`.
5. Successfully sent reminders are recorded in `email_notification_deliveries` so
   the same reminder window is not sent twice.

