"""SQLite schema and money helpers for the SMS reseller ledger."""

import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_UP

from .recharge import OmniRechargeError, validate_paid_recharge


PRICE_SCALE = 10_000
FREE_TRIAL_LIMIT_UNITS = 5_000
ACTIVE_ORDER_STATUSES = ("purchasing", "active", "code_received")


def init_sms_lab_tables(cursor):
    try:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN sms_wallet_units INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            provider_activation_id TEXT UNIQUE,
            service_code TEXT NOT NULL,
            service_name TEXT NOT NULL,
            country_id INTEGER NOT NULL,
            country_name TEXT NOT NULL,
            phone TEXT,
            provider_cost_units INTEGER NOT NULL DEFAULT 0,
            sale_price_units INTEGER NOT NULL,
            refunded_units INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            provider_status INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id),
            CHECK (provider_cost_units >= 0),
            CHECK (sale_price_units >= 0),
            CHECK (refunded_units >= 0)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sms_wallet_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            order_id INTEGER,
            amount_units INTEGER NOT NULL,
            balance_after_units INTEGER NOT NULL,
            kind TEXT NOT NULL,
            reference TEXT NOT NULL UNIQUE,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(order_id) REFERENCES sms_orders(id)
        )
        """
    )
    try:
        cursor.execute(
            "ALTER TABLE sms_orders ADD COLUMN trial_discount_units INTEGER NOT NULL DEFAULT 0 "
            "CHECK (trial_discount_units >= 0 AND trial_discount_units <= sale_price_units)"
        )
    except sqlite3.OperationalError:
        pass
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sms_trial_claims (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            order_id INTEGER NOT NULL UNIQUE REFERENCES sms_orders(id),
            status TEXT NOT NULL CHECK (status IN ('reserved', 'used')),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_orders_user_created "
        "ON sms_orders (user_id, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_orders_user_status "
        "ON sms_orders (user_id, status)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_wallet_ledger_user_created "
        "ON sms_wallet_ledger (user_id, created_at DESC)"
    )


def amount_to_units(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid money amount")
    if not amount.is_finite():
        raise ValueError("invalid money amount")
    return int((amount * PRICE_SCALE).to_integral_value(rounding=ROUND_UP))


def units_to_amount(units):
    return round(int(units or 0) / PRICE_SCALE, 4)


def sale_units_for_cost(cost_units, markup_percent):
    numerator = int(cost_units) * (100 + int(markup_percent))
    return (numerator + 99) // 100


def free_trial_status(conn, user_id):
    """A new SMS customer gets one order, never a cash wallet grant."""
    claim = conn.execute(
        "SELECT status FROM sms_trial_claims WHERE user_id = ?", (user_id,)
    ).fetchone()
    if claim:
        status = claim[0]
    elif conn.execute(
        "SELECT 1 FROM sms_orders WHERE user_id = ? "
        "AND status IN ('code_received', 'completed') LIMIT 1", (user_id,)
    ).fetchone():
        status = 'used'
    elif conn.execute(
        "SELECT 1 FROM sms_orders WHERE user_id = ? "
        "AND status IN ('purchasing', 'active') LIMIT 1", (user_id,)
    ).fetchone():
        status = 'reserved'
    else:
        status = 'available'
    return {'available': status == 'available', 'status': status,
            'max_price': units_to_amount(FREE_TRIAL_LIMIT_UNITS), 'currency': 'USD'}


def consume_free_trial(conn, user_id, order_id):
    """Persist successful use even if a later status update changes the order."""
    conn.execute(
        "INSERT INTO sms_trial_claims (user_id, order_id, status) VALUES (?, ?, 'used') "
        "ON CONFLICT(user_id) DO UPDATE SET status = 'used', updated_at = CURRENT_TIMESTAMP",
        (user_id, order_id),
    )


def settle_paid_sms_recharge(conn, user_id: int, order: dict) -> tuple[bool, int]:
    """Atomically claim one upstream payment and credit its local wallet once."""
    applied, balance = settle_paid_sms_recharges(conn, user_id, [order])
    return applied[0], balance


def settle_paid_sms_recharges(conn, user_id: int, orders: list[dict]) -> tuple[list[bool], int]:
    """Preflight and settle an entire payment batch in one wallet transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        settled = set()
        for order in orders:
            validate_paid_recharge(order)
            reference = f"online_recharge:{order['id']}"
            existing = conn.execute(
                "SELECT user_id FROM sms_wallet_ledger WHERE reference = ?", (reference,)
            ).fetchone()
            if existing is not None:
                if existing[0] != user_id:
                    raise OmniRechargeError("此充值订单已归属其他账号。", 409, "recharge_order_conflict")
                settled.add(reference)
        user = conn.execute(
            "SELECT sms_wallet_units FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if user is None:
            raise OmniRechargeError("请重新登录后继续。", 401, "login_required")
        balance = user[0]
        applied = []
        for order in orders:
            reference = f"online_recharge:{order['id']}"
            if reference in settled:
                applied.append(False)
                continue
            balance += order["wallet_units"]
            conn.execute("UPDATE users SET sms_wallet_units = ? WHERE id = ?", (balance, user_id))
            conn.execute(
                "INSERT INTO sms_wallet_ledger "
                "(user_id, amount_units, balance_after_units, kind, reference) "
                "VALUES (?, ?, ?, 'online_recharge', ?)",
                (user_id, order["wallet_units"], balance, reference),
            )
            settled.add(reference)
            applied.append(True)
        conn.commit()
        return applied, balance
    except Exception:
        conn.rollback()
        raise
