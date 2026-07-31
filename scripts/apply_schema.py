#!/usr/bin/env python3
"""Применяет db/schema.sql к базе (замена psql, которого нет на машине).

Использование:
  DATABASE_URL=postgres://... python3 scripts/apply_schema.py
  python3 scripts/apply_schema.py --db-url postgres://...

Схема идемпотентна — повторный запуск безопасен.
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "db" / "schema.sql"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    args = ap.parse_args()
    if not args.db_url:
        sys.exit("нужен DATABASE_URL (env) или --db-url")

    sql = SCHEMA.read_text(encoding="utf-8")
    with psycopg.connect(args.db_url) as conn:
        conn.execute(sql)
        conn.commit()
    print(f"[ok] схема применена: {SCHEMA.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
