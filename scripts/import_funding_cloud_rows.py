#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import sys


TABLES = {
    "crypto_funding_formation_watch_items": "INSERT OR REPLACE",
    "crypto_funding_formation_prediction_logs": "INSERT OR IGNORE",
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in TABLES:
        raise SystemExit("unsupported funding cloud import table")
    table = sys.argv[1]
    rows = json.load(sys.stdin)
    if not isinstance(rows, list) or not rows:
        print(0)
        return 0
    keys = list(rows[0])
    if not keys or any(not str(key).replace("_", "").isalnum() for key in keys):
        raise SystemExit("invalid funding cloud import columns")
    placeholders = ",".join("?" for _ in keys)
    sql = f"{TABLES[table]} INTO {table} ({','.join(keys)}) VALUES ({placeholders})"
    with sqlite3.connect("/var/lib/astro-funding-cloud/app.db") as db:
        db.executemany(sql, ([row.get(key) for key in keys] for row in rows))
        db.commit()
    print(len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
