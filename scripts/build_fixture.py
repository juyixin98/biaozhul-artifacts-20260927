"""Build the local synthetic SQLite fixture (fixtures/shop.db).

Run:  python scripts/build_fixture.py

All rows are synthetic, non-sensitive demonstration data. The resulting
database is opened read-only/immutable by the review service.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "fixtures" / "shop.db"

SCHEMA = """
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS audit_events;

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    display_name TEXT NOT NULL,
    country TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    stock INTEGER NOT NULL,
    category TEXT NOT NULL
);
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

USERS = [
    (1, "ada@example.test", "Ada", "US", "2026-01-04T09:10:00Z"),
    (2, "lin@example.test", "Lin", "CN", "2026-02-11T14:30:00Z"),
    (3, "mara@example.test", "Mara", "DE", "2026-03-21T08:05:00Z"),
]
ORDERS = [
    (101, 1, "paid", 1999, "2026-04-01T10:00:00Z"),
    (102, 1, "shipped", 4250, "2026-04-02T11:15:00Z"),
    (103, 2, "paid", 750, "2026-04-03T16:45:00Z"),
    (104, 3, "pending", 12000, "2026-04-04T07:20:00Z"),
]
PRODUCTS = [
    (1, "SKU-001", "Notebook", 1299, 40, "stationery"),
    (2, "SKU-002", "Pen", 199, 300, "stationery"),
    (3, "SKU-003", "Lamp", 4599, 12, "home"),
    (4, "SKU-004", "Mug", 899, 80, "home"),
]
AUDIT = [
    (1, "system", "fixture.init", "2026-01-01T00:00:00Z"),
]


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?)", USERS
        )
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?)", ORDERS
        )
        conn.executemany(
            "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", PRODUCTS
        )
        conn.executemany(
            "INSERT INTO audit_events VALUES (?, ?, ?, ?)", AUDIT
        )
        conn.commit()
    finally:
        conn.close()
    os.chmod(DB_PATH, 0o444)
    print(f"built {DB_PATH} with "
          f"{len(USERS)} users, {len(ORDERS)} orders, "
          f"{len(PRODUCTS)} products, {len(AUDIT)} audit events")
    return 0


if __name__ == "__main__":
    sys.exit(main())
