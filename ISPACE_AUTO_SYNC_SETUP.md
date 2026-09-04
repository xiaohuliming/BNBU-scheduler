# MAXCOURSE iSpace Auto-Sync Setup

Users can continue to run a one-time manual sync without storing their password.
Half-hourly auto-sync is a separate opt-in feature. It stores the iSpace password as a
Fernet authenticated-encryption token and never returns the token through the API.

The server must retain the encryption key because it needs to decrypt the password
briefly when logging in to iSpace. Losing the key makes every saved credential
unreadable. Anyone who obtains both the database and the key can decrypt the saved
credentials, so keep them in separate access-controlled locations.

## Environment variables

Generate the Fernet key once:

```bash
./venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Generate the dispatch secret:

```bash
openssl rand -hex 32
```

Add both values to the environment file loaded by `maxcourse.service`:

```ini
MAXCOURSE_ISPACE_CREDENTIAL_KEY="the-generated-fernet-key"
MAXCOURSE_ISPACE_SYNC_SECRET="the-generated-dispatch-secret"
```

Restrict the file to root:

```bash
chmod 600 /etc/maxcourse/email.env
```

Restart the app after updating the environment:

```bash
systemctl daemon-reload
systemctl restart maxcourse.service
```

## Half-hourly dispatcher

The protected endpoint is:

```text
POST /api/todos/auto-sync/dispatch
X-Auto-Sync-Secret: MAXCOURSE_ISPACE_SYNC_SECRET
```

Run it every 30 minutes, at minute 00 and minute 30 of every hour. The
dispatcher processes only users who explicitly enabled auto-sync and who still
have an encrypted credential. Configure `maxcourse-ispace-sync.timer` with:

```ini
[Timer]
OnCalendar=*:0/30
Persistent=true
Unit=maxcourse-ispace-sync.service
```

After changing the timer, run `systemctl daemon-reload` and
`systemctl restart maxcourse-ispace-sync.timer`. This timer only pulls DDLs;
`maxcourse-notifications.timer` controls reminder delivery independently.

After three consecutive authentication failures, auto-sync is disabled and the
saved credential is deleted. Temporary network and iSpace service failures remain
eligible for the next half-hourly retry. Authentication failures are still capped
at three consecutive attempts; do not change this policy when adjusting cadence.
