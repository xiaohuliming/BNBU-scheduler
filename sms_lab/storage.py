"""SQLite schema and money helpers for the SMS reseller ledger."""

import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_UP


PRICE_SCALE = 10_000
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
