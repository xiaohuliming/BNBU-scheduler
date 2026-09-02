# MAXCOURSE iSpace Auto-Sync Setup

Users can continue to run a one-time manual sync without storing their password.
Daily auto-sync is a separate opt-in feature. It stores the iSpace password as a
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

## Daily dispatcher

The protected endpoint is:

```text
POST /api/todos/auto-sync/dispatch
X-Auto-Sync-Secret: MAXCOURSE_ISPACE_SYNC_SECRET
```

Run it once each morning. A recommended schedule is 06:10 Beijing time. The
dispatcher processes only users who explicitly enabled auto-sync and who still
have an encrypted credential.

After three consecutive authentication failures, auto-sync is disabled and the
saved credential is deleted. Temporary network and iSpace service failures remain
eligible for the next daily retry.
