#!/usr/bin/env python3
"""Seed a local demo database with synthetic data.

Creates data/merge-demo.db with two composite-key target tables. All data is
synthetic and local; no network or production account is involved.

Usage:
    .venv/bin/python scripts/seed_demo.py [db_path]
"""

from __future__ import annotations

import os
import sqlite3
import sys


def seed(db_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE accounts(
            region  TEXT NOT NULL,
            id      INTEGER NOT NULL,
            name    TEXT NOT NULL,
            status  TEXT NOT NULL DEFAULT 'new',
            score   INTEGER,
            balance REAL NOT NULL DEFAULT 0,
            tier    TEXT,
            PRIMARY KEY (region, id)
        );
        CREATE TABLE ledger(
            book TEXT NOT NULL,
            seq  INTEGER NOT NULL,
            amt  REAL,
            note TEXT,
            PRIMARY KEY (book, seq)
        );
        INSERT INTO accounts(region,id,name,status,score,balance,tier) VALUES
            ('cn', 1, 'alpha', 'active', 10, 100.0, 'gold'),
            ('cn', 2, 'beta',  'active',  5, 200.0, 'silver'),
            ('cn', 3, 'gamma', 'frozen',  2, 300.0, NULL),
            ('us', 1, 'delta', 'active',  7,  50.0, 'bronze'),
            ('us', 2, 'epsil', 'churned', 1,   0.0, NULL);
        INSERT INTO ledger(book,seq,amt,note) VALUES
            ('cash', 1, 10.0, 'opening'),
            ('cash', 2, -3.5, 'refund'),
            ('bank', 1, 500.0, 'opening');
        """
    )
    conn.commit()
    conn.close()
    print(f"seeded synthetic demo database at {db_path}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/merge-demo.db"
    seed(path)
