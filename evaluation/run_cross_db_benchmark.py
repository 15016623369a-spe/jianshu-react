"""Compare a fixed retail aggregation on SQLite and PostgreSQL."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg

from app.hard_policy_data import prepare_hard_policy_database

CASES = (
    ("电水壶", "华南", "2026-10-01", "2026-11-01"),
    ("电水壶", "华南", "2026-11-01", "2026-12-01"),
    ("蓝牙耳机", "华北", "2026-10-01", "2026-11-01"),
    ("投影仪", "华东", "2026-10-01", "2026-11-01"),
)
SQL = """SELECT COUNT(*)::bigint, COALESCE(SUM(amount_cents), 0)::bigint,
                COALESCE(SUM(refund_cents), 0)::bigint,
                COALESCE(SUM(amount_cents - refund_cents), 0)::bigint
         FROM orders WHERE product = %s AND region = %s AND ordered_on >= %s AND ordered_on < %s"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default="host=127.0.0.1 port=55432 dbname=bench user=postgres password=bench")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory() as directory:
        sqlite_path = prepare_hard_policy_database(Path(directory))
        with sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=ro", uri=True) as sqlite:
            rows = sqlite.execute("SELECT id, ordered_on, product, region, amount_cents, refund_cents FROM orders").fetchall()
        with psycopg.connect(args.dsn) as postgres:
            with postgres.cursor() as cursor:
                cursor.execute("DROP TABLE IF EXISTS orders")
                cursor.execute("""CREATE TABLE orders (
                    id INTEGER PRIMARY KEY, ordered_on DATE NOT NULL, product TEXT NOT NULL,
                    region TEXT NOT NULL, amount_cents INTEGER NOT NULL, refund_cents INTEGER NOT NULL)""")
                cursor.executemany("INSERT INTO orders VALUES (%s, %s, %s, %s, %s, %s)", rows)
            cases = []
            for product, region, start, end in CASES:
                with sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=ro", uri=True) as sqlite:
                    left = sqlite.execute(
                        "SELECT COUNT(*), COALESCE(SUM(amount_cents),0), COALESCE(SUM(refund_cents),0), "
                        "COALESCE(SUM(amount_cents-refund_cents),0) FROM orders WHERE product=? AND region=? "
                        "AND ordered_on>=? AND ordered_on<?", (product, region, start, end),
                    ).fetchone()
                with postgres.cursor() as cursor:
                    cursor.execute(SQL, (product, region, start, end))
                    right = cursor.fetchone()
                cases.append({"product": product, "region": region, "start": start, "end": end,
                              "sqlite": [int(value) for value in left], "postgres": [int(value) for value in right],
                              "ok": tuple(int(value) for value in left) == tuple(int(value) for value in right)})
        report = {"suite": "cross-db-retail-v1", "cases": cases,
                  "ok": all(case["ok"] for case in cases)}
        if args.output:
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
